"""Strict duplicate-key YAML parsing checks, the fail-closed maas-db
bootstrap Job render tests, and the maas-db script smoke set."""

import os
import subprocess
import unittest

import yaml

from oa_support import ChartFixture, HookScriptTests

DB_URI = "postgresql://maas:secret@pg-app:5432/maas\n"


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
