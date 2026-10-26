"""Focused tests for the openshift-ai chart perimeter change: configurable
GatewayClass references with namespace-selective Route attachment, per-gateway
replicas, and the bounded MaasTenantConfig configuration hook.

Render tests run the rendered chart through ``helm template`` against a local
stub of the postgres-cluster dependency (no registry access); script tests
execute the rendered hook script with a fake ``oc``. No real API or network
is contacted.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "openshift-ai"))


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


class ChartFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="oa-test-")
        shutil.copytree(
            CHART, self.dir, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("charts", "Chart.lock"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def stub_dependency(self):
        dep = os.path.join(self.dir, "charts", "postgres-cluster")
        os.makedirs(dep, exist_ok=True)
        with open(os.path.join(dep, "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: postgres-cluster\nversion: 0.1.0\n")

    def values_file(self, values):
        fd, path = tempfile.mkstemp(prefix="oa-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(values, f)
        self.addCleanup(os.unlink, path)
        return path

    def render(self, values=None, expect_failure=False, namespace="test-release-ns"):
        """Renders the chart with the stub dependency; returns the docs, or
        the CalledProcessError when the render is expected to fail."""
        self.stub_dependency()
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", namespace]
        if values is not None:
            cmd += ["--values", self.values_file(values)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if expect_failure:
            self.assertNotEqual(result.returncode, 0)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return list(docs(result.stdout))

    @staticmethod
    def by_kind(docs):
        by_kind = {}
        for d in docs:
            by_kind.setdefault(d["kind"], []).append(d)
        return by_kind

    @staticmethod
    def pick(by_kind, kind, name):
        """Selects the doc of kind with metadata.name; asserts uniqueness."""
        matches = [d for d in by_kind[kind] if d["metadata"]["name"] == name]
        assert len(matches) == 1, (kind, name, [d["metadata"].get("name") for d in by_kind[kind]])
        return matches[0]

    def hook_values(self, **overrides):
        values = self.managed_maas()
        cfg = {"enabled": True, "namespace": "models-as-a-service",
               "waitTimeoutSeconds": 240, "pollIntervalSeconds": 5,
               "spec": {"telemetry": {"metrics": {"captureUser": False}}}}
        cfg.update(overrides)
        values["maasTenantConfig"] = cfg
        return values

    @staticmethod
    def hook_cm(by_kind):
        """The hook's script ConfigMap, identified by its patch.json key."""
        return next(d for d in by_kind["ConfigMap"]
                    if (d.get("data") or {}).get("patch.json") is not None)

    @staticmethod
    def managed_maas(mgmt="Managed"):
        return {"dataScienceCluster": {"create": True, "components": {
            "aigateway": {"managementState": "Managed",
                          "modelsAsAService": {"managementState": mgmt}}}}}


class RenderMatrixTests(ChartFixture):
    def test_no_gatewayclass_telemetry_telemetrypolicy(self):
        out = self.by_kind(self.render())
        self.assertNotIn("GatewayClass", out)
        self.assertNotIn("Telemetry", out)
        self.assertNotIn("TelemetryPolicy", out)

    def test_gateway_class_name_with_override(self):
        values = self.managed_maas()
        values["gateways"] = {
            "maasDefaultGateway": {"useRoute": True},
            "openshiftAiInference": {"create": True, "useRoute": True},
        }
        by_kind = self.by_kind(self.render(values))
        gateways = [d for d in by_kind["Gateway"]]
        self.assertTrue(all(g["spec"]["gatewayClassName"] == "data-science-gateway-class"
                            for g in gateways))
        routes = [d for d in by_kind["Route"]]
        self.assertTrue(all(r["spec"]["to"]["name"].endswith("-data-science-gateway-class")
                            for r in routes))

        values["gateways"]["maasDefaultGateway"]["gatewayClassName"] = 'q"uote-class'
        values["gateways"]["openshiftAiInference"]["gatewayClassName"] = "custom-class"
        by_kind = self.by_kind(self.render(values))
        gateways = {d["metadata"]["name"]: d for d in by_kind["Gateway"]}
        routes = {d["metadata"]["name"]: d for d in by_kind["Route"]}
        self.assertEqual(gateways["maas-default-gateway"]["spec"]["gatewayClassName"],
                         'q"uote-class')
        self.assertEqual(routes["maas-default-gateway"]["spec"]["to"]["name"],
                         'maas-default-gateway-q"uote-class')
        self.assertEqual(gateways["openshift-ai-inference"]["spec"]["gatewayClassName"],
                         "custom-class")
        self.assertEqual(routes["openshift-ai-inference"]["spec"]["to"]["name"],
                         "openshift-ai-inference-custom-class")

    def test_replicas_values(self):
        # Default nil omits the patch field entirely.
        values = self.managed_maas()
        values["gateways"] = {"maasDefaultGateway": {"useRoute": False}}
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "maas-default-gateway-config")
        self.assertNotIn("replicas", cm["data"].get("deployment", ""))

        for explicit in (0, 3):
            values["gateways"]["maasDefaultGateway"]["replicas"] = explicit
            cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                           "maas-default-gateway-config")
            self.assertIn(f"replicas: {explicit}", cm["data"]["deployment"])

        for invalid in (-1, 1.5):
            values["gateways"]["maasDefaultGateway"]["replicas"] = invalid
            self.render(values, expect_failure=True)

    def test_use_route_modes(self):
        for use_route in (True, False):
            values = self.managed_maas()
            values["gateways"] = {"maasDefaultGateway": {"useRoute": use_route}}
            by_kind = self.by_kind(self.render(values))
            cm = self.pick(by_kind, "ConfigMap", "maas-default-gateway-config")
            self.assertEqual("service" in cm["data"], use_route)
            # The parametersRef is independent of useRoute.
            self.assertEqual(
                self.pick(by_kind, "Gateway", "maas-default-gateway")
                ["spec"]["infrastructure"]["parametersRef"]["name"],
                "maas-default-gateway-config")
            self.assertEqual("Route" in by_kind, use_route)
            listener = self.pick(by_kind, "Gateway", "maas-default-gateway")[
                "spec"]["listeners"][0]
            self.assertEqual(listener["name"], "http" if use_route else "https")

    def test_inference_use_route_false(self):
        values = self.managed_maas()
        values["gateways"] = {"openshiftAiInference": {"useRoute": False}}
        by_kind = self.by_kind(self.render(values))
        gateway = self.pick(by_kind, "Gateway", "openshift-ai-inference")
        # parametersRef and the deployment overlay render in both useRoute
        # modes; Service/Route behavior is gated by useRoute.
        self.assertEqual(
            gateway["spec"]["infrastructure"]["parametersRef"]["name"],
            "openshift-ai-inference-config")
        self.assertEqual("Route" in by_kind, False)
        cm = self.pick(by_kind, "ConfigMap", "openshift-ai-inference-config")
        self.assertNotIn("service", cm.get("data") or {})
        self.assertNotIn("deployment", cm.get("data") or {})

    def test_route_namespace_selectors(self):
        values = self.managed_maas()
        values["gateways"] = {
            "maasDefaultGateway": {"useRoute": True},
            "openshiftAiInference": {"create": True, "useRoute": True},
        }
        gateways = {d["metadata"]["name"]: d for d in self.render(values)
                    if d["kind"] == "Gateway"}
        for name, label in (("maas-default-gateway", "maas-gateway-access"),
                            ("openshift-ai-inference", "openshift-ai-inference-access")):
            routes_ns = gateways[name]["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]
            self.assertEqual(routes_ns["from"], "Selector")
            self.assertEqual(routes_ns["selector"]["matchLabels"], {label: "true"})

        values["gateways"]["openshiftAiInference"]["routeNamespaceSelector"] = {
            "matchExpressions": [{"key": "k", "operator": "In", "values": ["a", "b"]}]}
        gateway = next(d for d in self.render(values)
                       if d["kind"] == "Gateway"
                       and d["metadata"]["name"] == "openshift-ai-inference")
        selector = gateway["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]["selector"]
        self.assertEqual(
            selector["matchExpressions"],
            [{"key": "k", "operator": "In", "values": ["a", "b"]}])

    def test_nonmap_selector_fails(self):
        # A user-supplied empty map merges back to the chart-defined default
        # (Helm cannot clear a default map with {}), so the render-time
        # rejection guards non-map selector values.
        self.render(self.managed_maas() | {"gateways": {
            "maasDefaultGateway": {"useRoute": True,
                                   "routeNamespaceSelector": "scalar"}}},
            expect_failure=True)

    def test_nulled_selector_leaf_fails(self):
        # Nulling the default matchLabels leaf DELETES it (Helm drops explicit
        # nulls for keys that exist in chart defaults), leaving an empty
        # selector that would allow routes from all namespaces.
        self.render(self.managed_maas() | {"gateways": {
            "maasDefaultGateway": {"useRoute": True,
                                   "routeNamespaceSelector": {"matchLabels": None}}}},
            expect_failure=True)

    def test_nulled_gateway_class_fails(self):
        # Nulling the values.yaml default class DELETES the key; templates do
        # not duplicate the default string, so rendering fails with a required
        # message instead of an empty gatewayClassName reference.
        result = self.render(self.managed_maas() | {"gateways": {
            "openshiftAiInference": {"gatewayClassName": None}}},
            expect_failure=True)
        self.assertIn("gatewayClassName is required", result.stderr)

    def _gateway_values(self, **maas_overrides):
        values = self.managed_maas()
        values["gateways"] = {
            "maasDefaultGateway": {"useRoute": True},
            "openshiftAiInference": {"create": True, "useRoute": True},
        }
        for gateway, overrides in maas_overrides.items():
            values["gateways"][gateway].update(overrides)
        return values

    def test_host_agreement_explicit_override(self):
        # A nonempty per-gateway hostname override wins and must agree across
        # Gateway listeners, Route hosts (useRoute) and Certificate DNS names
        # (no useRoute) for both gateways.
        values = self._gateway_values(
            maasDefaultGateway={"hostname": "maas.custom.example.org",
                                "useRoute": False},
            openshiftAiInference={"hostname": "infer.custom.example.org",
                                  "useRoute": False},
        )
        by_kind = self.by_kind(self.render(values))
        for gateway, hostname in (
                ("maas-default-gateway", "maas.custom.example.org"),
                ("openshift-ai-inference", "infer.custom.example.org")):
            listeners = self.pick(by_kind, "Gateway", gateway)["spec"]["listeners"]
            self.assertTrue(all(l["hostname"] == listeners[0]["hostname"]
                                for l in listeners))
            self.assertEqual(listeners[0]["hostname"], hostname)
            self.assertEqual(
                self.pick(by_kind, "Certificate",
                          f"{gateway}-certificate")["spec"]["dnsNames"],
                [listeners[0]["hostname"]])
            self.assertNotIn("Route", by_kind)

        values = self._gateway_values(
            maasDefaultGateway={"hostname": "maas.custom.example.org"},
            openshiftAiInference={"hostname": "infer.custom.example.org"},
        )
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(self.pick(by_kind, "Route", "maas-default-gateway")
                         ["spec"]["host"], "maas.custom.example.org")
        self.assertEqual(self.pick(by_kind, "Route", "openshift-ai-inference")
                         ["spec"]["host"], "infer.custom.example.org")

    def test_host_agreement_global_fallback(self):
        # Unset and falsey hostname overrides fall back to the global cluster
        # convention across Gateway listeners, Route hosts and Certificate DNS
        # names; disabling certificates removes them.
        for hostname in (None, "", False, 0):
            values = self._gateway_values(
                maasDefaultGateway={"hostname": hostname},
                openshiftAiInference={"hostname": hostname},
            )
            by_kind = self.by_kind(self.render(values))
            self.assertEqual(
                self.pick(by_kind, "Gateway", "maas-default-gateway")
                ["spec"]["listeners"][0]["hostname"],
                "maas.apps.cluster.example.com")
            self.assertEqual(
                self.pick(by_kind, "Route", "maas-default-gateway")["spec"]["host"],
                "maas.apps.cluster.example.com")
            self.assertEqual(
                self.pick(by_kind, "Gateway", "openshift-ai-inference")
                ["spec"]["listeners"][0]["hostname"],
                "inference.apps.cluster.example.com")
            self.assertEqual(
                self.pick(by_kind, "Route", "openshift-ai-inference")["spec"]["host"],
                "inference.apps.cluster.example.com")

        values = self._gateway_values(
            maasDefaultGateway={"useRoute": False},
            openshiftAiInference={"useRoute": False},
        ) | {"global": {"cluster": {"name": "mycluster",
                                    "baseDomain": "prod.example.io"}}}
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(
            self.pick(by_kind, "Certificate", "maas-default-gateway-certificate")
            ["spec"]["dnsNames"], ["maas.apps.mycluster.prod.example.io"])
        self.assertEqual(
            self.pick(by_kind, "Certificate", "openshift-ai-inference-certificate")
            ["spec"]["dnsNames"], ["inference.apps.mycluster.prod.example.io"])
        self.assertEqual(
            self.pick(by_kind, "Gateway", "maas-default-gateway")
            ["spec"]["listeners"][0]["hostname"],
            "maas.apps.mycluster.prod.example.io")

        values["gateways"]["maasDefaultGateway"]["certificate"] = {"create": False}
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(
            [c["metadata"]["name"] for c in by_kind.get("Certificate", [])],
            ["openshift-ai-inference-certificate"])

    def test_wrong_shape_selector_fields_fail(self):
        for sel in ({"matchLabels": ["list"]},
                    {"matchExpressions": {"map": True}}):
            self.render(self.managed_maas() | {"gateways": {
                "maasDefaultGateway": {"useRoute": True,
                                       "routeNamespaceSelector": sel}}},
                expect_failure=True)


class HookGateTests(ChartFixture):
    def test_hook_gates(self):
        by_kind = self.by_kind(self.render(self.hook_values()))
        for kind in ("Job", "ServiceAccount", "ConfigMap", "ClusterRole",
                     "ClusterRoleBinding"):
            self.assertIn(kind, by_kind)

        by_kind = self.by_kind(self.render(self.hook_values(enabled=False)))
        self.assertNotIn("Job", by_kind)
        self.assertNotIn("ServiceAccount", by_kind)
        self.assertNotIn("ClusterRole", by_kind)

        values = self.hook_values()
        values["dataScienceCluster"]["components"]["aigateway"][
            "modelsAsAService"] = {"managementState": "Removed"}
        by_kind = self.by_kind(self.render(values))
        self.assertNotIn("Job", by_kind)
        self.assertNotIn("ClusterRole", by_kind)

    def test_invalid_spec_fails(self):
        for spec in ({}, None, "scalar", ["list"]):
            self.render(self.hook_values(spec=spec), expect_failure=True)

    def test_invalid_timing_fails(self):
        for k in ("waitTimeoutSeconds", "pollIntervalSeconds"):
            for v in (0, -5, "240", 1.5):
                self.render(self.hook_values(**{k: v}), expect_failure=True)

    def test_release_namespace_resources(self):
        by_kind = self.by_kind(self.render(self.hook_values()))
        for kind in ("Job", "ServiceAccount"):
            self.assertEqual(by_kind[kind][0]["metadata"]["namespace"],
                             "test-release-ns")
        hook_cm = self.hook_cm(by_kind)
        self.assertEqual(hook_cm["metadata"]["namespace"], "test-release-ns")
        self.assertEqual(by_kind["ClusterRoleBinding"][0]["subjects"][0]["namespace"],
                         "test-release-ns")
        # The target product namespace appears only in the Job's env/patch
        # surface; the Job itself renders in the release namespace.
        self.assertNotEqual(by_kind["Job"][0]["metadata"]["namespace"],
                            "models-as-a-service")

    def test_exact_singleton_rbac(self):
        by_kind = self.by_kind(self.render(self.hook_values()))
        rules = by_kind["ClusterRole"][0]["rules"]
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]["apiGroups"], ["maas.opendatahub.io"])
        self.assertEqual(rules[0]["resources"], ["maastenantconfigs"])
        self.assertEqual(rules[0]["resourceNames"], ["default-tenant"])
        self.assertEqual(rules[0]["verbs"], ["get", "patch"])

    def test_same_release_different_namespace_rbac(self):
        # The same release name in two namespaces yields DISTINCT cluster
        # -scoped RBAC resources, each bound to its own release-namespace SA.
        names = {}
        for ns in ("ns-a", "ns-b"):
            by_kind = self.by_kind(self.render(self.hook_values(), namespace=ns))
            role = by_kind["ClusterRole"][0]
            binding = by_kind["ClusterRoleBinding"][0]
            self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
            self.assertEqual(binding["subjects"][0]["name"], role["metadata"]["name"])
            self.assertEqual(binding["subjects"][0]["namespace"], ns)
            self.assertEqual(by_kind["ServiceAccount"][0]["metadata"]["namespace"], ns)
            names[ns] = role["metadata"]["name"]
        self.assertNotEqual(names["ns-a"], names["ns-b"])
        # Names stay Kubernetes-length-valid.
        self.assertLessEqual(max(len(n) for n in names.values()), 63)

    def test_hook_job_settings(self):
        job = self.by_kind(self.render(self.hook_values()))["Job"][0]
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"], "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 270)
        env = {e["name"]: e["value"] for e in
               job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["MAAS_TENANT_NAMESPACE"], "models-as-a-service")
        self.assertEqual(env["MAAS_TENANT_WAIT_TIMEOUT"], "240")
        self.assertEqual(env["MAAS_TENANT_POLL_INTERVAL"], "5")

    def test_json_payload(self):
        hook_cm = self.hook_cm(self.by_kind(self.render(self.hook_values())))
        payload = json.loads(hook_cm["data"]["patch.json"])
        self.assertEqual(list(payload.keys()), ["spec"])
        self.assertEqual(payload["spec"]["telemetry"]["metrics"]["captureUser"], False)

        values = self.hook_values(
            spec={"telemetry": {"metrics": {"captureUser": False}, "drop": None}})
        hook_cm = self.hook_cm(self.by_kind(self.render(values)))
        payload = json.loads(hook_cm["data"]["patch.json"])
        # Supplied nulls survive to the JSON payload under an otherwise empty
        # chart-side spec.
        self.assertIsNone(payload["spec"]["telemetry"]["drop"])
        self.assertEqual(payload["spec"]["telemetry"]["metrics"]["captureUser"], False)

    def test_maas_db_glob_cannot_absorb_tenant_script(self):
        # The tenant script lives under files/maas-tenant-config/, so the
        # maas-db script ConfigMap's files/* glob cannot include it.
        values = self.hook_values()
        values["maasDb"] = {"create": False, "namespace": "redhat-ai-gateway-infra",
                            "secretName": "external-postgres-uri"}
        by_kind = self.by_kind(self.render(values))
        maas_db_cm = self.pick(by_kind, "ConfigMap", "create-maas-db-config-script")
        self.assertIn("create-maas-db-config.sh", maas_db_cm["data"])
        self.assertNotIn("configure-maas-tenant.sh", maas_db_cm["data"])
        self.assertNotIn("configure-maas-tenant", json.dumps(maas_db_cm["data"]))


class FakeOcScriptTests(ChartFixture):
    """Executes the rendered hook script against a fake oc binary."""

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="oa-script-")
        self.addCleanup(shutil.rmtree, self.work)

    def run_script(self, scenario, wait_timeout=10, poll_interval=1,
                   render_values=None):
        values = render_values if render_values is not None else self.hook_values()
        data = self.hook_cm(self.by_kind(self.render(values)))["data"]
        for name, content in data.items():
            with open(os.path.join(self.work, name), "w") as f:
                f.write(content)
        os.chmod(os.path.join(self.work, "configure-maas-tenant.sh"), 0o755)
        with open(os.path.join(self.work, "oc"), "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/bash
                echo "$@" >> "$OC_LOG"
                if [[ "$1" == "get" && "$2" == "--raw" ]]; then
                  case "$OC_SCENARIO" in
                    absent_then_present)
                      n=$(cat "$OC_STATE" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$OC_STATE"
                      if [ "$n" -eq 1 ]; then
                        echo 'Error from server (NotFound): the server could not find the requested resource' >&2
                        exit 1
                      fi
                      echo '{{"kind":"MaasTenantConfig"}}'
                      ;;
                    success|patch_fail)
                      echo '{{"kind":"MaasTenantConfig"}}'
                      ;;
                    timeout)
                      echo 'Error from server (NotFound): the server could not find the requested resource' >&2
                      exit 1
                      ;;
                    forbidden)
                      echo 'Error from server (Forbidden): forbidden' >&2
                      exit 1
                      ;;
                    transport)
                      echo 'unable to connect: connection refused' >&2
                      exit 1
                      ;;
                  esac
                else
                  case "$OC_SCENARIO" in
                    patch_fail)
                      echo 'Error: patch rejected' >&2
                      exit 1
                      ;;
                    *)
                      echo 'maastenantconfig.maas.opendatahub.io/default-tenant patched'
                      ;;
                  esac
                fi
                """))
        os.chmod(os.path.join(self.work, "oc"), 0o755)
        env = dict(
            os.environ,
            OC_SCENARIO=scenario,
            OC_LOG=os.path.join(self.work, "commands.log"),
            OC_STATE=os.path.join(self.work, "state"),
            PATH=f"{self.work}:{os.environ['PATH']}",
            MAAS_TENANT_NAMESPACE="models-as-a-service",
            MAAS_TENANT_WAIT_TIMEOUT=str(wait_timeout),
            MAAS_TENANT_POLL_INTERVAL=str(poll_interval),
            MAAS_TENANT_PATCH_FILE=os.path.join(self.work, "patch.json"),
        )
        return subprocess.run(
            [os.path.join(self.work, "configure-maas-tenant.sh")],
            capture_output=True, text=True, env=env, timeout=30)

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def test_absent_then_present(self):
        result = self.run_script("absent_then_present")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("patched"), 1)
        # One 404 retry, then success — no create/adopt operations.
        self.assertIn("--raw", self.log())
        self.assertNotRegex(self.log(), r"create|apply|adopt|replace|delete")

    def test_immediate_success(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log().count("--raw"), 1)

    def test_bounded_timeout(self):
        result = self.run_script("timeout", wait_timeout=2, poll_interval=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("did not appear", result.stderr)
        # Bounded: one extra attempt past the deadline at most.
        self.assertLessEqual(self.log().count("--raw"), 4)

    def test_forbidden_fails_without_repeated_masking(self):
        result = self.run_script("forbidden")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected failure", result.stderr)
        self.assertEqual(self.log().count("--raw"), 1)

    def test_transport_failure_fails_immediately(self):
        result = self.run_script("transport")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected failure", result.stderr)
        self.assertEqual(self.log().count("--raw"), 1)

    def test_patch_failure_surfaces_error(self):
        result = self.run_script("patch_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("patch rejected", result.stderr)

    def test_patch_file_preserves_settings_and_metacharacters(self):
        values = self.hook_values(spec={
            "telemetry": {"metrics": {"captureUser": False, "keep": None}},
            "note": 'has "quotes" & $meta `chars`'})
        result = self.run_script("success", render_values=values)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.work, "patch.json")) as f:
            payload = json.load(f)
        self.assertEqual(payload["spec"]["telemetry"]["metrics"]["captureUser"], False)
        self.assertIsNone(payload["spec"]["telemetry"]["metrics"]["keep"])
        self.assertEqual(payload["spec"]["note"], 'has "quotes" & $meta `chars`')

    def test_patch_command_argv(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        patch_lines = [l for l in self.log().splitlines()
                       if l.startswith("patch ")]
        self.assertEqual(len(patch_lines), 1)
        argv = patch_lines[0].split()
        self.assertEqual(argv[0], "patch")
        self.assertEqual(argv[1], "maastenantconfig")
        self.assertEqual(argv[2], "default-tenant")
        self.assertIn("--namespace", argv)
        self.assertEqual(argv[argv.index("--namespace") + 1], "models-as-a-service")
        self.assertIn("--type=merge", argv)
        self.assertEqual(argv[argv.index("--patch-file") + 1],
                         os.path.join(self.work, "patch.json"))


if __name__ == "__main__":
    unittest.main()
