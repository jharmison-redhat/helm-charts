"""Tests for the bounded MaasTenantConfig configuration hook: one
representative render of the hook surface, one render-failure case, the
ConfigMap-glob no-leakage invariant, and the script smoke set against a
fake oc binary."""

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

    def test_hook_surface_renders(self):
        by_kind = self.by_kind(self.render(self.hook_values()))
        for kind in ("Job", "ServiceAccount", "ConfigMap", "ClusterRole",
                     "ClusterRoleBinding"):
            self.assertTrue(self.docs_named(by_kind, kind, self.FRAGMENT),
                            kind)
        # The hook Job and its delivery objects render in the release
        # namespace; the target product namespace appears only in the
        # Job's env/patch surface.
        job = self.docs_named(by_kind, "Job", self.FRAGMENT)[0]
        self.assertEqual(job["metadata"]["namespace"], "test-release-ns")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        env = {e["name"]: e["value"] for e in
               job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["MAAS_TENANT_NAMESPACE"], "models-as-a-service")
        # The ClusterRole is scoped to the MaasTenantConfig singleton.
        rules = self.docs_named(by_kind, "ClusterRole", self.FRAGMENT)[0][
            "rules"]
        self.assertEqual(rules, [{
            "apiGroups": ["maas.opendatahub.io"],
            "resources": ["maastenantconfigs"],
            "resourceNames": ["default-tenant"],
            "verbs": ["get", "patch"],
        }])
        # The payload parses and carries the configured spec.
        payload = json.loads(self.hook_cm(by_kind)["data"]["patch.json"])
        self.assertEqual(payload["spec"]["telemetry"]["metrics"]["captureUser"],
                         False)

    def test_invalid_spec_fails(self):
        self.render(self.hook_values(spec="scalar"), expect_failure=True)

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
        # The chart's fail-closed no-tool check: the scripts carry no dual
        # jq/python3 preamble (they use oc jsonpath directly), so the
        # fail-closed contract here is that a tool-less PATH fails loudly
        # during the readiness wait, before any mutation is attempted.
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
