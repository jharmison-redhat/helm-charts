"""The fail-closed maas-db bootstrap Job render test and the maas-db
script smoke set."""

import os

from oa_support import ChartFixture, HookScriptTests

DB_URI = "postgresql://maas:secret@pg-app:5432/maas\n"


class MaasDbJobRenderTests(ChartFixture):
    """One representative render of the maas-db bootstrap Job surface."""

    def db_values(self, **overrides):
        values = self.managed_maas()
        db = {"create": True, "namespace": "redhat-ai-gateway-infra",
              "waitTimeoutSeconds": 240}
        db.update(overrides)
        values["maasDb"] = db
        return values

    def test_job_renders(self):
        by_kind = self.by_kind(self.render(self.db_values()))
        job = self.pick(by_kind, "Job", "create-maas-db-config")
        annotations = job["metadata"]["annotations"]
        self.assertEqual(annotations["helm.sh/hook"], "post-install,post-upgrade")
        self.assertEqual(annotations["helm.sh/hook-delete-policy"],
                         "before-hook-creation")
        self.assertEqual(job["metadata"]["namespace"],
                         "redhat-ai-gateway-infra")
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 270)
        env = {e["name"]: e["value"] for e in
               job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["DB_SOURCE_SECRET"],
                         "test-release-postgres-cluster-app")
        self.assertEqual(env["DB_NAMESPACE"], "redhat-ai-gateway-infra")


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
        script = self.stage_rendered_script(
            values, "create-maas-db-config-script", "create-maas-db-config.sh")
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
