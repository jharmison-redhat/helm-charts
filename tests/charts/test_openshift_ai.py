"""Focused tests for the openshift-ai chart. The module now covers the
configurable GatewayClass references with namespace-selective Route
attachment and per-gateway replicas, the bounded MaasTenantConfig
configuration hook, strict duplicate-key YAML parsing, the fail-closed
maas-db bootstrap job, HardwareProfile Node/Queue scheduling, and the
secured, bounded EvalHub credential-update job.

Render tests run the rendered chart through ``helm template`` against a
local stub of the postgres-cluster dependency (no registry access);
script tests execute the rendered hook and bootstrap scripts with a
fake ``oc``. No real API or network is contacted.
"""

import base64
import json
import os
import re
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


class _StrictLoader(yaml.SafeLoader):
    pass


def _no_duplicate_keys(loader, node, deep=False):
    mapping = {}
    for k_node, v_node in node.value:
        key = loader.construct_object(k_node, deep=True)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", k_node.start_mark)
        mapping[key] = loader.construct_object(v_node, deep=True)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


def strict_docs(text):
    """Documents with duplicate mapping keys rejected."""
    return [d for d in yaml.load_all(text, Loader=_StrictLoader) if d is not None]


class StrictYamlTests(unittest.TestCase):
    def test_rejects_duplicate_keys(self):
        with self.assertRaises(yaml.constructor.ConstructorError):
            strict_docs("a: 1\na: 2")

    def test_accepts_unique_keys(self):
        self.assertEqual(strict_docs("a: 1\nb:\n  c: 2"), [{"a": 1, "b": {"c": 2}}])


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


DB_URI = "postgresql://maas:secret@pg-app:5432/maas\n"


class MaasDbJobRenderTests(ChartFixture):
    """Render tests for the maas-db bootstrap Job env/deadline surfaces."""

    def db_values(self, **overrides):
        values = self.managed_maas()
        db = {"create": True, "namespace": "redhat-ai-gateway-infra",
              "waitTimeoutSeconds": 240}
        db.update(overrides)
        values["maasDb"] = db
        return values

    def db_job(self, values):
        by_kind = self.by_kind(self.render(values))
        return self.pick(by_kind, "Job", "create-maas-db-config")

    def db_env(self, job):
        return {e["name"]: e["value"] for e in
                job["spec"]["template"]["spec"]["containers"][0]["env"]}

    def test_managed_secret_name_and_deadline(self):
        job = self.db_job(self.db_values())
        env = self.db_env(job)
        self.assertEqual(env["DB_SOURCE_SECRET"],
                         "test-release-postgres-cluster-app")
        self.assertEqual(env["DB_NAMESPACE"], "redhat-ai-gateway-infra")
        self.assertEqual(env["DB_WAIT_TIMEOUT"], "240")
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 270)
        self.assertEqual(job["spec"]["backoffLimit"], 3)
        annotations = job["metadata"]["annotations"]
        self.assertEqual(annotations["helm.sh/hook"], "post-install,post-upgrade")
        self.assertEqual(annotations["helm.sh/hook-delete-policy"],
                         "before-hook-creation")
        self.assertEqual(job["metadata"]["namespace"], "redhat-ai-gateway-infra")

    def test_external_secret_name(self):
        job = self.db_job(self.db_values(create=False,
                                         secretName="external-postgres-uri"))
        self.assertEqual(self.db_env(job)["DB_SOURCE_SECRET"],
                         "external-postgres-uri")

    def test_custom_wait_deadline(self):
        job = self.db_job(self.db_values(waitTimeoutSeconds=300))
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 330)
        self.assertEqual(self.db_env(job)["DB_WAIT_TIMEOUT"], "300")

    def test_invalid_wait_values_fail(self):
        for v in (0, -5, "240", 1.5, True, None, 10 ** 15):
            self.render(self.db_values(waitTimeoutSeconds=v),
                        expect_failure=True)

    def test_gates(self):
        # Default maasDb (create false, secretName empty) renders nothing.
        self.assertNotIn("Job", self.by_kind(self.render(self.managed_maas())))
        # Both toggles unset in an explicit block: nothing.
        self.assertNotIn("Job", self.by_kind(
            self.render(self.db_values(create=False, secretName=""))))
        # Non-Managed modelsAsAService: nothing.
        values = self.db_values()
        values["dataScienceCluster"]["components"]["aigateway"][
            "modelsAsAService"] = {"managementState": "Removed"}
        self.assertNotIn("Job", self.by_kind(self.render(values)))

    def test_rendered_resources_parse_strictly(self):
        self.stub_dependency()
        text = subprocess.run(
            ["helm", "template", "test-release", self.dir, "--namespace",
             "test-release-ns", "--values",
             self.values_file(self.db_values())],
            capture_output=True, text=True,
        )
        self.assertEqual(text.returncode, 0, text.stderr)
        rendered = strict_docs(text.stdout)
        jobs = [d for d in rendered if d["kind"] == "Job"
                and d["metadata"]["name"] == "create-maas-db-config"]
        self.assertEqual(len(jobs), 1)


class MaasDbScriptTests(ChartFixture):
    """Executes the rendered maas-db script against a fake oc binary."""

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="oa-dbscript-")
        self.addCleanup(shutil.rmtree, self.work)
        self.target = os.path.join(self.work, "target-secret.yaml")
        self.tmpdir = os.path.join(self.work, "pod-tmp")
        os.makedirs(self.tmpdir)

    def run_script(self, scenario, wait_timeout=10):
        values = self.managed_maas()
        values["maasDb"] = {"create": False, "secretName": "src-secret",
                            "namespace": "maas-ns", "waitTimeoutSeconds": 240}
        data = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                         "create-maas-db-config-script")["data"]
        for name, content in data.items():
            with open(os.path.join(self.work, name), "w") as f:
                f.write(content)
        os.chmod(os.path.join(self.work, "create-maas-db-config.sh"), 0o755)
        with open(os.path.join(self.work, "oc"), "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/bash
                echo "$@" >> "$OC_LOG"
                if [[ "$1" == "get" && "$2" == "secret" ]]; then
                  if [[ "$*" == *metadata.name* ]]; then
                    case "$DB_SCENARIO" in
                      absent|timeout|missing_ns)
                        # ignore-not-found contract: rc 0, empty stdout
                        exit 0 ;;
                      absent_then_present)
                        n=$(cat "$OC_STATE" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$OC_STATE"
                        [ "$n" -eq 1 ] && exit 0
                        echo src-secret ;;
                      forbidden)
                        echo 'Error from server (Forbidden): forbidden' >&2
                        exit 1 ;;
                      transport404)
                        echo 'Error from server (Timeout): request 404 gateway timeout' >&2
                        exit 1 ;;
                      *)
                        echo src-secret ;;
                    esac
                  elif [[ "$*" == *data.uri* ]]; then
                    case "$DB_SCENARIO" in
                      read_fail)
                        echo 'Error from server: error executing jsonpath' >&2
                        exit 1 ;;
                      invalid_b64)
                        echo 'not!!valid' ;;
                      empty_uri)
                        exit 0 ;;
                      whitespace_uri)
                        printf '%s' "$(printf '   \\n' | base64)" ;;
                      *)
                        printf '%s' "$(printf '%s' "$DB_URI" | base64)" ;;
                    esac
                  fi
                  exit 0
                fi
                if [[ "$1" == "create" ]]; then
                  if [[ "$DB_SCENARIO" == "create_fail" ]]; then
                    echo 'Error: create rejected' >&2
                    exit 1
                  fi
                  # The dry-run manifest goes to stdout (feeding the pipe);
                  # echo the decoded --from-file payload as stringData
                  src=""
                  for a in "$@"; do
                    case "$a" in --from-file=DB_CONNECTION_URL=*) src="${{a##*=}}" ;; esac
                  done
                  echo 'apiVersion: v1'
                  echo 'kind: Secret'
                  echo 'stringData:'
                  echo "  DB_CONNECTION_URL: $(cat "$src")"
                  exit 0
                fi
                if [[ "$1" == "apply" ]]; then
                  if [[ "$DB_SCENARIO" == "apply_fail" ]]; then
                    # Hostile simulated client error carrying the decoded
                    # URI in its body
                    echo 'Error: apply rejected' >&2
                    echo "$DB_URI" >&2
                    exit 1
                  fi
                  cat > "$DB_TARGET_FILE"
                  exit 0
                fi
                exit 0
                """))
        os.chmod(os.path.join(self.work, "oc"), 0o755)
        env = dict(
            os.environ,
            DB_SCENARIO=scenario,
            DB_URI=DB_URI,
            DB_SOURCE_SECRET="src-secret",
            DB_NAMESPACE="maas-ns",
            DB_WAIT_TIMEOUT=str(wait_timeout),
            DB_TARGET_FILE=self.target,
            OC_LOG=os.path.join(self.work, "commands.log"),
            OC_STATE=os.path.join(self.work, "state"),
            PATH=f"{self.work}:{os.environ['PATH']}",
            TMPDIR=self.tmpdir,
        )
        return subprocess.run(
            [os.path.join(self.work, "create-maas-db-config.sh")],
            capture_output=True, text=True, env=env, timeout=60)

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def readiness_calls(self):
        return sum(1 for l in self.log().splitlines()
                   if "metadata.name" in l)

    def assert_no_mutation(self):
        self.assertNotRegex(self.log(), r"^create |^apply ")
        self.assertFalse(os.path.exists(self.target))

    def assert_uri_absent(self, result):
        combined = result.stdout + result.stderr + self.log()
        self.assertNotIn("postgresql://", combined)

    def test_absent_then_present(self):
        result = self.run_script("absent_then_present", wait_timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.readiness_calls(), 2)
        self.assert_uri_absent(result)

    def test_successful_payload(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("applied", result.stdout)
        with open(self.target) as f:
            target = f.read()
        # The URI reaches the target only through --from-file, byte-exact
        self.assertIn(DB_URI.rstrip("\n"), target)
        argv = [l for l in self.log().splitlines() if l.startswith("create ")]
        self.assertEqual(len(argv), 1)
        self.assertIn("--from-file=DB_CONNECTION_URL=", argv[0])
        self.assertNotIn("postgresql://", argv[0])
        self.assert_uri_absent(result)

    def test_bounded_timeout_names_namespace_and_secret(self):
        result = self.run_script("timeout", wait_timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)
        self.assertIn("maas-ns", result.stderr)
        self.assertIn("src-secret", result.stderr)
        self.assertLessEqual(self.readiness_calls(), 4)
        self.assert_no_mutation()
        self.assert_uri_absent(result)

    def test_missing_namespace_times_out(self):
        # A missing namespace is NotFound: rc 0, no marker, retried to timeout
        result = self.run_script("missing_ns", wait_timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)
        self.assertIn("maas-ns", result.stderr)
        self.assertIn("src-secret", result.stderr)
        self.assert_no_mutation()

    def test_forbidden_fails_immediately(self):
        result = self.run_script("forbidden")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected failure", result.stderr)
        self.assertEqual(self.readiness_calls(), 1)
        self.assert_no_mutation()

    def test_transport_failure_with_404_text_fails(self):
        # A transport diagnostic containing "404" is NOT startup absence
        result = self.run_script("transport404")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected failure", result.stderr)
        self.assertEqual(self.readiness_calls(), 1)
        self.assert_no_mutation()

    def test_read_failure_without_mutation(self):
        result = self.run_script("read_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        self.assert_uri_absent(result)

    def test_invalid_base64_fails_without_mutation(self):
        result = self.run_script("invalid_b64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not valid base64", result.stderr)
        self.assert_no_mutation()
        self.assert_uri_absent(result)

    def test_empty_uri_fails_without_mutation(self):
        result = self.run_script("empty_uri")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        self.assert_uri_absent(result)

    def test_whitespace_uri_fails_without_mutation(self):
        result = self.run_script("whitespace_uri")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        self.assert_uri_absent(result)

    def test_create_failure_propagates(self):
        result = self.run_script("create_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to apply", result.stderr)
        self.assert_uri_absent(result)

    def test_apply_failure_propagates_without_leaking_uri(self):
        result = self.run_script("apply_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to apply", result.stderr)
        self.assertNotIn("postgresql://", result.stderr)
        self.assert_uri_absent(result)

    def test_request_timeouts_bounded_to_ten_seconds(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        get_lines = [l for l in self.log().splitlines()
                     if l.startswith("get ")]
        self.assertTrue(get_lines)
        for line in get_lines:
            timeout_args = [a for a in line.split()
                            if a.startswith("--request-timeout=")]
            self.assertEqual(len(timeout_args), 1)
            seconds = int(timeout_args[0].split("=")[1].rstrip("s"))
            self.assertLessEqual(seconds, 10)
            self.assertGreater(seconds, 0)

    def test_wall_clock_capped_for_small_wait(self):
        # With a 5s wait and an absent secret the script must give up well
        # inside the poll interval overshoot: capped sleeps, bounded gets.
        result = self.run_script("absent", wait_timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertLessEqual(self.readiness_calls(), 4)

    def test_cleanup_removes_private_files(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        # The EXIT trap removed the private temp dir (the target yaml lives
        # in the test work dir, not the script's TMPDIR)
        self.assertEqual(os.listdir(self.tmpdir), [])
        result = self.run_script("invalid_b64")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.listdir(self.tmpdir), [])


class HwpProfileTests(ChartFixture):
    """Render tests for HardwareProfile scheduling: top-level type/kueue
    values contract. Shape-only — no claim of live CRD acceptance."""

    def hwp_values(self, profiles):
        return {"hardwareProfiles": profiles}

    def values_example(self):
        """The commented hardwareProfiles example in values.yaml, uncommented."""
        with open(os.path.join(CHART, "values.yaml")) as f:
            lines = f.read().splitlines()
        out, capture = [], False
        for line in lines:
            if line.startswith("# hardwareProfiles:"):
                capture = True
            elif capture and not line.startswith("#"):
                break
            if capture:
                out.append(re.sub(r"^# ?", "", line))
        return yaml.safe_load("\n".join(out))

    def test_default_and_explicit_node(self):
        profiles = {
            "plain": {"displayName": "Plain"},
            "noded": {"type": "Node", "node": {"tolerations": [
                {"key": "nvidia.com/gpu", "effect": "NoSchedule",
                 "operator": "Exists"}]}},
        }
        by_kind = self.by_kind(self.render(self.hwp_values(profiles)))
        plain = self.pick(by_kind, "HardwareProfile", "plain")["spec"]["scheduling"]
        self.assertEqual(plain["type"], "Node")
        self.assertEqual(plain["node"], {})
        self.assertNotIn("kueue", plain)
        noded = self.pick(by_kind, "HardwareProfile", "noded")["spec"]["scheduling"]
        self.assertEqual(noded["type"], "Node")
        self.assertEqual(noded["node"], {"tolerations": [
            {"key": "nvidia.com/gpu", "effect": "NoSchedule",
             "operator": "Exists"}]})

    def test_queue_kueue_map(self):
        profiles = {"a100-queue": {
            "type": "Queue",
            "kueue": {"localQueueName": "gpu-queue",
                      "extra": {"supported": "keys preserved"}},
            "displayName": "NVIDIA A100 (Kueue queue)",
        }}
        by_kind = self.by_kind(self.render(self.hwp_values(profiles)))
        scheduling = self.pick(by_kind, "HardwareProfile", "a100-queue")[
            "spec"]["scheduling"]
        self.assertEqual(scheduling["type"], "Queue")
        self.assertEqual(scheduling["kueue"], {
            "localQueueName": "gpu-queue",
            "extra": {"supported": "keys preserved"}})
        self.assertNotIn("node", scheduling)

    def test_queue_missing_local_queue_name_fails(self):
        for kueue in (None, {}, {"extra": {"supported": "keys preserved"}}):
            profiles = {"a100-queue": {"type": "Queue"}}
            if kueue is not None:
                profiles["a100-queue"]["kueue"] = kueue
            result = self.render(self.hwp_values(profiles), expect_failure=True)
            self.assertIn("hardwareProfiles.a100-queue", result.stderr)

    def test_values_example_renders_queue(self):
        by_kind = self.by_kind(self.render(self.hwp_values(
            self.values_example()["hardwareProfiles"])))
        scheduling = self.pick(by_kind, "HardwareProfile", "a100-queue")[
            "spec"]["scheduling"]
        self.assertEqual(scheduling["type"], "Queue")
        self.assertEqual(scheduling["kueue"]["localQueueName"], "gpu-queue")
        l40s = self.pick(by_kind, "HardwareProfile", "l40s")["spec"]["scheduling"]
        self.assertEqual(l40s["type"], "Node")
        self.assertNotIn("kueue", l40s)


EVALHOOK = "post-install,post-upgrade"
EVALHOOK_DELETE = "before-hook-creation"
EH_ACCESS = 'AKIA"quoted\\backslash'
EH_SECRET = "s3cr3t-key\nwith trailing newline"


class EvalHubJobRenderTests(ChartFixture):
    """Render tests for the update-secret-minio hook Job."""

    def evalhub_values(self, evalhub_create=True, tenant_create=True,
                       **tenant_overrides):
        tenant = {"create": tenant_create, "namespace": "evalhub-tenant",
                  "waitTimeoutSeconds": 240}
        tenant.update(tenant_overrides)
        return {"evalhub": {"create": evalhub_create, "tenant": tenant}}

    def job(self, values):
        return self.pick(self.by_kind(self.render(values)), "Job",
                         "update-secret-minio")

    def env(self, job):
        return {e["name"]: e["value"] for e in
                job["spec"]["template"]["spec"]["containers"][0]["env"]}

    def test_hooks_and_deadline(self):
        job = self.job(self.evalhub_values())
        annotations = job["metadata"]["annotations"]
        self.assertEqual(annotations["helm.sh/hook"], EVALHOOK)
        self.assertEqual(annotations["helm.sh/hook-delete-policy"],
                         EVALHOOK_DELETE)
        self.assertEqual(job["metadata"]["namespace"], "evalhub-tenant")
        self.assertEqual(job["metadata"]["name"], "update-secret-minio")
        self.assertEqual(job["spec"]["backoffLimit"], 3)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 270)
        self.assertEqual(job["spec"]["template"]["spec"]["serviceAccountName"],
                         "patch-secret")
        env = self.env(job)
        self.assertEqual(env["TENANT_NAMESPACE"], "evalhub-tenant")
        self.assertEqual(env["TENANT_WAIT_TIMEOUT"], "240")
        # bash -ec: pipefail without credential xtrace
        args = job["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertEqual(args[0], "-ec")

    def test_hooks_absent_from_supporting_resources(self):
        by_kind = self.by_kind(self.render(self.evalhub_values()))
        for kind in ("ServiceAccount", "RoleBinding", "Role", "Namespace",
                     "EvalHub"):
            self.assertIn(kind, by_kind)
            # Supporting objects stay ordinary release resources: no hook
            # annotations anywhere outside the hook Job
            for doc in by_kind[kind]:
                annotations = doc["metadata"].get("annotations") or {}
                self.assertNotIn("helm.sh/hook", annotations)
                self.assertNotIn("helm.sh/hook-delete-policy", annotations)
        self.assertEqual(
            [d["metadata"]["name"] for d in by_kind["ServiceAccount"]
             if d["metadata"]["name"] == "patch-secret"], ["patch-secret"])

    def test_hook_manifest_classification(self):
        # Model Helm manifest classification, not a cluster migration: with
        # hooks enabled the update-secret-minio Job belongs to the hook
        # manifest set (tagged with the hook annotations — that tagging is
        # what makes a failed hook affect release status), while its
        # supporting objects are ordinary manifests without hook annotations.
        by_kind = self.by_kind(self.render(self.evalhub_values()))
        job = self.pick(by_kind, "Job", "update-secret-minio")
        annotations = job["metadata"]["annotations"]
        self.assertEqual(annotations["helm.sh/hook"], EVALHOOK)
        self.assertEqual(annotations["helm.sh/hook-delete-policy"],
                         EVALHOOK_DELETE)
        for kind in ("ServiceAccount", "RoleBinding", "Role", "Namespace",
                     "DataSciencePipelinesApplication"):
            for doc in by_kind[kind]:
                self.assertNotIn("helm.sh/hook",
                                 doc["metadata"].get("annotations") or {})
                self.assertNotIn("helm.sh/hook-delete-policy",
                                 doc["metadata"].get("annotations") or {})

    def test_gates(self):
        self.assertNotIn("Job", self.by_kind(self.render({})))
        self.assertNotIn("Job", self.by_kind(
            self.render(self.evalhub_values(evalhub_create=False))))
        self.assertNotIn("Job", self.by_kind(
            self.render(self.evalhub_values(tenant_create=False))))

    def test_custom_wait_deadline(self):
        job = self.job(self.evalhub_values(waitTimeoutSeconds=300))
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 330)
        self.assertEqual(self.env(job)["TENANT_WAIT_TIMEOUT"], "300")

    def test_invalid_wait_values_fail(self):
        for v in (0, -5, "240", 1.5, True, None, 10 ** 15):
            self.render(self.evalhub_values(waitTimeoutSeconds=v),
                        expect_failure=True)


class EvalHubScriptTests(EvalHubJobRenderTests):
    """Executes the rendered update-secret-minio inline script against a
    fake oc binary."""

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="oa-ehscript-")
        self.addCleanup(shutil.rmtree, self.work)
        self.target = os.path.join(self.work, "target")
        os.makedirs(self.target)
        # An unrelated existing Secret key that the narrow update must not touch
        with open(os.path.join(self.target, "unrelated-key"), "w") as f:
            f.write("unrelated-value")

    def run_script(self, scenario, wait_timeout=10):
        values = self.evalhub_values()
        job = self.pick(self.by_kind(self.render(values)), "Job",
                        "update-secret-minio")
        container = job["spec"]["template"]["spec"]["containers"][0]
        script = container["args"][1]
        # Execute exactly as the Job does: /bin/bash -ec <inline script>
        with open(os.path.join(self.work, "oc"), "w") as f:
            f.write(textwrap.dedent(f"""\
                #!/bin/bash
                echo "$@" >> "$OC_LOG"
                if [[ "$1" == "get" && "$2" == "secret" ]]; then
                  if [[ "$*" == *metadata.name* ]]; then
                    case "$EH_SCENARIO" in
                      absent|timeout)
                        # ignore-not-found contract: rc 0, empty stdout
                        exit 0 ;;
                      absent_then_present)
                        n=$(cat "$OC_STATE" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "$OC_STATE"
                        [ "$n" -eq 1 ] && exit 0
                        echo ds-pipeline-s3-dspa ;;
                      forbidden)
                        echo 'Error from server (Forbidden): forbidden' >&2
                        exit 1 ;;
                      transport404)
                        echo 'Error from server (Timeout): request 404 gateway timeout' >&2
                        exit 1 ;;
                      *)
                        echo ds-pipeline-s3-dspa ;;
                    esac
                  elif [[ "$*" == *data.accesskey* ]]; then
                    case "$EH_SCENARIO" in
                      read_fail)
                        echo 'Error from server: error executing jsonpath' >&2
                        exit 1 ;;
                      empty_uri)
                        exit 0 ;;
                      whitespace_uri)
                        printf '%s' "$(printf '   \\n' | base64)" ;;
                      invalid_b64)
                        echo 'not!!valid' ;;
                      *)
                        printf '%s' "$(printf '%s' "$EH_ACCESS" | base64)" ;;
                    esac
                  elif [[ "$*" == *data.secretkey* ]]; then
                    case "$EH_SCENARIO" in
                      invalid_b64)
                        echo 'not!!valid' ;;
                      secretkey_invalid)
                        echo 'not!!valid' ;;
                      secretkey_empty)
                        exit 0 ;;
                      *)
                        printf '%s' "$(printf '%s' "$EH_SECRET" | base64)" ;;
                    esac
                  fi
                  exit 0
                fi
                if [[ "$1" == "set" && "$2" == "data" ]]; then
                  if [[ "$EH_SCENARIO" == "set_fail" ]]; then
                    # Hostile simulated client error carrying the decoded
                    # synthetic credentials in its body
                    echo 'Error: set data rejected' >&2
                    echo "$EH_ACCESS" >&2
                    echo "$EH_SECRET" >&2
                    exit 1
                  fi
                  for a in "$@"; do
                    case "$a" in
                      --from-file=*)
                        arg="${{a#--from-file=}}"
                        key="${{arg%%=*}}"
                        src="${{arg#*=}}"
                        cat "$src" > "$EH_TARGET/$key" ;;
                    esac
                  done
                  exit 0
                fi
                exit 0
                """))
        os.chmod(os.path.join(self.work, "oc"), 0o755)
        env = dict(
            os.environ,
            EH_SCENARIO=scenario,
            EH_ACCESS=EH_ACCESS,
            EH_SECRET=EH_SECRET,
            TENANT_NAMESPACE="evalhub-tenant",
            TENANT_WAIT_TIMEOUT=str(wait_timeout),
            EH_TARGET=self.target,
            OC_LOG=os.path.join(self.work, "commands.log"),
            OC_STATE=os.path.join(self.work, "state"),
            PATH=f"{self.work}:{os.environ['PATH']}",
            TMPDIR=os.path.join(self.work, "pod-tmp"),
        )
        os.makedirs(env["TMPDIR"], exist_ok=True)
        return subprocess.run(["/bin/bash", "-ec", script],
                              capture_output=True, text=True,
                              env=env, timeout=60)

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def readiness_calls(self):
        return sum(1 for l in self.log().splitlines()
                   if "metadata.name" in l)

    def set_data_calls(self):
        return sum(1 for l in self.log().splitlines()
                   if l.startswith("set data"))

    def assert_no_mutation(self):
        self.assertEqual(self.set_data_calls(), 0)
        with open(os.path.join(self.target, "unrelated-key")) as f:
            self.assertEqual(f.read(), "unrelated-value")
        for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
                    "AWS_S3_ENDPOINT", "AWS_S3_BUCKET", "AWS_DEFAULT_REGION"):
            self.assertFalse(os.path.exists(os.path.join(self.target, key)))

    def assert_credentials_absent(self, result):
        combined = result.stdout + result.stderr + self.log()
        self.assertNotIn('AKIA"quoted', combined)
        self.assertNotIn("s3cr3t-key", combined)

    def test_absent_then_present(self):
        result = self.run_script("absent_then_present", wait_timeout=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.readiness_calls(), 2)
        self.assert_credentials_absent(result)

    def test_successful_update(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("updated", result.stdout)
        # The five AWS_* keys carry the private file bytes; credentials are
        # preserved byte-exact, including trailing newlines
        with open(os.path.join(self.target, "AWS_ACCESS_KEY_ID")) as f:
            self.assertEqual(f.read(), EH_ACCESS)
        with open(os.path.join(self.target, "AWS_SECRET_ACCESS_KEY")) as f:
            self.assertEqual(f.read(), EH_SECRET)
        self.assertEqual(
            open(os.path.join(self.target, "AWS_S3_ENDPOINT")).read(),
            "http://minio-dspa.evalhub-tenant.svc.cluster.local:9000")
        self.assertEqual(
            open(os.path.join(self.target, "AWS_S3_BUCKET")).read(), "mlpipeline")
        self.assertEqual(
            open(os.path.join(self.target, "AWS_DEFAULT_REGION")).read(),
            "us-east-1")
        # The narrow update leaves unrelated keys untouched
        self.assertEqual(
            open(os.path.join(self.target, "unrelated-key")).read(),
            "unrelated-value")
        set_lines = [l for l in self.log().splitlines()
                     if l.startswith("set data")]
        self.assertEqual(len(set_lines), 1)
        self.assertIn("secret/ds-pipeline-s3-dspa", set_lines[0])
        self.assertIn("--from-file=AWS_ACCESS_KEY_ID=", set_lines[0])
        self.assertNotIn("AKIA", set_lines[0])
        self.assert_credentials_absent(result)

    def test_literal_metacharacters_preserved(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        # Byte-exact regardless of shell metacharacters and trailing newline
        with open(os.path.join(self.target, "AWS_ACCESS_KEY_ID"), "rb") as f:
            self.assertEqual(f.read(), EH_ACCESS.encode())
        with open(os.path.join(self.target, "AWS_SECRET_ACCESS_KEY"), "rb") as f:
            self.assertEqual(f.read(), EH_SECRET.encode())

    def test_bounded_timeout_names_namespace_and_secret(self):
        result = self.run_script("timeout", wait_timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)
        self.assertIn("evalhub-tenant", result.stderr)
        self.assertIn("ds-pipeline-s3-dspa", result.stderr)
        self.assertLessEqual(self.readiness_calls(), 4)
        self.assert_no_mutation()
        self.assert_credentials_absent(result)

    def test_forbidden_fails_immediately(self):
        result = self.run_script("forbidden")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected failure", result.stderr)
        self.assertEqual(self.readiness_calls(), 1)
        self.assert_no_mutation()

    def test_transport_failure_with_404_text_fails(self):
        result = self.run_script("transport404")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected failure", result.stderr)
        self.assertEqual(self.readiness_calls(), 1)
        self.assert_no_mutation()

    def test_read_failure_without_mutation(self):
        result = self.run_script("read_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        self.assert_credentials_absent(result)

    def test_invalid_base64_fails_without_mutation(self):
        result = self.run_script("invalid_b64")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not valid base64", result.stderr)
        self.assert_no_mutation()
        self.assert_credentials_absent(result)

    def test_empty_credential_fails_without_mutation(self):
        result = self.run_script("empty_uri")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        self.assert_credentials_absent(result)

    def test_whitespace_credential_fails_without_mutation(self):
        result = self.run_script("whitespace_uri")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        self.assert_credentials_absent(result)

    def test_secretkey_invalid_fails_without_mutation(self):
        # accesskey decodes fine; a broken secretkey must still fail without
        # mutation
        result = self.run_script("secretkey_invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not valid base64", result.stderr)
        self.assert_no_mutation()
        self.assert_credentials_absent(result)

    def test_secretkey_empty_fails_without_mutation(self):
        result = self.run_script("secretkey_empty")
        self.assertNotEqual(result.returncode, 0)
        self.assert_no_mutation()
        self.assert_credentials_absent(result)

    def test_set_data_failure_propagates_without_leaking(self):
        # The fake oc is hostile: its error body carries the decoded
        # credentials, but the script suppresses error bodies so they must
        # not appear in the script's stdout/stderr (or argv)
        result = self.run_script("set_fail")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("failed to update the data", result.stderr)
        self.assertNotIn('AKIA"quoted', result.stdout + result.stderr)
        self.assertNotIn("s3cr3t-key", result.stdout + result.stderr)
        self.assert_credentials_absent(result)

    def test_request_timeouts_bounded_to_ten_seconds(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        get_lines = [l for l in self.log().splitlines()
                     if l.startswith("get ")]
        self.assertTrue(get_lines)
        for line in get_lines:
            timeout_args = [a for a in line.split()
                            if a.startswith("--request-timeout=")]
            self.assertEqual(len(timeout_args), 1)
            seconds = int(timeout_args[0].split("=")[1].rstrip("s"))
            self.assertLessEqual(seconds, 10)
            self.assertGreater(seconds, 0)

    def test_wall_clock_capped_for_small_wait(self):
        result = self.run_script("absent", wait_timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertLessEqual(self.readiness_calls(), 4)
        self.assert_no_mutation()

    def test_cleanup_removes_private_files(self):
        result = self.run_script("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.listdir(os.path.join(self.work, "pod-tmp")), [])
        result = self.run_script("invalid_b64")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.listdir(os.path.join(self.work, "pod-tmp")), [])


if __name__ == "__main__":
    unittest.main()
