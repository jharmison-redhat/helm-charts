"""Focused tests for the keycloak-auth chart. The module covers the
default-off kubeadmin-removal surface (no removal Job, ServiceAccount,
Role, or RoleBinding unless explicitly opted in), the exact named-secret
delete-only RBAC and rendered removal command, and the umbrella
example's explicit safe override, dependency pin, and metadata
coherence.

Render tests run the chart through ``helm template`` (keycloak-auth has
no chart dependencies, so no stubs or registry access are needed).
Command tests execute the rendered Job argv against a fake ``oc``
binary with an isolated environment; the fake is invoked by direct path
substitution of the rendered argv, so no run can fall through to a real
``oc`` or contact any cluster.
"""

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "keycloak-auth"))
UMBRELLA = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "examples", "maas-umbrella"))

FAKE_OC = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
env | LC_ALL=C sort > "$OC_ENV"
case "$OC_SCENARIO" in
  success)
    exit 0 ;;
  absent)
    # Simulates a NotFound response from the API server: with
    # --ignore-not-found the client treats it as a successful delete.
    if [[ " $* " == *" --ignore-not-found "* ]]; then
      exit 0
    fi
    echo 'Error from server (NotFound): secrets "kubeadmin" not found' >&2
    exit 1 ;;
  forbidden)
    echo 'Error from server (Forbidden): secrets "kubeadmin" is forbidden' >&2
    exit 1 ;;
  transport)
    echo 'unable to connect to the server: dial tcp: connection refused' >&2
    exit 1 ;;
  *)
    echo 'Error: deletion rejected' >&2
    exit 1 ;;
esac
"""


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


class ChartFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ka-test-")
        shutil.copytree(CHART, self.dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("charts", "Chart.lock"))
        self.addCleanup(shutil.rmtree, self.dir)

    def values_file(self, values):
        fd, path = tempfile.mkstemp(prefix="ka-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(values, f)
        self.addCleanup(os.unlink, path)
        return path

    def render(self, values=None):
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", "test-release-ns"]
        if values is not None:
            cmd += ["--values", self.values_file(values)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [d for d in docs(result.stdout) if d is not None]

    @staticmethod
    def by_kind(docs):
        by_kind = {}
        for d in docs:
            by_kind.setdefault(d["kind"], []).append(d)
        return by_kind

    @staticmethod
    def pick(by_kind, kind, name):
        matches = [d for d in by_kind[kind] if d["metadata"]["name"] == name]
        assert len(matches) == 1, (kind, name, by_kind[kind])
        return matches[0]

    def opt_in_render(self):
        return self.by_kind(self.render({"removeKubeAdmin": True}))


class RemovalGateTests(ChartFixture):
    """Rendering gates: the removal surface is opt-in only, and unrelated
    resources render unchanged in every mode."""

    def test_default_renders_no_removal_resources(self):
        by_kind = self.by_kind(self.render())
        for kind in ("Job", "ServiceAccount", "Role", "RoleBinding"):
            self.assertNotIn(kind, by_kind)
        self.assertIn("Group", by_kind)
        self.assertIn("ClusterRoleBinding", by_kind)

    def test_explicit_false_renders_no_removal_resources(self):
        by_kind = self.by_kind(self.render({"removeKubeAdmin": False}))
        for kind in ("Job", "ServiceAccount", "Role", "RoleBinding"):
            self.assertNotIn(kind, by_kind)
        self.assertIn("Group", by_kind)
        self.assertIn("ClusterRoleBinding", by_kind)

    def test_opt_in_renders_removal_resources(self):
        by_kind = self.opt_in_render()
        for kind in ("Job", "ServiceAccount", "Role", "RoleBinding"):
            self.assertIn(kind, by_kind)
        for kind in ("Job", "ServiceAccount", "Role", "RoleBinding"):
            doc = by_kind[kind][0]
            self.assertEqual(doc["metadata"]["name"], "remove-kubeadmin")
            self.assertEqual(doc["metadata"]["namespace"], "kube-system")

    def test_unrelated_admin_binding_unchanged(self):
        # The cluster-admins Group/ClusterRoleBinding is independent of the
        # removal opt-in and keeps its Group subject in both modes.
        for by_kind in (self.by_kind(self.render()), self.opt_in_render()):
            binding = self.pick(by_kind, "ClusterRoleBinding", "cluster-admins")
            self.assertEqual(binding["roleRef"]["name"], "cluster-admin")
            self.assertEqual(binding["subjects"], [{
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Group",
                "name": "cluster-admins",
            }])

    @staticmethod
    def configured_values(remove):
        return {
            "global": {"oidc": {"secret": "test-client-secret"}},
            "ingressCA": "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----",
            "removeKubeAdmin": remove,
        }

    def test_configured_oauth_secret_and_ca_invariant_across_toggle(self):
        # With the OIDC secret and ingressCA configured, the OAuth identity
        # provider, its client Secret, and the router-ca trust ConfigMap all
        # render, and every non-removal document is byte-identical across
        # the removal toggle states.
        off = self.by_kind(self.render(self.configured_values(False)))
        on = self.by_kind(self.render(self.configured_values(True)))
        for kind, name in (("OAuth", "cluster"),
                           ("Secret", "openid-client-secret"),
                           ("ConfigMap", "router-ca"),
                           ("Group", "cluster-admins"),
                           ("ClusterRoleBinding", "cluster-admins")):
            for by_kind in (off, on):
                self.assertIn(kind, by_kind)
            self.assertEqual(self.pick(off, kind, name), self.pick(on, kind, name))
        secret = self.pick(off, "Secret", "openid-client-secret")
        self.assertEqual(secret["data"]["clientSecret"],
                         base64.b64encode(b"test-client-secret").decode())
        oauth = self.pick(off, "OAuth", "cluster")
        idp = oauth["spec"]["identityProviders"][0]
        self.assertEqual(idp["name"], "rhbk")
        self.assertEqual(idp["openID"]["clientSecret"]["name"], "openid-client-secret")
        self.assertEqual(idp["openID"]["ca"]["name"], "router-ca")
        self.assertEqual(self.pick(off, "ConfigMap", "router-ca")["data"]["ca.crt"],
                         self.configured_values(False)["ingressCA"])

    def test_job_identity_and_hooks(self):
        job = self.pick(self.opt_in_render(), "Job", "remove-kubeadmin")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(job["spec"]["backoffLimit"], 4)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "remove-kubeadmin")
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(container["name"], "remove-kubeadmin")
        self.assertEqual(
            container["image"],
            "image-registry.openshift-image-registry.svc:5000/openshift/tools:latest")
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")

    def test_service_account_gates_with_job(self):
        sa = self.pick(self.opt_in_render(), "ServiceAccount", "remove-kubeadmin")
        self.assertEqual(sa["metadata"]["namespace"], "kube-system")

    def test_role_is_delete_only_named_secret(self):
        role = self.pick(self.opt_in_render(), "Role", "remove-kubeadmin")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["kubeadmin"],
            "verbs": ["delete"],
        }])

    def test_role_binding_binds_only_the_removal_service_account(self):
        binding = self.pick(self.opt_in_render(), "RoleBinding", "remove-kubeadmin")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "remove-kubeadmin",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "remove-kubeadmin",
            "namespace": "kube-system",
        }])

    def test_no_edit_or_cluster_admin_grant_on_opt_in(self):
        by_kind = self.opt_in_render()
        for binding in by_kind["RoleBinding"]:
            self.assertEqual(binding["roleRef"]["kind"], "Role")
            self.assertEqual(binding["roleRef"]["name"], "remove-kubeadmin")
        for role in by_kind["Role"]:
            self.assertEqual(role["rules"][0]["verbs"], ["delete"])

    def test_rendered_command_is_direct_oc_argv(self):
        container = self.pick(
            self.opt_in_render(), "Job", "remove-kubeadmin"
        )["spec"]["template"]["spec"]["containers"][0]
        # No shell layer, no xtrace, no error suppression.
        self.assertEqual(container["command"], ["oc"])
        self.assertEqual(container["args"], [
            "delete", "secret", "kubeadmin",
            "--namespace", "kube-system",
            "--ignore-not-found", "--wait=false",
        ])


class FakeOcCommandTests(ChartFixture):
    """Executes the rendered removal command against a fake oc binary.

    The fake oc is invoked by direct path substitution (fake binary path +
    the rendered argument vector), never through PATH lookup, so the run
    cannot fall through to a real oc. The child environment is an explicit
    minimal set — no parent variables are inherited — with an isolated
    HOME/KUBECONFIG, so no kubeconfig or cluster credential from the test
    host can reach the command."""

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="ka-script-")
        self.addCleanup(shutil.rmtree, self.work)
        container = self.pick(
            self.opt_in_render(), "Job", "remove-kubeadmin"
        )["spec"]["template"]["spec"]["containers"][0]
        self.argv = container["command"] + container["args"]

    def run_command(self, scenario):
        fake = os.path.join(self.work, "oc")
        with open(fake, "w") as f:
            f.write(FAKE_OC)
        os.chmod(fake, 0o755)
        # Substitute the rendered binary name with the fake: argv[0] "oc"
        # becomes the fake's path, the rendered arguments are untouched.
        # The env dict replaces the parent environment entirely; the
        # sentinel below only exists in the parent and must never appear
        # in the fake's recorded environment.
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": self.work,
            "KUBECONFIG": os.path.join(self.work, "kubeconfig"),
            "OC_SCENARIO": scenario,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_ENV": os.path.join(self.work, "env.log"),
        }
        with mock.patch.dict(os.environ, {"KA_PARENT_SENTINEL": "must-not-leak"}):
            return subprocess.run(
                [fake] + self.argv[1:], capture_output=True, text=True,
                env=env, timeout=30)

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def child_env(self):
        with open(os.path.join(self.work, "env.log")) as f:
            return dict(
                line.split("=", 1) for line in f.read().splitlines() if "=" in line)

    def test_child_environment_is_minimal_and_isolated(self):
        result = self.run_command("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        child = self.child_env()
        # The parent-only sentinel proves no parent variable leaked through.
        self.assertNotIn("KA_PARENT_SENTINEL", child)
        self.assertEqual(child["HOME"], self.work)
        self.assertEqual(child["KUBECONFIG"], os.path.join(self.work, "kubeconfig"))
        self.assertEqual(child["PATH"], "/usr/bin:/bin")
        self.assertEqual(child["OC_SCENARIO"], "success")
        self.assertEqual(
            {k for k in child if k.startswith("OC_")},
            {"OC_SCENARIO", "OC_LOG", "OC_ENV"})

    def test_success(self):
        result = self.run_command("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("delete secret kubeadmin", self.log())

    def test_absent_secret_is_successful_delete(self):
        # The API's NotFound response combined with --ignore-not-found is a
        # successful, idempotent deletion.
        result = self.run_command("absent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ignore-not-found", self.log())

    def test_forbidden_fails(self):
        result = self.run_command("forbidden")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)

    def test_transport_failure_fails(self):
        result = self.run_command("transport")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("connection refused", result.stderr)

    def test_other_failure_fails(self):
        result = self.run_command("other")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deletion rejected", result.stderr)

    def test_rendered_argv_is_delete_only(self):
        result = self.run_command("success")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.log().splitlines()
        self.assertEqual(len(argv), 1)
        argv = argv[0].split()
        self.assertEqual(argv[:3], ["delete", "secret", "kubeadmin"])
        self.assertEqual(argv[argv.index("--namespace") + 1], "kube-system")
        self.assertIn("--ignore-not-found", argv)
        self.assertIn("--wait=false", argv)
        for verb in ("get", "list", "watch"):
            self.assertNotIn(verb, argv)


class UmbrellaMetadataTests(unittest.TestCase):
    """Parses the umbrella example's values and metadata: its explicit
    safe override and the pin/version coherence with the edited chart,
    without fetching any published package."""

    @staticmethod
    def load(path):
        with open(path) as f:
            return yaml.safe_load(f)

    def test_chart_default_is_safe(self):
        self.assertIs(self.load(os.path.join(CHART, "values.yaml"))["removeKubeAdmin"],
                      False)

    def test_umbrella_explicit_value_is_safe(self):
        umbrella_values = self.load(os.path.join(UMBRELLA, "values.yaml"))
        self.assertIs(umbrella_values["keycloak-auth"]["removeKubeAdmin"], False)

    def test_auth_chart_version_bump(self):
        meta = self.load(os.path.join(CHART, "Chart.yaml"))
        self.assertEqual(meta["version"], "0.3.0")
        self.assertEqual(meta["appVersion"], "0.3.0")

    def test_umbrella_version_and_pin_coherence(self):
        umbrella = self.load(os.path.join(UMBRELLA, "Chart.yaml"))
        self.assertEqual(umbrella["version"], "0.5.1")
        self.assertNotIn("appVersion", umbrella)
        deps = {d["name"]: d["version"] for d in umbrella["dependencies"]}
        self.assertEqual(deps["keycloak-auth"], ">=0.3.0 <0.4.0")
        # The pin's range must contain the chart's new version.
        chart_version = tuple(
            int(p) for p in self.load(os.path.join(CHART, "Chart.yaml"))["version"].split("."))
        self.assertGreaterEqual(chart_version, (0, 3, 0))
        self.assertLess(chart_version, (0, 4, 0))


class CommandContractTests(unittest.TestCase):
    """Static source checks for the rendered command contract: the exact
    flags come from the template, and the template uses no shell layer."""

    def test_template_has_no_shell_or_suppression(self):
        with open(os.path.join(CHART, "templates", "job-remove-kubeadmin.yaml")) as f:
            template = f.read()
        self.assertNotIn("/bin/bash", template)
        self.assertNotIn("-xc", template)
        self.assertNotIn("||:", template)
        self.assertIn("--ignore-not-found", template)
        self.assertIn("--wait=false", template)


if __name__ == "__main__":
    unittest.main()
