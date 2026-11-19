"""Render tests for the openshift-ai update-secret-minio EvalHub
credential-update hook Job plus its script smoke set against a fake oc
binary."""

import os

from oa_support import ChartFixture, HookScriptTests

EH_ACCESS = 'AKIA"quoted\\backslash'
EH_SECRET = "s3cr3t-key\nwith trailing newline"


class EvalHubJobRenderTests(ChartFixture):
    """One representative render of the update-secret-minio hook surface:
    the hook Job (with its hook annotations and placement) plus its
    supporting resources as ordinary release manifests."""

    def test_hook_surface_renders(self):
        by_kind = self.by_kind(self.render(self.evalhub_values()))
        job = self.pick(by_kind, "Job", "update-secret-minio")
        annotations = job["metadata"]["annotations"]
        self.assertEqual(annotations["helm.sh/hook"],
                         "post-install,post-upgrade")
        self.assertEqual(annotations["helm.sh/hook-delete-policy"],
                         "before-hook-creation")
        self.assertEqual(job["metadata"]["namespace"], "evalhub-tenant")
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 270)
        self.assertEqual(
            job["spec"]["template"]["spec"]["serviceAccountName"],
            "patch-secret")
        # The script ConfigMap is an ordinary release resource in the same
        # namespace; the supporting resources carry no hook annotations.
        cm = self.pick(by_kind, "ConfigMap", "update-secret-minio-script")
        self.assertEqual(cm["metadata"]["namespace"], "evalhub-tenant")
        self.assertIn("update-secret-minio.sh", cm["data"])
        self.assertNotIn("helm.sh/hook", cm["metadata"].get("annotations") or {})
        for kind in ("ServiceAccount", "Role", "RoleBinding", "EvalHub"):
            self.assertTrue(self.docs_named(by_kind, kind, ""), kind)
            for doc in by_kind[kind]:
                self.assertNotIn(
                    "helm.sh/hook",
                    doc["metadata"].get("annotations") or {})


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
        script = self.stage_rendered_script(
            self.evalhub_values(), "update-secret-minio-script",
            "update-secret-minio.sh")
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
