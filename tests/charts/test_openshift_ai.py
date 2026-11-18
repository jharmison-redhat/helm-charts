"""Focused tests for the openshift-ai chart. The module covers the
configurable GatewayClass references with namespace-selective Route
attachment and per-gateway replicas, the bounded MaasTenantConfig
configuration hook, strict duplicate-key YAML parsing, the fail-closed
maas-db bootstrap job, HardwareProfile Node/Queue scheduling, and the
secured, bounded EvalHub credential-update job.

Render tests run the rendered chart through ``helm template`` against a
local stub of the postgres-cluster dependency (no registry access). Per
the repo's unit-test policy the script-execution layer is a minimal smoke
set: ONE happy-path run per hook/bootstrap script and ONE fail-closed
no-tool check for the chart. Readiness/timeout/error-body behavior is
proven by live-cluster validation, not fixtures.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

from support import docs
import support

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "openshift-ai"))


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


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "oa-test-"
    values_prefix = "oa-values-"

    def render(self, values=None, expect_failure=False,
               namespace="test-release-ns"):
        """Renders the chart with the stub dependency; returns the docs, or
        the failed result when the render is expected to fail."""
        self.stub_dependency()
        result = self.helm_template(values, namespace)
        if expect_failure:
            self.assertNotEqual(result.returncode, 0)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return list(docs(result.stdout))

    @staticmethod
    def docs_named(by_kind, kind, fragment):
        """The docs of kind whose metadata.name carries the fragment — the
        chart renders several independent hook families, so kind-wide
        assertions must be scoped per family."""
        return [d for d in by_kind.get(kind, []) if fragment in d["metadata"]["name"]]

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

    def evalhub_values(self, evalhub_create=True, tenant_create=True,
                       **tenant_overrides):
        tenant = {"create": tenant_create, "namespace": "evalhub-tenant",
                  "waitTimeoutSeconds": 240}
        tenant.update(tenant_overrides)
        return {"evalhub": {"create": evalhub_create, "tenant": tenant}}


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
    # The MaasTenantConfig hook's resources all carry "maas-tenant-config" in
    # their names; the chart's other hook families share the kinds, so
    # assertions are scoped by that fragment.
    FRAGMENT = "maas-tenant-config"

    def test_hook_gates(self):
        by_kind = self.by_kind(self.render(self.hook_values()))
        for kind in ("Job", "ServiceAccount", "ConfigMap", "ClusterRole",
                     "ClusterRoleBinding"):
            self.assertTrue(self.docs_named(by_kind, kind, self.FRAGMENT),
                            kind)

        by_kind = self.by_kind(self.render(self.hook_values(enabled=False)))
        for kind in ("Job", "ServiceAccount", "ClusterRole",
                     "ClusterRoleBinding"):
            self.assertEqual(self.docs_named(by_kind, kind, self.FRAGMENT),
                             [], kind)

        values = self.hook_values()
        values["dataScienceCluster"]["components"]["aigateway"][
            "modelsAsAService"] = {"managementState": "Removed"}
        by_kind = self.by_kind(self.render(values))
        for kind in ("Job", "ClusterRole"):
            self.assertEqual(self.docs_named(by_kind, kind, self.FRAGMENT),
                             [], kind)

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
            self.assertEqual(self.docs_named(by_kind, kind, self.FRAGMENT)[0]
                             ["metadata"]["namespace"], "test-release-ns")
        hook_cm = self.hook_cm(by_kind)
        self.assertEqual(hook_cm["metadata"]["namespace"], "test-release-ns")
        binding = self.docs_named(by_kind, "ClusterRoleBinding",
                                  self.FRAGMENT)[0]
        self.assertEqual(binding["subjects"][0]["namespace"],
                         "test-release-ns")
        # The target product namespace appears only in the Job's env/patch
        # surface; the Job itself renders in the release namespace.
        self.assertNotEqual(self.docs_named(by_kind, "Job",
                                            self.FRAGMENT)[0]
                            ["metadata"]["namespace"],
                            "models-as-a-service")

    def test_exact_singleton_rbac(self):
        by_kind = self.by_kind(self.render(self.hook_values()))
        rules = self.docs_named(by_kind, "ClusterRole", self.FRAGMENT)[0][
            "rules"]
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
            role = self.docs_named(by_kind, "ClusterRole", self.FRAGMENT)[0]
            binding = self.docs_named(by_kind, "ClusterRoleBinding",
                                      self.FRAGMENT)[0]
            self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
            self.assertEqual(binding["subjects"][0]["name"], role["metadata"]["name"])
            self.assertEqual(binding["subjects"][0]["namespace"], ns)
            self.assertEqual(self.docs_named(by_kind, "ServiceAccount",
                                             self.FRAGMENT)[0]
                             ["metadata"]["namespace"], ns)
            names[ns] = role["metadata"]["name"]
        self.assertNotEqual(names["ns-a"], names["ns-b"])
        # Names stay Kubernetes-length-valid.
        self.assertLessEqual(max(len(n) for n in names.values()), 63)

    def test_hook_job_settings(self):
        job = self.docs_named(self.by_kind(self.render(self.hook_values())),
                              "Job", self.FRAGMENT)[0]
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
        # The tenant script lives under files/maas-tenant-config/, the
        # EvalHub credential script under files/evalhub/, and the replica
        # hook scripts under files/replicas/, so the maas-db script
        # ConfigMap's files/* glob cannot include any of them
        values = self.hook_values()
        values["maasDb"] = {"create": False, "namespace": "redhat-ai-gateway-infra",
                            "secretName": "external-postgres-uri"}
        by_kind = self.by_kind(self.render(values))
        maas_db_cm = self.pick(by_kind, "ConfigMap", "create-maas-db-config-script")
        self.assertIn("create-maas-db-config.sh", maas_db_cm["data"])
        self.assertNotIn("configure-maas-tenant.sh", maas_db_cm["data"])
        self.assertNotIn("update-secret-minio.sh", maas_db_cm["data"])
        self.assertNotIn("configure-maas-tenant", json.dumps(maas_db_cm["data"]))
        self.assertNotIn("evalhub", json.dumps(maas_db_cm["data"]))
        self.assertNotIn("replicas", json.dumps(maas_db_cm["data"]))
        # The operand hook families deliver their scripts from family/lib
        # subdirectories, so the root-level files/* glob cannot absorb them
        # either.
        self.assertNotIn("set-chart-created-cr", json.dumps(maas_db_cm["data"]))
        self.assertNotIn("converge-operator-configs", json.dumps(maas_db_cm["data"]))
        self.assertNotIn("dashboard-config", json.dumps(maas_db_cm["data"]))
        self.assertNotIn("mlflow", json.dumps(maas_db_cm["data"]))


class FakeOcScriptTests(ChartFixture):
    """Executes the rendered configure-maas-tenant.sh hook script against a
    fake oc binary (smoke set: one happy-path run plus the chart's
    fail-closed no-tool check).

    Intentionally separate from the shared HookScriptTests harness: this
    script uses a different oc surface (oc get --raw and oc patch on the
    MaasTenantConfig singleton) than the secret-reading bootstrap/credential
    scripts (secret get / set data --from-file)."""

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="oa-script-")
        self.addCleanup(shutil.rmtree, self.work)

    def stage_scripts(self):
        values = self.hook_values()
        data = self.hook_cm(self.by_kind(self.render(values)))["data"]
        for name, content in data.items():
            with open(os.path.join(self.work, name), "w") as f:
                f.write(content)
        os.chmod(os.path.join(self.work, "configure-maas-tenant.sh"), 0o755)

    def run_script(self, wait_timeout=10, poll_interval=1):
        self.stage_scripts()
        with open(os.path.join(self.work, "oc"), "w") as f:
            f.write(textwrap.dedent("""\
                #!/bin/bash
                echo "$@" >> "$OC_LOG"
                if [[ "$1" == "get" && "$2" == "--raw" ]]; then
                  echo '{"kind":"MaasTenantConfig"}'
                else
                  echo 'maastenantconfig.maas.opendatahub.io/default-tenant patched'
                fi
                """))
        os.chmod(os.path.join(self.work, "oc"), 0o755)
        env = dict(
            os.environ,
            OC_LOG=os.path.join(self.work, "commands.log"),
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

    def test_happy_path_smoke(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        # One readiness read, then exactly one merge patch; the script
        # neither creates, adopts, nor deletes the operator-managed
        # singleton.
        self.assertEqual(self.log().count("--raw"), 1)
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
        self.assertNotRegex(self.log(), r"create|apply|adopt|replace|delete")

    def test_no_tools_fails_closed(self):
        # The scripts carry no dual jq/python3 preamble (they use oc
        # jsonpath directly), so the fail-closed contract here is that a
        # tool-less PATH fails loudly during the readiness wait, before any
        # mutation is attempted.
        self.stage_scripts()
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir)
        for tool in ("date", "sleep"):
            os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
        env = dict(
            os.environ,
            PATH=bin_dir,
            MAAS_TENANT_NAMESPACE="models-as-a-service",
            MAAS_TENANT_WAIT_TIMEOUT="2",
            MAAS_TENANT_POLL_INTERVAL="1",
            MAAS_TENANT_PATCH_FILE=os.path.join(self.work, "patch.json"),
        )
        result = subprocess.run(
            [os.path.join(self.work, "configure-maas-tenant.sh")],
            capture_output=True, text=True, env=env, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unexpected failure", result.stderr)


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
        # Default maasDb (create false, secretName empty) renders no
        # maas-db plumbing; the chart's other hook families are unaffected.
        by_kind = self.by_kind(self.render(self.managed_maas()))
        self.assertEqual(self.docs_named(by_kind, "Job",
                                         "create-maas-db-config"), [])
        # Both toggles unset in an explicit block: nothing.
        by_kind = self.by_kind(self.render(
            self.db_values(create=False, secretName="")))
        self.assertEqual(self.docs_named(by_kind, "Job",
                                         "create-maas-db-config"), [])
        # Non-Managed modelsAsAService: nothing.
        values = self.db_values()
        values["dataScienceCluster"]["components"]["aigateway"][
            "modelsAsAService"] = {"managementState": "Removed"}
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(self.docs_named(by_kind, "Job",
                                         "create-maas-db-config"), [])

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


FAKE_OC = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" && "$2" == "secret" ]]; then
  if [[ "$*" == *metadata.name* ]]; then
    echo "$OC_SECRET"
  else
    key=""
    for k in $OC_KEYS; do
      if [[ "$*" == *data.${k}* ]]; then key="$k"; fi
    done
    if [ -n "$key" ]; then
      payload="$(cat "$OC_READ/$key")"
      printf '%s' "$(printf '%s' "$payload" | base64)"
    fi
  fi
  exit 0
fi
if [[ "$1" == "set" && "$2" == "data" ]]; then
  for a in "$@"; do
    case "$a" in
      --from-file=*)
        arg="${a#--from-file=}"
        key="${arg%%=*}"
        src="${arg#*=}"
        cat "$src" > "$OC_TARGET/$key" ;;
    esac
  done
  exit 0
fi
if [[ "$1" == "create" ]]; then
  src=""
  for a in "$@"; do
    case "$a" in --from-file=*) arg="${a#--from-file=}"; src="${arg#*=}" ;; esac
  done
  # The dry-run manifest goes to stdout (feeding the pipe); echo the decoded
  # --from-file payload as stringData
  echo 'apiVersion: v1'
  echo 'kind: Secret'
  echo 'stringData:'
  echo "  PAYLOAD: $(cat "$src")"
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  cat > "$OC_TARGET/apply.yaml"
  exit 0
fi
exit 0
"""


class HookScriptTests(ChartFixture):
    """Shared fake-oc harness for the secret-reading bootstrap/credential
    scripts (maas-db, EvalHub credential): temp workdir, a happy-path fake
    oc (per-key payloads via OC_READ files, mutation capture), argv log,
    and a private TMPDIR. The smoke set runs only the success path per
    script."""

    secret_name = ""        # source secret name the fake oc reports
    secret_keys = ()        # jsonpath keys the scripts read from the secret

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="oa-hookscript-")
        self.addCleanup(shutil.rmtree, self.work)
        self.target = os.path.join(self.work, "target")
        os.makedirs(self.target)
        self.read = os.path.join(self.work, "read")
        os.makedirs(self.read)
        self.tmpdir = os.path.join(self.work, "pod-tmp")
        os.makedirs(self.tmpdir)

    def set_payload(self, key, content):
        """Sets the fake-oc payload for a secret key."""
        with open(os.path.join(self.read, key), "w") as f:
            f.write(content)

    def execute(self, argv, wait_timeout, env_extra):
        with open(os.path.join(self.work, "oc"), "w") as f:
            f.write(FAKE_OC)
        os.chmod(os.path.join(self.work, "oc"), 0o755)
        env = dict(
            os.environ,
            OC_SECRET=self.secret_name,
            OC_KEYS=" ".join(self.secret_keys),
            OC_LOG=os.path.join(self.work, "commands.log"),
            OC_READ=self.read,
            OC_TARGET=self.target,
            PATH=f"{self.work}:{os.environ['PATH']}",
            TMPDIR=self.tmpdir,
        )
        env.update(env_extra or {})
        return subprocess.run(argv, capture_output=True, text=True,
                              env=env, timeout=60)

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def mutation_lines(self):
        return [l for l in self.log().splitlines()
                if l.startswith(("set data", "create ", "apply "))]

    def assert_absent(self, result, *secrets):
        combined = result.stdout + result.stderr + self.log()
        for secret in secrets:
            self.assertNotIn(secret, combined)


class MaasDbScriptTests(HookScriptTests):
    """Happy-path smoke for the rendered maas-db script (as mounted from
    its ConfigMap) against a fake oc binary."""

    secret_name = "src-secret"
    secret_keys = ("uri",)

    def setUp(self):
        super().setUp()
        self.set_payload("uri", DB_URI)

    def run_script(self, wait_timeout=10):
        values = self.managed_maas()
        values["maasDb"] = {"create": False, "secretName": "src-secret",
                            "namespace": "maas-ns", "waitTimeoutSeconds": 240}
        data = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                         "create-maas-db-config-script")["data"]
        script = os.path.join(self.work, "create-maas-db-config.sh")
        with open(script, "w") as f:
            f.write(data["create-maas-db-config.sh"])
        os.chmod(script, 0o755)
        return self.execute(
            [script], wait_timeout, dict(
                DB_SOURCE_SECRET="src-secret",
                DB_NAMESPACE="maas-ns",
                DB_WAIT_TIMEOUT=str(wait_timeout),
            ))

    def test_happy_path_smoke(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("applied", result.stdout)
        with open(os.path.join(self.target, "apply.yaml")) as f:
            target = f.read()
        # The URI reaches the target only through --from-file; the fake
        # apply-side capture carries the decoded payload
        self.assertIn(DB_URI.rstrip("\n"), target)
        argv = self.mutation_lines()
        self.assertEqual(len(argv), 2)
        self.assertIn("--from-file=DB_CONNECTION_URL=", " ".join(argv))
        self.assertNotIn("postgresql://", " ".join(argv))
        self.assert_absent(result, "postgresql://")


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
        # The script mounts from its dedicated ConfigMap, like the
        # maas-tenant-config Job
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"],
                         ["/bin/bash", "/config/update-secret-minio.sh"])
        volumes = job["spec"]["template"]["spec"]["volumes"]
        self.assertEqual(
            [(v["name"], v["configMap"]["name"]) for v in volumes],
            [("script", "update-secret-minio-script")])

    def test_script_configmap_mount(self):
        by_kind = self.by_kind(self.render(self.evalhub_values()))
        cm = self.pick(by_kind, "ConfigMap", "update-secret-minio-script")
        self.assertEqual(cm["metadata"]["namespace"], "evalhub-tenant")
        self.assertIn("update-secret-minio.sh", cm["data"])
        # The script ConfigMap is an ordinary release resource: no hooks
        annotations = cm["metadata"].get("annotations") or {}
        self.assertNotIn("helm.sh/hook", annotations)
        self.assertNotIn("helm.sh/hook-delete-policy", annotations)

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
        # The update-secret-minio Job and its script ConfigMap share the
        # gates; the chart's other hook families are unaffected.
        self.assertEqual(self.docs_named(self.by_kind(self.render({})),
                                         "Job", "update-secret-minio"), [])
        for values in (self.evalhub_values(evalhub_create=False),
                       self.evalhub_values(tenant_create=False)):
            by_kind = self.by_kind(self.render(values))
            self.assertEqual(self.docs_named(by_kind, "Job",
                                             "update-secret-minio"), [])
            configmaps = [d["metadata"]["name"]
                          for d in by_kind.get("ConfigMap", [])]
            self.assertNotIn("update-secret-minio-script", configmaps)

    def test_custom_wait_deadline(self):
        job = self.job(self.evalhub_values(waitTimeoutSeconds=300))
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 330)
        self.assertEqual(self.env(job)["TENANT_WAIT_TIMEOUT"], "300")

    def test_invalid_wait_values_fail(self):
        for v in (0, -5, "240", 1.5, True, None, 10 ** 15):
            self.render(self.evalhub_values(waitTimeoutSeconds=v),
                        expect_failure=True)


class EvalHubScriptTests(HookScriptTests):
    """Happy-path smoke for the rendered update-secret-minio script (as
    mounted from its ConfigMap) against a fake oc binary."""

    secret_name = "ds-pipeline-s3-dspa"
    secret_keys = ("accesskey", "secretkey")

    def setUp(self):
        super().setUp()
        self.set_payload("accesskey", EH_ACCESS)
        self.set_payload("secretkey", EH_SECRET)
        # An unrelated existing Secret key that the narrow update must not touch
        with open(os.path.join(self.target, "unrelated-key"), "w") as f:
            f.write("unrelated-value")

    def assert_credentials_absent(self, result):
        self.assert_absent(result, 'AKIA"quoted', "s3cr3t-key", "AKIA")

    def run_script(self, wait_timeout=10):
        script_cm = self.pick(self.by_kind(self.render(self.evalhub_values())),
                              "ConfigMap", "update-secret-minio-script")
        script = os.path.join(self.work, "update-secret-minio.sh")
        with open(script, "w") as f:
            f.write(script_cm["data"]["update-secret-minio.sh"])
        os.chmod(script, 0o755)
        return self.execute(
            ["/bin/bash", script], wait_timeout, dict(
                TENANT_NAMESPACE="evalhub-tenant",
                TENANT_WAIT_TIMEOUT=str(wait_timeout),
            ))

    def test_happy_path_smoke(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("updated", result.stdout)
        # The AWS_* keys carry the private file bytes; credentials are
        # preserved byte-exact, including trailing newlines, and unrelated
        # keys stay untouched
        with open(os.path.join(self.target, "AWS_ACCESS_KEY_ID"), "rb") as f:
            self.assertEqual(f.read(), EH_ACCESS.encode())
        with open(os.path.join(self.target, "AWS_SECRET_ACCESS_KEY"), "rb") as f:
            self.assertEqual(f.read(), EH_SECRET.encode())
        with open(os.path.join(self.target, "AWS_S3_ENDPOINT")) as f:
            self.assertEqual(f.read(),
                             "http://minio-dspa.evalhub-tenant.svc.cluster.local:9000")
        with open(os.path.join(self.target, "AWS_S3_BUCKET")) as f:
            self.assertEqual(f.read(), "mlpipeline")
        with open(os.path.join(self.target, "AWS_DEFAULT_REGION")) as f:
            self.assertEqual(f.read(), "us-east-1")
        with open(os.path.join(self.target, "unrelated-key")) as f:
            self.assertEqual(f.read(), "unrelated-value")
        set_lines = [l for l in self.log().splitlines()
                     if l.startswith("set data")]
        self.assertEqual(len(set_lines), 1)
        self.assertIn("secret/ds-pipeline-s3-dspa", set_lines[0])
        self.assertIn("--from-file=AWS_ACCESS_KEY_ID=", set_lines[0])
        self.assertNotIn("AKIA", set_lines[0])
        self.assert_credentials_absent(result)


# Fake oc for the replicas hook-script smoke runs: serves a canned
# Deployment for get and records every --patch= payload. Invocations are
# logged so tests can assert the exact oc call surface.
FAKE_OC_REPLICAS = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" ]]; then
  cat "$OC_DEPLOYMENT_FILE"
elif [[ "$1" == "patch" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == --patch=* ]]; then
      printf '%s\\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
    fi
  done
  exit 0
else
  echo "unexpected oc invocation: $*" >&2
  exit 64
fi
"""


class _ReplicasValuesMixin:
    """The replicas values block with per-test overrides."""

    def replicas_values(self, **overrides):
        values = self.managed_maas()
        values["replicas"] = {"dashboard": None, "operator": None,
                              "asCronJob": True, "schedule": "*/15 * * * *",
                              "waitTimeoutSeconds": 240}
        values["replicas"].update(overrides)
        return values


class ReplicaSurfaceTests(_ReplicasValuesMixin, ChartFixture):
    """Render contracts for the replicas management surface: presence-aware
    gating (null hands off, explicit 0 is managed), mode selection
    (CronJob drift-corrector vs partial manifests), the partial-manifest
    shape with server-side-apply annotations, RBAC scoping, and the
    platform-objects-never-render contract (the operator-created Deployments
    are release-owned only in the deliberately-opted-in partial-manifests
    mode)."""

    def surface(self, values):
        by_kind = self.by_kind(self.render(values))
        role = self.pick(by_kind, "ClusterRole",
                         [r["metadata"]["name"] for r in by_kind["ClusterRole"]
                          if "replicas" in r["metadata"]["name"]][0])
        return by_kind, role

    def components_file(self, by_kind):
        cms = [d for d in by_kind["ConfigMap"]
               if (d.get("data") or {}).get("components.txt") is not None]
        self.assertEqual(len(cms), 1)
        return cms[0]["data"]["components.txt"]

    def test_default_hands_off(self):
        # All-null counts render nothing for the replicas surface and never
        # a Deployment: the chart manages nothing by default.
        by_kind = self.by_kind(self.render())
        self.assertNotIn("CronJob", by_kind)
        self.assertNotIn("Deployment", by_kind)
        self.assertEqual(
            [c for c in by_kind.get("ClusterRole", [])
             if "replicas" in c["metadata"]["name"]], [])
        self.assertEqual([j for j in by_kind.get("Job", [])
                          if "replicas" in j["metadata"]["name"]], [])

    def test_cron_mode_never_renders_deployments(self):
        by_kind, _ = self.surface(self.replicas_values(dashboard=1,
                                                       operator=3))
        self.assertNotIn("Deployment", by_kind)
        self.assertIn("CronJob", by_kind)
        cronjob = by_kind["CronJob"][0]
        self.assertEqual(cronjob["metadata"]["namespace"], "test-release-ns")
        self.assertEqual(cronjob["spec"]["schedule"], "*/15 * * * *")
        self.assertEqual(cronjob["spec"]["concurrencyPolicy"], "Forbid")
        # The CronJob is an ordinary release manifest (GitOps-clean): no hook
        # annotations anywhere in the chart's rendered CronJob.
        self.assertNotIn("helm.sh/hook", cronjob["metadata"].get("annotations") or {})
        # The components file carries exactly the managed components with
        # their fixed namespaces.
        self.assertEqual(
            self.components_file(by_kind),
            "redhat-ods-applications rhods-dashboard 1\n"
            "redhat-ods-operator rhods-operator 3\n")

    def test_presence_aware_zero_is_managed(self):
        # Explicit 0 is managed (presence-aware, not falsey); null hands off.
        by_kind, _ = self.surface(self.replicas_values(operator=0))
        self.assertEqual(self.components_file(by_kind),
                         "redhat-ods-operator rhods-operator 0\n")
        by_kind, _ = self.surface(self.replicas_values(dashboard=2))
        self.assertEqual(self.components_file(by_kind),
                         "redhat-ods-applications rhods-dashboard 2\n")

    def test_rbac_scoped_to_managed_deployments(self):
        values = self.replicas_values(dashboard=1)
        by_kind, role = self.surface(values)
        # The replicas surface contributes exactly one ClusterRole (the
        # chart's other hook families contribute their own).
        self.assertEqual(len(self.docs_named(by_kind, "ClusterRole",
                                             "replicas")), 1)
        self.assertEqual(role["rules"], [{
            "apiGroups": ["apps"], "resources": ["deployments"],
            "resourceNames": ["rhods-dashboard"], "verbs": ["get", "patch"],
        }])
        binding = self.pick(by_kind, "ClusterRoleBinding",
                            role["metadata"]["name"])
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": role["metadata"]["name"],
            "namespace": "test-release-ns"}])
        # Only the managed Deployment is in scope: managing the operator
        # adds exactly its resourceName.
        values["replicas"]["operator"] = 0
        _, role = self.surface(values)
        self.assertEqual(role["rules"][0]["resourceNames"],
                         ["rhods-dashboard", "rhods-operator"])

    def test_hook_jobs(self):
        values = self.replicas_values(dashboard=1, waitTimeoutSeconds=300)
        by_kind, role = self.surface(values)
        fullname = role["metadata"]["name"]
        jobs = {j["metadata"]["name"]: j for j in by_kind["Job"]}
        set_job = jobs[f"{fullname}-set"]
        restore_job = jobs[f"{fullname}-restore"]
        self.assertEqual(
            set_job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            set_job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(
            restore_job["metadata"]["annotations"]["helm.sh/hook"],
            "pre-delete")
        self.assertEqual(set_job["spec"]["activeDeadlineSeconds"], 330)
        self.assertEqual(set_job["spec"]["backoffLimit"], 0)
        env = {e["name"]: e["value"] for e in
               set_job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["REPLICAS_WAIT_FOR_MISSING"], "true")
        self.assertEqual(env["REPLICAS_WAIT_TIMEOUT"], "300")
        # The CronJob's container skips missing Deployments instead of
        # failing every schedule interval.
        cron_env = {e["name"]: e["value"] for e in
                    by_kind["CronJob"][0]["spec"]["jobTemplate"]["spec"]
                    ["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(cron_env["REPLICAS_WAIT_FOR_MISSING"], "false")
        # Both hook Jobs and the CronJob mount the shared script ConfigMap.
        for job in (set_job, restore_job):
            volume = job["spec"]["template"]["spec"]["volumes"][0]
            self.assertEqual(volume["configMap"]["name"], fullname)
        cron_volume = (by_kind["CronJob"][0]["spec"]["jobTemplate"]["spec"]
                       ["template"]["spec"]["volumes"][0])
        self.assertEqual(cron_volume["configMap"]["name"], fullname)

    def test_partial_manifest_mode(self):
        values = self.replicas_values(dashboard=1, operator=0,
                                      asCronJob=False)
        by_kind, _ = self.surface(values)
        self.assertNotIn("CronJob", by_kind)
        deployments = {d["metadata"]["name"]: d
                       for d in by_kind["Deployment"]}
        self.assertEqual(set(deployments),
                         {"rhods-dashboard", "rhods-operator"})
        dashboard = deployments["rhods-dashboard"]
        self.assertEqual(dashboard["metadata"]["namespace"],
                         "redhat-ods-applications")
        self.assertEqual(deployments["rhods-operator"]["metadata"]["namespace"],
                         "redhat-ods-operator")
        for name, count in (("rhods-dashboard", 1), ("rhods-operator", 0)):
            d = deployments[name]
            # Patch shape only: annotations plus the replica count.
            self.assertEqual(
                d["metadata"]["annotations"],
                {"argocd.argoproj.io/sync-options": "ServerSideApply=true",
                 "helm.sh/resource-policy": "keep"})
            self.assertEqual(d["spec"], {"replicas": count})

    def test_partial_manifest_mode_without_counts_fails(self):
        result = self.render(self.replicas_values(asCronJob=False),
                             expect_failure=True)
        self.assertIn("nothing to ship", result.stderr)

    def test_invalid_values_fail(self):
        for bad in ({"dashboard": -1}, {"dashboard": "1"}, {"operator": 1.5},
                    {"operator": True}):
            self.render(self.replicas_values(**bad), expect_failure=True)
        # An empty schedule only matters when the CronJob renders, so pair it
        # with a managed count (with nothing managed the surface renders
        # nothing at all and the invalid schedule is moot).
        result = self.render(self.replicas_values(schedule="", dashboard=1),
                             expect_failure=True)
        self.assertIn("replicas.schedule", result.stderr)
        # The wait timeout only matters when the hook Jobs render; pair it
        # with a managed count (same gating as the empty schedule above).
        result = self.render(
            self.replicas_values(waitTimeoutSeconds=0, dashboard=1),
            expect_failure=True)
        self.assertIn("replicas.waitTimeoutSeconds", result.stderr)
        result = self.render(self.replicas_values(asCronJob="yes"),
                             expect_failure=True)
        self.assertIn("replicas.asCronJob must be a boolean (got string)",
                      result.stderr)

    def test_custom_schedule_renders(self):
        by_kind, _ = self.surface(
            self.replicas_values(dashboard=1, schedule="*/5 * * * *"))
        self.assertEqual(by_kind["CronJob"][0]["spec"]["schedule"],
                         "*/5 * * * *")


class _ReplicasScriptFixture(_ReplicasValuesMixin, ChartFixture,
                             support.FakeOcScriptFixture):
    """Executes the rendered replicas hook scripts against a fake oc binary
    (this module's FAKE_OC_REPLICAS): a restricted PATH built solely from
    symlinks in an isolated bin directory (python3 as the single JSON tool,
    core utilities, and the fake oc), the scripts staged in one directory
    (they source the shared preamble from their own directory, mirroring
    the single ConfigMap mount), and an explicit minimal child
    environment."""

    work_prefix = "oa-replicas-"
    bin_tools = ["mktemp", "grep", "sleep", "rm", "cat", "date"]
    fake_oc_script = FAKE_OC_REPLICAS

    def setUp(self):
        super().setUp()
        self.deployment_file = os.path.join(self.work, "deployment.json")
        self.patch_log = os.path.join(self.work, "patches.log")
        for stale in (self.deployment_file, self.patch_log,
                      os.path.join(self.work, "commands.log")):
            if os.path.exists(stale):
                os.unlink(stale)

    def stage_scripts(self, values):
        by_kind = self.by_kind(self.render(values))
        cms = [d for d in by_kind["ConfigMap"]
               if (d.get("data") or {}).get("components.txt") is not None]
        self.assertEqual(len(cms), 1)
        super().stage_scripts(cms[0]["data"])

    def run_script(self, script_name, deployment_json):
        bin_dir = self.build_bin_dir()
        with open(self.deployment_file, "w") as f:
            f.write(deployment_json)
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "TMPDIR": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_DEPLOYMENT_FILE": self.deployment_file,
            "OC_PATCH_LOG": self.patch_log,
            "REPLICAS_COMPONENTS_FILE": os.path.join(self.work,
                                                     "components.txt"),
            "REPLICAS_WAIT_TIMEOUT": "2",
            "REPLICAS_POLL_INTERVAL": "1",
            "REPLICAS_WAIT_FOR_MISSING": "true",
        }
        return subprocess.run(
            ["/bin/bash", os.path.join(self.work, script_name)],
            capture_output=True, text=True, env=env, timeout=60)

    @staticmethod
    def deployment_doc(replicas, annotations=None, rv="42"):
        return json.dumps({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "rhods-dashboard",
                         "namespace": "redhat-ods-applications",
                         "resourceVersion": rv,
                         "annotations": annotations or {}},
            "spec": {"replicas": replicas},
        })


class EnforceReplicasScriptTests(_ReplicasScriptFixture):
    """Minimal smoke set for the replica hook scripts, per the repo's
    unit-test policy: one happy-path run per script (python3-only tool path)
    plus the chart's fail-closed no-tool check. State-machine behavior
    (drift correction, recreate re-baselining, 409 retries, foreign-proof
    walk-back) is proven by live-cluster validation, not fixtures."""

    def test_enforce_happy_path_smoke(self):
        self.stage_scripts(self.replicas_values(dashboard=1))
        result = self.run_script("enforce-replicas.sh",
                                 self.deployment_doc(replicas=2))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        # The prior (2) is recorded under proof and the count patched to 1,
        # conditionally on the live resourceVersion.
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "42",
                         "annotations": {"openshift-ai/replicas-prior": "2"}},
            "spec": {"replicas": 1},
        })
        # The chart neither creates, adopts, nor deletes Deployments.
        self.assertNotIn("create", self.log())
        self.assertNotIn("delete", self.log())
        self.assertNotIn("apply", self.log())

    def test_restore_happy_path_smoke(self):
        self.stage_scripts(self.replicas_values(dashboard=1))
        result = self.run_script(
            "restore-replicas.sh",
            self.deployment_doc(
                replicas=1, rv="43",
                annotations={"openshift-ai/replicas-prior": "2"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        # The recorded prior is restored and the proof annotation cleared.
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "43",
                         "annotations": {"openshift-ai/replicas-prior": None}},
            "spec": {"replicas": 2},
        })
        self.assertNotIn("delete", self.log())

    def test_no_oc_fails_closed(self):
        # The chart's fail-closed no-tool check: with no oc on PATH the
        # enforce script fails before any mutation.
        self.stage_scripts(self.replicas_values(dashboard=1))
        bin_dir = self.build_bin_dir(include_oc=False)
        with open(self.deployment_file, "w") as f:
            f.write(self.deployment_doc(replicas=2))
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "TMPDIR": self.work,
            "REPLICAS_COMPONENTS_FILE": os.path.join(self.work,
                                                     "components.txt"),
            "REPLICAS_WAIT_TIMEOUT": "2",
            "REPLICAS_POLL_INTERVAL": "1",
            "REPLICAS_WAIT_FOR_MISSING": "true",
        }
        result = subprocess.run(
            ["/bin/bash", os.path.join(self.work, "enforce-replicas.sh")],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("oc CLI is not available", result.stderr)
        self.assertFalse(os.path.exists(self.patch_log))


# Fake oc for the operand hook-script smoke runs: reports the CRD wait as
# Established, serves a canned object for get (or NotFound under
# OC_OBJECT_ABSENT, or separate Auth/DSCI payloads for the cluster-scoped
# singletons), captures the created payload, records every --patch= payload,
# and accepts delete. Invocations are logged so tests can assert the exact
# oc call surface.
FAKE_OC_OPERAND = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" && "$2" == crd/* ]]; then
  echo "True"
  exit 0
fi
if [[ "$1" == "get" ]]; then
  if [ -n "$OC_OBJECT_ABSENT" ]; then
    echo "Error from server (NotFound): $2 not found" >&2
    exit 1
  fi
  if [[ -n "$OC_AUTH_FILE" && "$2" == auths* ]]; then
    cat "$OC_AUTH_FILE"
  elif [[ -n "$OC_DSCI_FILE" && "$2" == dscinitializations* ]]; then
    cat "$OC_DSCI_FILE"
  else
    cat "$OC_OBJECT_FILE"
  fi
  exit 0
fi
if [[ "$1" == "create" ]]; then
  prev=""
  for a in "$@"; do
    if [[ "$prev" == "-f" ]]; then cat "$a" > "$OC_CREATE_CAPTURE"; fi
    prev="$a"
  done
  exit 0
fi
if [[ "$1" == "patch" ]]; then
  for a in "$@"; do
    if [[ "$a" == --patch=* ]]; then
      printf '%s\\n' "${a#--patch=}" >> "$OC_PATCH_LOG"
    fi
  done
  exit 0
fi
if [[ "$1" == "delete" ]]; then
  exit 0
fi
echo "unexpected oc invocation: $*" >&2
exit 64
"""


class _OperandScriptFixture(ChartFixture, support.FakeOcScriptFixture):
    """Executes the rendered operand hook scripts against a fake oc binary
    (this module's FAKE_OC_OPERAND): the family's script ConfigMap staged
    into one directory (scripts and JSON payloads side by side, mirroring
    the single ConfigMap mount), a restricted PATH (python3 as the single
    JSON tool, core utilities, the fake oc), and per-test object JSON files
    the fake oc serves."""

    work_prefix = "oa-operand-"
    bin_tools = ["mktemp", "grep", "sleep", "rm", "cat", "date"]
    fake_oc_script = FAKE_OC_OPERAND

    FAMILY_FRAGMENT = ""  # the ConfigMap-name fragment of the family under test

    def setUp(self):
        super().setUp()
        self.object_file = os.path.join(self.work, "object.json")
        self.auth_file = os.path.join(self.work, "auth.json")
        self.dsci_file = os.path.join(self.work, "dsci.json")
        self.patch_log = os.path.join(self.work, "patches.log")
        self.create_capture = os.path.join(self.work, "created.json")

    def stage_cm(self):
        by_kind = self.by_kind(self.render())
        cms = [d for d in by_kind["ConfigMap"]
               if self.FAMILY_FRAGMENT in d["metadata"]["name"]
               and "hook-preamble.sh" in (d.get("data") or {})]
        self.assertEqual(len(cms), 1, self.FAMILY_FRAGMENT)
        self.stage_scripts(cms[0]["data"])
        return cms[0]["metadata"]["name"]

    def run_script(self, script_name, env_extra, absent=False):
        env = {
            "PATH": self.build_bin_dir(),
            "HOME": self.work,
            "TMPDIR": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_OBJECT_FILE": self.object_file,
            "OC_AUTH_FILE": self.auth_file,
            "OC_DSCI_FILE": self.dsci_file,
            "OC_PATCH_LOG": self.patch_log,
            "OC_CREATE_CAPTURE": self.create_capture,
            "WAIT_TIMEOUT": "2",
        }
        if absent:
            env["OC_OBJECT_ABSENT"] = "1"
        env.update(env_extra)
        return subprocess.run(
            ["/bin/bash", os.path.join(self.work, script_name)],
            capture_output=True, text=True, env=env, timeout=60)

    def captured_create(self):
        with open(self.create_capture) as f:
            return json.load(f)


class OperandHookRenderTests(ChartFixture):
    """Render contracts for the operand configuration hook surfaces: the
    four CRD-ordering-blocked product objects never render as release
    manifests, each hook family carries its gated Jobs and scoped RBAC, and
    the by-value payload shapes reach the script ConfigMaps."""

    def family(self, by_kind, fragment):
        return {
            kind: self.docs_named(by_kind, kind, fragment)
            for kind in ("Job", "ServiceAccount", "ClusterRole",
                         "ClusterRoleBinding", "ConfigMap")
        }

    def test_platform_objects_never_render(self):
        by_kind = self.by_kind(self.render())
        self.assertNotIn("OdhDashboardConfig", by_kind)
        self.assertNotIn("Auth", by_kind)
        self.assertNotIn("MLflow", by_kind)
        self.assertNotIn("DSCInitialization", by_kind)
        self.assertEqual([d for d in by_kind.get("ConfigMap", [])
                          if d["metadata"]["name"] == "odh-segment-key-config"],
                         [])

    def test_hook_jobs(self):
        # The chart-created families carry a set Job AND a pre-delete proof
        # -gated delete Job; the operator-configs family converges by value
        # only, so it carries a single set Job.
        expected_jobs = {"operator-configs": 1, "dashboard-config": 2,
                         "mlflow-": 2}
        by_kind = self.by_kind(self.render())
        for fragment, count in expected_jobs.items():
            fam = self.family(by_kind, fragment)
            # Supporting resources are ordinary release manifests in the
            # release namespace.
            self.assertEqual(len(fam["ServiceAccount"]), 1, fragment)
            self.assertEqual(fam["ServiceAccount"][0]["metadata"]["namespace"],
                             "test-release-ns")
            # Set Jobs run post-install/post-upgrade, delete Jobs pre-delete;
            # every hook Job is recreated via before-hook-creation.
            jobs = {j["metadata"]["name"]: j for j in fam["Job"]}
            self.assertEqual(len(jobs), count, fragment)
            for name, job in jobs.items():
                delete_policy = job["metadata"]["annotations"][
                    "helm.sh/hook-delete-policy"]
                self.assertEqual(delete_policy, "before-hook-creation", name)
                self.assertEqual(job["spec"]["backoffLimit"], 0, name)
            set_jobs = [j for j in fam["Job"]
                        if not j["metadata"]["name"].endswith("-delete")]
            delete_jobs = [j for j in fam["Job"]
                           if j["metadata"]["name"].endswith("-delete")]
            self.assertEqual(len(set_jobs), 1, fragment)
            self.assertEqual(len(delete_jobs), count - 1, fragment)
            self.assertEqual(set_jobs[0]["metadata"]["annotations"]["helm.sh/hook"],
                             "post-install,post-upgrade")
            self.assertEqual(set_jobs[0]["spec"]["activeDeadlineSeconds"], 270)
            for job in delete_jobs:
                self.assertEqual(job["metadata"]["annotations"]["helm.sh/hook"],
                                 "pre-delete", fragment)
                self.assertEqual(job["spec"]["activeDeadlineSeconds"], 300)

    def test_chart_created_cr_rbac(self):
        by_kind = self.by_kind(self.render())
        for fragment, group, plural, crd, name in (
                ("dashboard-config", "opendatahub.io", "odhdashboardconfigs",
                 "odhdashboardconfigs.opendatahub.io", "odh-dashboard-config"),
                ("mlflow-", "mlflow.opendatahub.io", "mlflows",
                 "mlflows.mlflow.opendatahub.io", "mlflow")):
            rules = self.family(by_kind, fragment)["ClusterRole"][0]["rules"]
            self.assertEqual(rules, [
                {"apiGroups": [group], "resources": [plural],
                 "resourceNames": [name],
                 "verbs": ["get", "patch", "delete"]},
                # create is the one grant RBAC cannot scope by name — it
                # cannot match inside a resourceNames-scoped rule, so it
                # carries its own unscoped rule (the set hook must create the
                # object when no default exists).
                {"apiGroups": [group], "resources": [plural],
                 "verbs": ["create"]},
                {"apiGroups": ["apiextensions.k8s.io"],
                 "resources": ["customresourcedefinitions"],
                 "resourceNames": [crd], "verbs": ["get"]},
            ], fragment)

    def test_operator_configs_rbac(self):
        # The operator-created singletons are never created or deleted by
        # this chart: get/patch only, resource-name-scoped.
        by_kind = self.by_kind(self.render())
        rules = self.family(by_kind, "operator-configs")["ClusterRole"][0][
            "rules"]
        self.assertEqual(rules, [
            {"apiGroups": ["services.platform.opendatahub.io"],
             "resources": ["auths"], "resourceNames": ["auth"],
             "verbs": ["get", "patch"]},
            {"apiGroups": ["dscinitialization.opendatahub.io"],
             "resources": ["dscinitializations"],
             "resourceNames": ["default-dsci"],
             "verbs": ["get", "patch"]},
            {"apiGroups": [""], "resources": ["configmaps"],
             "resourceNames": ["odh-segment-key-config"],
             "verbs": ["get", "patch"]},
            {"apiGroups": ["apiextensions.k8s.io"],
             "resources": ["customresourcedefinitions"],
             "resourceNames": ["auths.services.platform.opendatahub.io"],
             "verbs": ["get"]},
        ])

    def test_dashboard_config_payloads(self):
        by_kind = self.by_kind(self.render())
        cm = self.family(by_kind, "dashboard-config")["ConfigMap"][0]
        create = json.loads(cm["data"]["create.json"])
        self.assertEqual(create["apiVersion"], "opendatahub.io/v1alpha")
        self.assertEqual(create["kind"], "OdhDashboardConfig")
        self.assertEqual(create["metadata"]["name"], "odh-dashboard-config")
        self.assertEqual(create["metadata"]["namespace"],
                         "redhat-ods-applications")
        self.assertEqual(create["metadata"]["labels"],
                         {"app.kubernetes.io/part-of": "rhods-dashboard",
                          "app.opendatahub.io/rhods-dashboard": "true"})
        self.assertEqual(create["metadata"]["annotations"],
                         {"openshift-ai/chart-created": "true"})
        self.assertEqual(create["spec"], {
            "notebookController": {"enabled": True,
                                   "notebookNamespace": "rhods-notebooks",
                                   "pvcSize": "20Gi"}})
        self.assertEqual(json.loads(cm["data"]["spec.json"]),
                         {"spec": create["spec"]})

        values = self.managed_maas() | {
            "dashboardConfig": {"disableTracking": True, "drop": None},
            "templateDisablement": ["trustyai-cpu-small"],
            "templateOrder": ["jupyter-minimal-notebook"],
            "showJupyterInEnabledApplications": False,
            "defaultWorkbenchPvcSize": "40Gi",
        }
        by_kind = self.by_kind(self.render(values))
        cm = self.family(by_kind, "dashboard-config")["ConfigMap"][0]
        create = json.loads(cm["data"]["create.json"])
        self.assertEqual(create["spec"], {
            "notebookController": {"enabled": False,
                                   "notebookNamespace": "rhods-notebooks",
                                   "pvcSize": "40Gi"},
            "dashboardConfig": {"disableTracking": True, "drop": None},
            "templateDisablement": ["trustyai-cpu-small"],
            "templateOrder": ["jupyter-minimal-notebook"],
        })
        # An explicit null in dashboardConfig survives to the payload
        # (merge-patch removal semantics).
        self.assertIsNone(create["spec"]["dashboardConfig"]["drop"])

    def test_mlflow_payloads_and_gate(self):
        by_kind = self.by_kind(self.render())
        cm = self.family(by_kind, "mlflow-")["ConfigMap"][0]
        create = json.loads(cm["data"]["create.json"])
        self.assertEqual(create["apiVersion"], "mlflow.opendatahub.io/v1")
        self.assertEqual(create["kind"], "MLflow")
        self.assertEqual(create["metadata"]["name"], "mlflow")
        self.assertEqual(create["metadata"]["annotations"],
                         {"openshift-ai/chart-created": "true"})
        self.assertEqual(create["spec"]["backendStoreUri"],
                         "sqlite:////mlflow/mlflow.db")
        self.assertEqual(json.loads(cm["data"]["spec.json"]),
                         {"spec": create["spec"]})

        # Disabling MLflow removes ONLY the set hook and its create/spec
        # payloads: the proof-gated delete plumbing (delete Job, ServiceAccount,
        # ClusterRole, ClusterRoleBinding, script ConfigMap) renders
        # unconditionally so a chart-created MLflow is still walked back on
        # uninstall instead of being orphaned with its proof annotation.
        values = self.managed_maas() | {"mlflow": {"create": False}}
        by_kind = self.by_kind(self.render(values))
        fam = self.family(by_kind, "mlflow-")
        self.assertEqual(len(fam["Job"]), 1)
        self.assertTrue(fam["Job"][0]["metadata"]["name"].endswith("-delete"))
        self.assertEqual(fam["Job"][0]["metadata"]["annotations"]["helm.sh/hook"],
                         "pre-delete")
        self.assertEqual(len(fam["ServiceAccount"]), 1)
        self.assertEqual(len(fam["ClusterRole"]), 1)
        self.assertEqual(len(fam["ClusterRoleBinding"]), 1)
        self.assertEqual(len(fam["ConfigMap"]), 1)
        cm = fam["ConfigMap"][0]
        self.assertNotIn("create.json", cm["data"])
        self.assertNotIn("spec.json", cm["data"])
        self.assertIn("delete-chart-created-cr.sh", cm["data"])
        # The other families are unaffected.
        self.assertEqual(len(self.family(by_kind,
                                         "dashboard-config")["Job"]), 2)
        self.assertEqual(len(self.family(by_kind,
                                         "operator-configs")["Job"]), 1)

    def test_operator_configs_payloads(self):
        by_kind = self.by_kind(self.render())
        cm = self.family(by_kind, "operator-configs")["ConfigMap"][0]
        self.assertEqual(json.loads(cm["data"]["auth-patch.json"]), {
            "spec": {"adminGroups": ["rhods-admins"],
                     "allowedGroups": ["system:authenticated"]}})
        self.assertEqual(json.loads(cm["data"]["dsci-patch.json"]), {
            "spec": {"monitoring": {
                "managementState": "Managed",
                "namespace": "redhat-ods-monitoring",
                "metrics": {"storage": {"size": "5Gi", "retention": "1d"}}}}})
        self.assertEqual(json.loads(cm["data"]["segment-key-patch.json"]),
                         {"data": {"segmentKeyEnabled": "false"}})

        values = self.managed_maas() | {
            "userGroups": ["my-admins", "other-group"],
            "enableUserTelemetry": True,
            "dataScienceClusterInitialization": {
                "monitoring": {"managementState": "Removed",
                               "metrics": None,
                               "traces": None}},
        }
        by_kind = self.by_kind(self.render(values))
        cm = self.family(by_kind, "operator-configs")["ConfigMap"][0]
        self.assertEqual(json.loads(cm["data"]["auth-patch.json"]), {
            "spec": {"adminGroups": ["rhods-admins"],
                     "allowedGroups": ["my-admins", "other-group"]}})
        # The DSCI payload carries exactly the configured spec fields. Helm
        # coalescing DELETES a null override of a chart-default leaf
        # (metrics drops out of the payload entirely — value removal), while
        # an explicit null on a key absent from the chart defaults survives
        # (merge-patch removal semantics).
        self.assertEqual(json.loads(cm["data"]["dsci-patch.json"]), {
            "spec": {"monitoring": {
                "managementState": "Removed",
                "namespace": "redhat-ods-monitoring",
                "traces": None}}})
        self.assertEqual(json.loads(cm["data"]["segment-key-patch.json"]),
                         {"data": {"segmentKeyEnabled": "true"}})

        # Emptying the values block empties the desired spec: the converge
        # is a no-op rather than an attempt to patch anything.
        values = self.managed_maas() | {"dataScienceClusterInitialization": None}
        by_kind = self.by_kind(self.render(values))
        cm = self.family(by_kind, "operator-configs")["ConfigMap"][0]
        self.assertEqual(json.loads(cm["data"]["dsci-patch.json"]),
                         {"spec": {}})

    def test_enable_user_telemetry_validated(self):
        # The segment-key payload stringifies the value, so a null (which also
        # deletes the values.yaml default) or any non-boolean fails the render
        # with an actionable message instead of rendering "<nil>" or
        # non-boolean text.
        for v in (None, "true", 1, "yes"):
            result = self.render(
                self.managed_maas() | {"enableUserTelemetry": v},
                expect_failure=True)
            self.assertIn("enableUserTelemetry must be a boolean", result.stderr)
        for v, rendered in ((True, "true"), (False, "false")):
            values = self.managed_maas() | {"enableUserTelemetry": v}
            cm = self.family(self.by_kind(self.render(values)),
                             "operator-configs")["ConfigMap"][0]
            self.assertEqual(json.loads(cm["data"]["segment-key-patch.json"]),
                             {"data": {"segmentKeyEnabled": rendered}})

    def test_dsc_component_composition_validated(self):
        # An explicitly Removed component never establishes its CRDs, so the
        # matching hook surface would burn its full wait budget: the render
        # fails naming both keys instead.
        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["mlflowoperator"] = {
            "managementState": "Removed"}
        result = self.render(values, expect_failure=True)
        self.assertIn("dataScienceCluster.components.mlflowoperator", result.stderr)
        self.assertIn("mlflow.create", result.stderr)

        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["dashboard"] = {
            "managementState": "Removed"}
        result = self.render(values, expect_failure=True)
        self.assertIn("dataScienceCluster.components.dashboard", result.stderr)

        # Disabling the MLflow set hook skips its check — the ungated delete
        # plumbing still renders.
        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["mlflowoperator"] = {
            "managementState": "Removed"}
        values["mlflow"] = {"create": False}
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(len(self.family(by_kind, "mlflow-")["Job"]), 1)

        # The chart not rendering the DataScienceCluster leaves the
        # components tree to the operator: nothing fails.
        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["mlflowoperator"] = {
            "managementState": "Removed"}
        values["dataScienceCluster"]["create"] = False
        self.render(values)

    def test_wait_timeout_validation(self):
        for v in (0, -5, "240", 1.5, True, None):
            self.render(self.managed_maas()
                        | {"operandHookWaitTimeoutSeconds": v},
                        expect_failure=True)
        values = self.managed_maas() | {"operandHookWaitTimeoutSeconds": 480}
        by_kind = self.by_kind(self.render(values))
        for fragment in ("operator-configs", "dashboard-config", "mlflow-"):
            for job in self.family(by_kind, fragment)["Job"]:
                env = {e["name"]: e["value"] for e in
                       job["spec"]["template"]["spec"]["containers"][0]["env"]}
                if "WAIT_TIMEOUT" in env:
                    self.assertEqual(env["WAIT_TIMEOUT"], "480", fragment)
                    self.assertEqual(job["spec"]["activeDeadlineSeconds"], 510,
                                     fragment)


class OperandSetScriptTests(_OperandScriptFixture):
    """Happy-path smoke for the rendered set-chart-created-cr script (as
    mounted from each family's ConfigMap) against a fake oc binary: the
    fresh-install create path (object absent after the CRD wait) under the
    chart-created proof payload."""

    def run_set(self, absent=True):
        return self.run_script("set-chart-created-cr.sh", {
            "CRD_NAME": "odhdashboardconfigs.opendatahub.io",
            "RESOURCE": "odhdashboardconfigs.opendatahub.io/odh-dashboard-config",
            "OBJECT_NAME": "odh-dashboard-config",
            "OBJECT_NAMESPACE": "redhat-ods-applications",
            "CREATE_PAYLOAD": os.path.join(self.work, "create.json"),
            "SPEC_PAYLOAD": os.path.join(self.work, "spec.json"),
        }, absent=absent)

    def test_dashboard_create_path_smoke(self):
        self.FAMILY_FRAGMENT = "dashboard-config"
        self.stage_cm()
        result = self.run_set()
        self.assertEqual(result.returncode, 0, result.stderr)
        created = self.captured_create()
        self.assertEqual(created["kind"], "OdhDashboardConfig")
        self.assertEqual(created["metadata"]["annotations"],
                         {"openshift-ai/chart-created": "true"})
        self.assertIn("notebookController", created["spec"])
        # The create path neither patches nor deletes.
        self.assertNotIn(" patch ", self.log())
        self.assertNotIn(" delete ", self.log())

    def test_mlflow_create_path_smoke(self):
        # The same script delivered by the mlflow family's ConfigMap proves
        # both deliveries against their family-specific payloads.
        self.FAMILY_FRAGMENT = "mlflow-"
        self.stage_cm()
        result = self.run_script("set-chart-created-cr.sh", {
            "CRD_NAME": "mlflows.mlflow.opendatahub.io",
            "RESOURCE": "mlflows.mlflow.opendatahub.io/mlflow",
            "OBJECT_NAME": "mlflow",
            "OBJECT_NAMESPACE": "redhat-ods-applications",
            "CREATE_PAYLOAD": os.path.join(self.work, "create.json"),
            "SPEC_PAYLOAD": os.path.join(self.work, "spec.json"),
        }, absent=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        created = self.captured_create()
        self.assertEqual(created["kind"], "MLflow")
        self.assertEqual(created["metadata"]["name"], "mlflow")
        self.assertIn("backendStoreUri", created["spec"])
        self.assertNotIn(" patch ", self.log())

    def test_converge_desired_subset(self):
        # The converge path compares only the DESIRED subset of the live spec:
        # a live object already carrying the desired values (plus extra fields
        # the dashboard backend writes) produces no patch, while a drifted
        # desired field still yields one merge patch carrying the live
        # resourceVersion.
        self.FAMILY_FRAGMENT = "dashboard-config"
        self.stage_cm()
        desired = {"notebookController": {"enabled": True,
                                          "notebookNamespace": "rhods-notebooks",
                                          "pvcSize": "20Gi"}}
        with open(self.object_file, "w") as f:
            json.dump({"metadata": {"name": "odh-dashboard-config",
                                    "namespace": "redhat-ods-applications",
                                    "resourceVersion": "44"},
                       "spec": desired | {"dashboardBackendOption":
                                          {"logLevel": "info"}}}, f)
        result = self.run_set(absent=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already matches", result.stdout)
        self.assertEqual(self.patches(), [])
        self.assertNotIn(" create ", self.log())

        with open(self.object_file, "w") as f:
            json.dump({"metadata": {"name": "odh-dashboard-config",
                                    "namespace": "redhat-ods-applications",
                                    "resourceVersion": "45"},
                       "spec": desired | {"notebookController":
                                          {"enabled": False,
                                           "notebookNamespace": "rhods-notebooks",
                                           "pvcSize": "20Gi"}}}, f)
        result = self.run_set(absent=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "45"},
            "spec": {"notebookController": desired["notebookController"]}})


class OperandDeleteScriptTests(_OperandScriptFixture):
    """Happy-path smoke for the rendered delete-chart-created-cr script: the
    proof-gated uninstall path (deletes only under the chart-created proof
    annotation)."""

    def test_proof_gated_delete_smoke(self):
        self.FAMILY_FRAGMENT = "dashboard-config"
        self.stage_cm()
        with open(self.object_file, "w") as f:
            json.dump({"metadata": {
                "name": "odh-dashboard-config",
                "namespace": "redhat-ods-applications",
                "resourceVersion": "43",
                "annotations": {"openshift-ai/chart-created": "true"}},
                "spec": {}}, f)
        result = self.run_script("delete-chart-created-cr.sh", {
            "RESOURCE": "odhdashboardconfigs.opendatahub.io/odh-dashboard-config",
            "OBJECT_NAME": "odh-dashboard-config",
            "OBJECT_NAMESPACE": "redhat-ods-applications",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        delete_lines = [l for l in self.log().splitlines()
                        if l.startswith("delete ")]
        self.assertEqual(len(delete_lines), 1)
        self.assertIn("odhdashboardconfigs.opendatahub.io/odh-dashboard-config",
                      delete_lines[0])
        self.assertIn("redhat-ods-applications", delete_lines[0])


class OperandConvergeScriptTests(_OperandScriptFixture):
    """Happy-path smoke for the rendered converge-operator-configs script:
    the three operator-created singletons present with drifted values
    converge to one merge patch each, carrying the live resourceVersion."""

    def test_converge_smoke(self):
        self.FAMILY_FRAGMENT = "operator-configs"
        self.stage_cm()
        with open(self.auth_file, "w") as f:
            json.dump({"metadata": {"name": "auth", "resourceVersion": "50",
                                    "annotations": {}},
                       "spec": {"adminGroups": ["rhods-admins"],
                                "allowedGroups": ["old-group"]}}, f)
        with open(self.dsci_file, "w") as f:
            json.dump({"metadata": {"name": "default-dsci",
                                    "resourceVersion": "52"},
                       "spec": {"monitoring": {"managementState": "Removed"}}}, f)
        with open(self.object_file, "w") as f:
            json.dump({"metadata": {"name": "odh-segment-key-config",
                                    "namespace": "redhat-ods-applications",
                                    "resourceVersion": "51",
                                    "annotations": {}},
                       "data": {"segmentKeyEnabled": "true"}}, f)
        result = self.run_script("converge-operator-configs.sh", {
            "AUTH_PATCH": os.path.join(self.work, "auth-patch.json"),
            "DSCI_PATCH": os.path.join(self.work, "dsci-patch.json"),
            "SEGMENT_PATCH": os.path.join(self.work, "segment-key-patch.json"),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 3)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "50"},
            "spec": {"adminGroups": ["rhods-admins"],
                     "allowedGroups": ["system:authenticated"]}})
        # The DSCI converge patches exactly the configured spec fields (the
        # whole desired monitoring block), leaving the rest of the live spec
        # untouched by comparison.
        self.assertEqual(patches[1], {
            "metadata": {"resourceVersion": "52"},
            "spec": {"monitoring": {
                "managementState": "Managed",
                "namespace": "redhat-ods-monitoring",
                "metrics": {"storage": {"size": "5Gi", "retention": "1d"}}}}})
        self.assertEqual(patches[2], {
            "metadata": {"resourceVersion": "51"},
            "data": {"segmentKeyEnabled": "false"}})
        # The converge neither creates nor deletes the operator-owned
        # objects.
        self.assertNotIn("create", self.log())
        self.assertNotIn("delete", self.log())
        # The resource target strings the script invokes, per oc call: the
        # fake oc serves whatever target it is asked, so a wrong resource
        # string smokes green unless the invocation surface itself is
        # asserted. Every DSCI invocation must carry the full
        # "<plural>.<group>/name" form — the API GROUP is singular
        # (dscinitialization), the plural RESOURCE dscinitializations, and
        # the bare "dscinitializations.<singular-group>" form is rejected by
        # the API. The Auth and segment-key invocations must each appear at
        # least once.
        dsci_calls = [line for line in self.log().splitlines()
                      if "dscinitializations" in line]
        self.assertTrue(dsci_calls)
        for line in dsci_calls:
            self.assertIn(
                "dscinitializations.dscinitialization.opendatahub.io/default-dsci",
                line, line)
        for target in ("auths.services.platform.opendatahub.io/auth",
                       "configmap/odh-segment-key-config"):
            self.assertTrue(any(target in line
                                for line in self.log().splitlines()), target)
        # Exactly one -n per namespaced oc invocation, always followed by the
        # segment-key namespace — the class of bug where a "-n <ns>" argument
        # pair leaks into conditional_write's namespace parameter makes every
        # get 404 on a namespace named "-n". The cluster-scoped singletons
        # carry no -n at all.
        namespaced = 0
        for line in self.log().splitlines():
            argv = line.split()
            if any(a.startswith(("auths", "dscinitializations")) for a in argv):
                self.assertNotIn("-n", argv, line)
            self.assertLessEqual(argv.count("-n"), 1, line)
            if "-n" in argv:
                namespaced += 1
                self.assertEqual(argv[argv.index("-n") + 1],
                                 "redhat-ods-applications", line)
        self.assertGreater(namespaced, 0)


if __name__ == "__main__":
    unittest.main()


class HookGuardTests(ChartFixture):
    """Every hook Job container carries the guard-without-pin
    securityContext: the four restricted-pod guards and no
    runAsUser/runAsGroup anywhere in the pod — restricted SCCs assign the
    namespace range UID to UID-less pods, and a pin would fight that
    platform assignment."""

    def all_hook_values(self):
        values = self.managed_maas()
        values["maasDb"] = {"create": True}
        values["maasTenantConfig"] = {
            "enabled": True, "namespace": "models-as-a-service",
            "waitTimeoutSeconds": 240, "pollIntervalSeconds": 5,
            "spec": {"telemetry": {"metrics": {"captureUser": False}}}}
        values["replicas"] = {"dashboard": 1, "operator": None,
                              "asCronJob": True,
                              "schedule": "*/15 * * * *",
                              "waitTimeoutSeconds": 240}
        values["evalhub"] = {"create": True, "tenant": {
            "create": True, "namespace": "evalhub-tenant",
            "waitTimeoutSeconds": 240}}
        return values

    def test_every_hook_job_is_guarded_without_pin(self):
        jobs = support.hook_jobs(self.render(self.all_hook_values()))
        names = [j["metadata"]["name"] for j in jobs]
        # The per-family counts lock the assertion to the full hook-Job
        # inventory; the replica and tenant surface names carry release/
        # namespace hashes, so families are matched by fragment.
        self.assertEqual(len(jobs), 10, names)
        for fragment, count in (("replicas", 2), ("maas-tenant-config", 1),
                                ("create-maas-db-config", 1),
                                ("update-secret-minio", 1),
                                ("dashboard-config", 2), ("mlflow-", 2),
                                ("operator-configs", 1)):
            self.assertEqual(
                sum(1 for n in names if fragment in n), count, names)
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])
