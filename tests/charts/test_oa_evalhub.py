"""Render tests for the openshift-ai update-secret-minio EvalHub
credential-update hook Job plus its script smoke set against a fake oc
binary."""

import os

from oa_support import ChartFixture, HookScriptTests

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
