"""Tests for the bounded MaasTenantConfig configuration hook: render
gating, RBAC scoping, namespace placement, and the script smoke set
against a fake oc binary."""

import json
import os
import shutil
import subprocess
import tempfile
import textwrap

from oa_support import ChartFixture


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
