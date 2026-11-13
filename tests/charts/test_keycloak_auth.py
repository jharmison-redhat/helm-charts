"""Focused tests for the keycloak-auth chart. The module covers the
default-off kubeadmin-removal surface (no removal Job, ServiceAccount,
Role, or RoleBinding unless explicitly opted in), the exact named-secret
delete-only RBAC and rendered removal command, the OAuth identity-provider
hook surface (the platform OAuth object is never owned — hook Jobs upsert
and remove only this chart's named entry with a resourceVersion-conditional
read-modify-write), the console logout-redirect surface (hook Jobs set and
unset spec.authentication.logoutRedirect on the platform Console object with
the same conditional-write discipline, gated on logoutRedirect.enabled and
global.oidc.secret), the umbrella example's explicit safe override,
dependency pin, and metadata coherence.

Render tests run the chart through ``helm template`` (keycloak-auth has
no chart dependencies, so no stubs or registry access are needed).
Command tests execute the rendered Job argv against a fake ``oc``
binary with an isolated environment; the fake is invoked by direct path
substitution of the rendered argv, so no run can fall through to a real
``oc`` or contact any cluster. Script tests run the rendered hook scripts
against a fake ``oc`` on a restricted PATH built entirely from symlinks,
so neither a real ``oc`` nor the test host's ambient tools can leak into
the run.
"""

import base64
import json
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

# Fake oc for the OAuth hook scripts: serves a canned OAuth object for get
# and records every --patch= payload, with per-scenario API behavior. Each
# get bumps the served metadata.resourceVersion (first get serves the canned
# value, each later get serves it incremented), so a retry can only patch
# successfully with a version from a fresh read. Every invocation is logged
# so tests can assert the exact oc call surface.
FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
env | LC_ALL=C sort > "$OC_ENV"
if [[ "$1" == "get" ]]; then
  case "$OC_SCENARIO" in
    absent-object)
      echo 'Error from server (NotFound): oauths.config.openshift.io "cluster" not found' >&2
      exit 1 ;;
    get-error)
      echo 'Error from server (Forbidden): oauths.config.openshift.io "cluster" is forbidden' >&2
      exit 1 ;;
    *)
      rv_file="$OC_GET_FILE.rv"
      if [[ -f "$rv_file" ]]; then
        next_rv=$(($(cat "$rv_file") + 1))
      else
        next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$OC_GET_FILE" | grep -oE '[0-9]+')
      fi
      echo "$next_rv" > "$rv_file"
      sed "s/\\"resourceVersion\\": *\\"[0-9]*\\"/\\"resourceVersion\\": \\"$next_rv\\"/" "$OC_GET_FILE" ;;
  esac
elif [[ "$1" == "patch" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == --patch=* ]]; then
      printf '%s\\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
    fi
  done
  attempts=$(cat "$OC_ATTEMPTS" 2>/dev/null || true)
  attempts=$(( ${attempts:-0} + 1 ))
  echo "$attempts" > "$OC_ATTEMPTS"
  case "$OC_SCENARIO" in
    conflict-then-success)
      if [ "$attempts" -ge 2 ]; then
        exit 0
      fi
      echo 'Error from server (Conflict): Operation cannot be fulfilled on oauths.config.openshift.io "cluster"' >&2
      exit 1 ;;
    conflict-exhausted)
      echo 'Error from server (Conflict): Operation cannot be fulfilled on oauths.config.openshift.io "cluster"' >&2
      exit 1 ;;
    patch-error)
      echo 'Error from server (Forbidden): oauths.config.openshift.io "cluster" is forbidden' >&2
      exit 1 ;;
    *)
      exit 0 ;;
  esac
else
  echo "unexpected oc invocation: $*" >&2
  exit 64
fi
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

    def render_raw(self, values=None):
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", "test-release-ns"]
        if values is not None:
            cmd += ["--values", self.values_file(values)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def render(self, values=None):
        return [d for d in docs(self.render_raw(values)) if d is not None]

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

    @staticmethod
    def configured_values(remove=False, ingressCA=None):
        values = {
            "global": {"oidc": {"secret": "test-client-secret"}},
            "removeKubeAdmin": remove,
        }
        if ingressCA is not None:
            values["ingressCA"] = ingressCA
        return values

    def configured_render(self, ingressCA=None):
        return self.by_kind(self.render(self.configured_values(ingressCA=ingressCA)))

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
        # The keycloak-auth-cluster-admins ClusterRoleBinding is independent of
        # the removal opt-in and keeps its cluster-admins Group subject in both
        # modes. The binding is chart-prefixed so it cannot collide with the
        # OpenShift platform's own bootstrap `cluster-admins` binding; the
        # Group it targets is deliberately unprefixed (OAuth group-mapping
        # target).
        for by_kind in (self.by_kind(self.render()), self.opt_in_render()):
            binding = self.pick(by_kind, "ClusterRoleBinding",
                                "keycloak-auth-cluster-admins")
            self.assertEqual(binding["roleRef"]["name"], "cluster-admin")
            self.assertEqual(binding["subjects"], [{
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Group",
                "name": "cluster-admins",
            }])

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


class OAuthNeverOwnedTests(ChartFixture):
    """The platform OAuth object always exists on OpenShift, so the chart
    must never render it as a release-owned manifest — configured or not."""

    def test_oauth_resource_never_renders(self):
        for by_kind in (self.by_kind(self.render()),
                        self.configured_render(),
                        self.configured_render(ingressCA="CA"),
                        self.by_kind(self.render(
                            self.configured_values(remove=True, ingressCA="CA")))):
            self.assertNotIn("OAuth", by_kind)

    def test_oidc_not_configured_renders_no_oauth_surface(self):
        by_kind = self.by_kind(self.render())
        for kind in ("Job", "ServiceAccount", "ClusterRole", "ConfigMap"):
            self.assertNotIn(kind, by_kind)
        names = {d["metadata"]["name"] for docs in by_kind.values() for d in docs}
        for name in ("oauth-idp-upsert", "oauth-idp-remove",
                     "oauth-idp", "oauth-idp-config"):
            self.assertNotIn(name, names)


class OAuthHookSurfaceTests(ChartFixture):
    """With the OIDC secret configured, the hook Jobs, script ConfigMap,
    ServiceAccount, and minimal ClusterRole surface render with exact
    hooks and RBAC."""

    def test_upsert_job_identity_and_hooks(self):
        job = self.pick(self.configured_render(), "Job", "oauth-idp-upsert")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(job["spec"]["backoffLimit"], 4)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "oauth-idp")
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(
            container["image"],
            "image-registry.openshift-image-registry.svc:5000/openshift/tools:latest")
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(container["command"], ["/mnt/oauth-idp-upsert.sh"])
        self.assertEqual(container["env"],
                         [{"name": "IDP_NAME", "value": "rhbk"}])
        self.assertEqual(container["volumeMounts"], [{
            "name": "oauth-idp-config", "mountPath": "/mnt",
        }])
        self.assertEqual(pod["volumes"], [{
            "name": "oauth-idp-config",
            "configMap": {"name": "oauth-idp-config", "defaultMode": 493},
        }])

    def test_remove_job_is_pre_delete_hook(self):
        job = self.pick(self.configured_render(), "Job", "oauth-idp-remove")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"], "pre-delete")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(
            job["spec"]["template"]["spec"]["serviceAccountName"], "oauth-idp")
        container = job["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["/mnt/oauth-idp-remove.sh"])
        self.assertEqual(container["env"],
                         [{"name": "IDP_NAME", "value": "rhbk"}])

    def test_script_configmap_delivers_scripts_and_entry(self):
        cm = self.pick(self.configured_render(), "ConfigMap", "oauth-idp-config")
        data = cm["data"]
        for key in ("oauth-idp-upsert.sh", "oauth-idp-remove.sh", "idp-entry.json"):
            self.assertIn(key, data)
        self.assertIn("set -euo pipefail", data["oauth-idp-upsert.sh"])
        self.assertIn("set -euo pipefail", data["oauth-idp-remove.sh"])
        self.assertIn("oauths.config.openshift.io/cluster",
                      data["oauth-idp-upsert.sh"])
        self.assertIn("oauths.config.openshift.io/cluster",
                      data["oauth-idp-remove.sh"])
        entry = json.loads(data["idp-entry.json"])
        self.assertEqual(entry["name"], "rhbk")

    def test_service_account_gates_with_jobs(self):
        # No explicit namespace: the SA, script ConfigMap, and hook Jobs all
        # land in the release namespace, which only the ClusterRoleBinding
        # subject needs to name explicitly.
        sa = self.pick(self.configured_render(), "ServiceAccount", "oauth-idp")
        self.assertNotIn("namespace", sa["metadata"])
        for job_name in ("oauth-idp-upsert", "oauth-idp-remove"):
            job = self.pick(self.configured_render(), "Job", job_name)
            self.assertNotIn("namespace", job["metadata"])

    def test_cluster_role_is_get_and_patch_on_named_oauth_only(self):
        role = self.pick(self.configured_render(), "ClusterRole", "oauth-idp")
        self.assertEqual(role["rules"], [{
            "apiGroups": ["config.openshift.io"],
            "resources": ["oauths"],
            "resourceNames": ["cluster"],
            "verbs": ["get", "patch"],
        }])

    def test_cluster_role_binding_binds_only_the_oauth_service_account(self):
        binding = self.pick(self.configured_render(),
                            "ClusterRoleBinding", "oauth-idp")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "oauth-idp",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "oauth-idp",
            "namespace": "test-release-ns",
        }])

    def test_client_secret_and_router_ca_render_with_oauth_surface(self):
        off = self.configured_render()
        on = self.configured_render(ingressCA="CA")
        for by_kind in (off, on):
            secret = self.pick(by_kind, "Secret", "openid-client-secret")
            self.assertEqual(secret["data"]["clientSecret"],
                             base64.b64encode(b"test-client-secret").decode())
        self.assertEqual(
            self.pick(on, "ConfigMap", "router-ca")["data"]["ca.crt"], "CA")
        self.assertNotIn("router-ca", {d["metadata"]["name"]
                                       for d in off.get("ConfigMap", [])})


OLD_IDP_ENTRY = {
    "name": "rhbk",
    "type": "OpenID",
    "mappingMethod": "claim",
    "openID": {
        "claims": {
            "email": ["email"],
            "name": ["name"],
            "preferredUsername": ["preferred_username", "email", "name"],
            "groups": ["groups"],
        },
        "clientID": "ocp-idp",
        "clientSecret": {"name": "openid-client-secret"},
        "extraScopes": [],
        "issuer": "https://keycloak.apps.cluster.example.com/realms/sso",
    },
}


class IdpEntryShapeTests(ChartFixture):
    """The identity provider entry the hook Jobs upsert preserves the exact
    shape the release-owned OAuth manifest used to render."""

    def idp_entry(self, ingressCA=None):
        cm = self.pick(self.configured_render(ingressCA=ingressCA),
                       "ConfigMap", "oauth-idp-config")
        return json.loads(cm["data"]["idp-entry.json"])

    def test_entry_matches_old_oauth_manifest_shape(self):
        self.assertEqual(self.idp_entry(), OLD_IDP_ENTRY)

    def test_entry_with_ingress_ca_adds_ca_reference_only(self):
        entry = self.idp_entry(ingressCA="CA")
        with_ca = json.loads(json.dumps(OLD_IDP_ENTRY))
        with_ca["openID"]["ca"] = {"name": "router-ca"}
        self.assertEqual(entry, with_ca)

    def test_entry_follows_non_default_values(self):
        values = {
            "global": {
                "wildcardDomain": "apps.other.example.com",
                "oidc": {
                    "realm": "other-realm",
                    "clientId": "other-client",
                    "secret": "test-client-secret",
                },
            },
            "keycloak": {"name": "other-keycloak"},
            "oauth": {"idpName": "other-idp"},
        }
        cm = self.pick(self.by_kind(self.render(values)),
                       "ConfigMap", "oauth-idp-config")
        entry = json.loads(cm["data"]["idp-entry.json"])
        expected = json.loads(json.dumps(OLD_IDP_ENTRY))
        expected["name"] = "other-idp"
        expected["openID"]["clientID"] = "other-client"
        expected["openID"]["issuer"] = (
            "https://other-keycloak.apps.other.example.com/realms/other-realm")
        self.assertEqual(entry, expected)
        self.assertEqual(entry["openID"]["clientSecret"],
                         {"name": "openid-client-secret"})

    def test_entry_carries_no_secret_material(self):
        for entry in (self.idp_entry(), self.idp_entry(ingressCA="CA")):
            self.assertNotIn("test-client-secret", json.dumps(entry))


class PayloadHygieneTests(ChartFixture):
    """No credential may ever appear in a rendered manifest outside the
    client Secret resource itself."""

    def test_secret_value_appears_nowhere_outside_the_secret(self):
        secret_b64 = base64.b64encode(b"test-client-secret").decode()
        for values in (self.configured_values(),
                       self.configured_values(remove=True, ingressCA="CA")):
            raw = self.render_raw(values)
            self.assertNotIn("test-client-secret", raw)
            self.assertEqual(raw.count(secret_b64), 1,
                             "the secret's encoded value must appear exactly "
                             "once, in the openid-client-secret Secret")


class FakeOcScriptFixture(ChartFixture):
    """Shared plumbing for executing rendered hook scripts against a fake
    oc binary. Each run gets a restricted PATH built solely from symlinks
    in an isolated bin directory (the JSON tool chosen per test path, core
    utilities, and the fake oc), so no ambient host tool or real oc can be
    reached. The child environment is an explicit minimal set — no parent
    variables are inherited."""
    MAX_ATTEMPTS = 5

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="ka-script-")
        self.addCleanup(shutil.rmtree, self.work)

    def build_bin_dir(self, include_jq=None, include_python=None):
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for name in os.listdir(bin_dir):
            os.unlink(os.path.join(bin_dir, name))
        if include_jq is None:
            include_jq = shutil.which("jq") is not None
        if include_python is None:
            include_python = shutil.which("python3") is not None
        tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env", "sort"]
        if include_jq:
            self.assertTrue(shutil.which("jq"),
                            "jq requested but not available on the host")
            tools.append("jq")
        if include_python:
            self.assertTrue(shutil.which("python3"),
                            "python3 requested but not available on the host")
            tools.append("python3")
        for tool in tools:
            os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
        fake_oc = os.path.join(bin_dir, "oc")
        with open(fake_oc, "w") as f:
            f.write(FAKE_OC_SCRIPT)
        os.chmod(fake_oc, 0o755)
        return bin_dir

    def tool_path_variants(self):
        """Both JSON tool paths the hook scripts can take; the jq path is
        included only when the host provides jq."""
        variants = [{"include_jq": False, "include_python": True}]
        if shutil.which("jq"):
            variants.append({"include_jq": True, "include_python": False})
        return variants

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def patches(self):
        path = os.path.join(self.work, "patches.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f.read().splitlines() if line]

    def child_env(self):
        with open(os.path.join(self.work, "env.log")) as f:
            return dict(
                line.split("=", 1) for line in f.read().splitlines() if "=" in line)

    def run_child(self, script_path, env):
        with mock.patch.dict(os.environ, {"KA_PARENT_SENTINEL": "must-not-leak"}):
            return subprocess.run(
                ["/bin/bash", script_path], capture_output=True, text=True,
                env=env, timeout=120)

    def canned_doc(self):
        """Canned object the fake oc serves for get."""
        raise NotImplementedError

    def script_env(self):
        """Capability-specific entries merged into the child environment."""
        raise NotImplementedError

    def run_script(self, script_name, scenario, canned_doc=None,
                   include_jq=None, include_python=None):
        bin_dir = self.build_bin_dir(include_jq=include_jq,
                                     include_python=include_python)
        script_path = os.path.join(self.work, script_name)
        with open(script_path, "w") as f:
            f.write(self.scripts[script_name])
        if canned_doc is None:
            canned_doc = self.canned_doc()
        get_file = os.path.join(self.work, self.get_file_name)
        with open(get_file, "w") as f:
            json.dump(canned_doc, f)
        # Each run is self-contained: drop logs and the fake oc's
        # resourceVersion counter left by any earlier run.
        for stale in ("commands.log", "env.log", "patches.log", "attempts",
                      get_file + ".rv"):
            stale_path = os.path.join(self.work, stale)
            if os.path.exists(stale_path):
                os.unlink(stale_path)
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            **self.script_env(),
            "OC_SCENARIO": scenario,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_ENV": os.path.join(self.work, "env.log"),
            "OC_GET_FILE": get_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
            "OC_ATTEMPTS": os.path.join(self.work, "attempts"),
        }
        return self.run_child(script_path, env)


class OAuthIdpScriptTests(FakeOcScriptFixture):
    """Executes the rendered OAuth hook scripts against a fake oc binary."""

    get_file_name = "oauth.json"

    def setUp(self):
        super().setUp()
        cm = self.pick(self.configured_render(), "ConfigMap", "oauth-idp-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("oauth-idp-upsert.sh", "oauth-idp-remove.sh")}
        self.entry = json.loads(cm["data"]["idp-entry.json"])
        with open(os.path.join(self.work, "entry.json"), "w") as f:
            json.dump(self.entry, f)
        self.other_idp = {
            "name": "htpasswd",
            "type": "HTPasswd",
            "mappingMethod": "claim",
            "htpasswd": {"fileData": {"name": "htpasswd-secret"}},
        }

    def oauth_doc(self, idps, resource_version="1597"):
        return {
            "apiVersion": "config.openshift.io/v1",
            "kind": "OAuth",
            "metadata": {"name": "cluster", "resourceVersion": resource_version},
            "spec": {"identityProviders": idps},
        }

    def canned_doc(self):
        return self.oauth_doc([])

    def script_env(self):
        return {
            "IDP_NAME": "rhbk",
            "ENTRY_FILE": os.path.join(self.work, "entry.json"),
        }

    def assert_get_argv(self):
        argv = self.log().splitlines()
        self.assertEqual(
            argv[0],
            "get oauths.config.openshift.io/cluster -o json "
            "--request-timeout=30s")

    def test_child_environment_is_minimal_and_isolated(self):
        result = self.run_script("oauth-idp-upsert.sh", "fresh",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
        child = self.child_env()
        # The parent-only sentinel proves no parent variable leaked through.
        self.assertNotIn("KA_PARENT_SENTINEL", child)
        self.assertEqual(child["IDP_NAME"], "rhbk")
        self.assertEqual(child["HOME"], self.work)
        self.assertEqual(child["PATH"], os.path.join(self.work, "bin"))
        self.assertEqual(
            {k for k in child if k.startswith("OC_")},
            {"OC_SCENARIO", "OC_LOG", "OC_ENV", "OC_GET_FILE",
             "OC_PATCH_LOG", "OC_ATTEMPTS"})

    def test_upsert_success(self):
        result = self.run_script("oauth-idp-upsert.sh", "fresh",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_get_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"identityProviders": [self.other_idp, self.entry]},
        })

    def test_upsert_is_idempotent_no_op_when_entry_matches(self):
        result = self.run_script(
            "oauth-idp-upsert.sh", "fresh",
            self.oauth_doc([self.other_idp, self.entry]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_upsert_replaces_stale_same_name_entry(self):
        stale = json.loads(json.dumps(self.entry))
        stale["openID"]["clientID"] = "stale-client"
        result = self.run_script("oauth-idp-upsert.sh", "fresh",
                                 self.oauth_doc([stale]))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["spec"]["identityProviders"], [self.entry])

    def test_upsert_no_op_when_equal_entry_is_not_last(self):
        # An exactly equal entry earlier in the list must be a no-op, not a
        # reordering patch — the entry is matched by name.
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "oauth-idp-upsert.sh", "fresh",
                    self.oauth_doc([self.entry, self.other_idp]), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.patches(), [])
                self.assertEqual(len(self.log().splitlines()), 1)

    def test_upsert_replaces_stale_mid_list_entry_in_place(self):
        stale = json.loads(json.dumps(self.entry))
        stale["openID"]["clientID"] = "stale-client"
        tail = {"name": "ldap", "type": "LDAP", "mappingMethod": "claim",
                "ldap": {"url": "ldap://ldap.example.com"}}
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "oauth-idp-upsert.sh", "fresh",
                    self.oauth_doc([self.other_idp, stale, tail]), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                patches = self.patches()
                self.assertEqual(len(patches), 1)
                self.assertEqual(patches[0]["spec"]["identityProviders"],
                                 [self.other_idp, self.entry, tail])

    def test_upsert_retries_conflict_then_succeeds(self):
        result = self.run_script("oauth-idp-upsert.sh", "conflict-then-success",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
        # Exactly one GET per attempt: the retry must re-read the object.
        gets = [line for line in self.log().splitlines()
                if line.startswith("get ")]
        self.assertEqual(len(gets), 2)
        patches = self.patches()
        self.assertEqual(len(patches), 2)
        # The fake oc bumps the served resourceVersion on every GET, so the
        # failed attempt carried the stale version and the successful retry
        # carried the fresh one from its own re-read.
        self.assertEqual(patches[0]["metadata"]["resourceVersion"], "1597")
        self.assertEqual(patches[1]["metadata"]["resourceVersion"], "1598")
        self.assertIn("retrying", result.stderr)

    def test_upsert_conflict_exhaustion_fails(self):
        result = self.run_script("oauth-idp-upsert.sh", "conflict-exhausted",
                                 self.oauth_doc([self.other_idp]))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)
        self.assertIn("Conflict", result.stderr)

    def test_upsert_non_conflict_api_error_propagates(self):
        result = self.run_script("oauth-idp-upsert.sh", "patch-error",
                                 self.oauth_doc([self.other_idp]))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)
        self.assertEqual(len(self.patches()), 1)

    def test_upsert_absent_oauth_object_fails_with_prerequisite(self):
        result = self.run_script("oauth-idp-upsert.sh", "absent-object",
                                 self.oauth_doc([self.other_idp]))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prerequisite missing", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_upsert_get_error_propagates(self):
        result = self.run_script("oauth-idp-upsert.sh", "get-error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_upsert_python3_tool_path(self):
        result = self.run_script("oauth-idp-upsert.sh", "fresh",
                                 self.oauth_doc([self.other_idp]),
                                 include_jq=False, include_python=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["spec"]["identityProviders"],
                         [self.other_idp, self.entry])

    def test_upsert_jq_tool_path(self):
        if shutil.which("jq") is None:
            self.skipTest("jq not available on the host")
        result = self.run_script("oauth-idp-upsert.sh", "fresh",
                                 self.oauth_doc([self.other_idp]),
                                 include_jq=True, include_python=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["spec"]["identityProviders"],
                         [self.other_idp, self.entry])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("oauth-idp-upsert.sh", "fresh",
                                 self.oauth_doc([self.other_idp]),
                                 include_jq=False, include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")

    def test_patch_payload_carries_no_secret_material(self):
        result = self.run_script("oauth-idp-upsert.sh", "fresh",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
        for patch in self.patches():
            self.assertNotIn("test-client-secret", json.dumps(patch))
            self.assertNotIn(base64.b64encode(b"test-client-secret").decode(),
                             json.dumps(patch))

    def test_remove_success_preserves_other_idps(self):
        result = self.run_script("oauth-idp-remove.sh", "fresh",
                                 self.oauth_doc([self.other_idp, self.entry]))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"identityProviders": [self.other_idp]},
        })

    def test_remove_python3_tool_path(self):
        result = self.run_script("oauth-idp-remove.sh", "fresh",
                                 self.oauth_doc([self.other_idp, self.entry]),
                                 include_jq=False, include_python=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"identityProviders": [self.other_idp]},
        })

    def test_remove_is_idempotent_no_op_when_entry_absent(self):
        result = self.run_script("oauth-idp-remove.sh", "fresh",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_remove_absent_oauth_object_is_successful_no_op(self):
        result = self.run_script("oauth-idp-remove.sh", "absent-object",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])

    def test_remove_conflict_exhaustion_fails(self):
        result = self.run_script("oauth-idp-remove.sh", "conflict-exhausted",
                                 self.oauth_doc([self.other_idp, self.entry]))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)


class LogoutRedirectSurfaceTests(ChartFixture):
    """With logoutRedirect.enabled and the OIDC secret configured, the
    console logout-redirect surface renders: the script/URL ConfigMap, its
    dedicated ServiceAccount/ClusterRole/ClusterRoleBinding, and the set
    (post-install,post-upgrade) and unset (pre-delete) hook Jobs. The
    surface skips silently when the secret is absent (the chart's
    documented convention for IdP resources) or the feature is disabled."""

    DEFAULT_URL = (
        "https://keycloak.apps.cluster.example.com/realms/sso/protocol/"
        "openid-connect/logout?client_id=ocp-idp"
        "&post_logout_redirect_uri=https%3A%2F%2F"
        "console-openshift-console.apps.cluster.example.com")

    def configured(self, **logout_overrides):
        values = self.configured_values()
        if logout_overrides:
            values["logoutRedirect"] = logout_overrides
        return self.by_kind(self.render(values))

    def logout_names(self, by_kind):
        return {d["metadata"]["name"]
                for kind in ("ConfigMap", "ServiceAccount", "ClusterRole",
                             "ClusterRoleBinding", "Job")
                for d in by_kind.get(kind, [])
                if d["metadata"]["name"].startswith("logout-")}

    def test_default_secret_configured_renders_full_surface(self):
        by_kind = self.configured()
        names = self.logout_names(by_kind)
        for name in ("logout-redirect-config", "logout-redirect",
                     "logout-redirect-set", "logout-redirect-unset"):
            self.assertIn(name, names)
        self.assertEqual(
            self.pick(by_kind, "ConfigMap", "logout-redirect-config")
            ["data"]["logout-redirect-url"], self.DEFAULT_URL)

    def test_enabled_without_secret_skips_silently(self):
        # enabled defaults to true; an empty global.oidc.secret gates the
        # whole surface off (same conjunction as the IdP surface) whether
        # the enabled key is defaulted or explicit.
        for by_kind in (self.by_kind(self.render()),
                        self.by_kind(
                            self.render({"logoutRedirect": {"enabled": True}})),
                        self.configured(enabled=False)):
            self.assertEqual(self.logout_names(by_kind), set())

    def test_set_job_is_post_install_post_upgrade_hook(self):
        job = self.pick(self.configured(), "Job", "logout-redirect-set")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(job["spec"]["backoffLimit"], 4)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "logout-redirect")
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(container["command"],
                         ["/mnt/logout-redirect-set.sh"])
        self.assertEqual(
            container["image"],
            "image-registry.openshift-image-registry.svc:5000/openshift/tools:latest")
        self.assertEqual(container["volumeMounts"], [{
            "name": "logout-redirect-config", "mountPath": "/mnt",
        }])
        self.assertEqual(pod["volumes"], [{
            "name": "logout-redirect-config",
            "configMap": {"name": "logout-redirect-config",
                          "defaultMode": 493},
        }])

    def test_unset_job_is_pre_delete_hook(self):
        job = self.pick(self.configured(), "Job", "logout-redirect-unset")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"], "pre-delete")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(
            job["spec"]["template"]["spec"]["serviceAccountName"],
            "logout-redirect")
        self.assertEqual(
            job["spec"]["template"]["spec"]["containers"][0]["command"],
            ["/mnt/logout-redirect-unset.sh"])

    def test_rbac_is_get_and_patch_on_named_console_only(self):
        by_kind = self.configured()
        sa = self.pick(by_kind, "ServiceAccount", "logout-redirect")
        self.assertNotIn("namespace", sa["metadata"])
        role = self.pick(by_kind, "ClusterRole", "logout-redirect")
        self.assertEqual(role["rules"], [{
            "apiGroups": ["config.openshift.io"],
            "resources": ["consoles"],
            "resourceNames": ["cluster"],
            "verbs": ["get", "patch"],
        }])
        binding = self.pick(by_kind, "ClusterRoleBinding", "logout-redirect")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "logout-redirect",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "logout-redirect",
            "namespace": "test-release-ns",
        }])

    def test_configmap_delivers_scripts_and_url(self):
        cm = self.pick(self.configured(), "ConfigMap",
                       "logout-redirect-config")
        data = cm["data"]
        for key in ("logout-redirect-set.sh", "logout-redirect-unset.sh",
                    "logout-redirect-url"):
            self.assertIn(key, data)
        for key in ("logout-redirect-set.sh", "logout-redirect-unset.sh"):
            self.assertIn("set -euo pipefail", data[key])
            self.assertIn("consoles.config.openshift.io/cluster", data[key])

    def test_console_host_override_honored(self):
        values = {
            "global": {
                "oidc": {
                    "secret": "test-client-secret",
                    "consoleHost": "console.example.com",
                },
            },
        }
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "logout-redirect-config")
        self.assertEqual(
            cm["data"]["logout-redirect-url"],
            "https://keycloak.apps.cluster.example.com/realms/sso/protocol/"
            "openid-connect/logout?client_id=ocp-idp"
            "&post_logout_redirect_uri=https%3A%2F%2Fconsole.example.com")


class LogoutRedirectScriptTests(FakeOcScriptFixture):
    """Executes the rendered logout-redirect hook scripts against a fake
    oc binary serving a canned Console object."""

    get_file_name = "console.json"

    def setUp(self):
        super().setUp()
        cm = self.pick(self.configured_render(), "ConfigMap",
                       "logout-redirect-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("logout-redirect-set.sh",
                                     "logout-redirect-unset.sh")}
        self.url = cm["data"]["logout-redirect-url"].strip()
        self.url_file = os.path.join(self.work, "logout-redirect-url")
        with open(self.url_file, "w") as f:
            f.write(self.url + "\n")

    def console_doc(self, logout_redirect=None, resource_version="1597"):
        doc = {
            "apiVersion": "config.openshift.io/v1",
            "kind": "Console",
            "metadata": {"name": "cluster",
                         "resourceVersion": resource_version},
            "spec": {},
        }
        if logout_redirect is not None:
            doc["spec"]["authentication"] = {
                "logoutRedirect": logout_redirect}
        return doc

    def canned_doc(self):
        return self.console_doc()

    def script_env(self):
        return {"REDIRECT_URL_FILE": self.url_file}

    def assert_get_argv(self):
        argv = self.log().splitlines()
        self.assertEqual(
            argv[0],
            "get consoles.config.openshift.io/cluster -o json "
            "--request-timeout=30s")

    def assert_patch_argv(self):
        for line in self.log().splitlines():
            if line.startswith("patch "):
                self.assertIn(
                    "patch consoles.config.openshift.io/cluster --type=merge",
                    line)

    def test_set_success_on_unset_field(self):
        result = self.run_script("logout-redirect-set.sh", "fresh",
                                 self.console_doc())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_get_argv()
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"authentication": {"logoutRedirect": self.url}},
        })

    def test_set_is_idempotent_no_op_when_equal(self):
        result = self.run_script("logout-redirect-set.sh", "fresh",
                                 self.console_doc(self.url))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_set_stops_loudly_on_foreign_value(self):
        result = self.run_script("logout-redirect-set.sh", "fresh",
                                 self.console_doc("https://elsewhere.example.com"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign", result.stderr)
        self.assertIn("https://elsewhere.example.com", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_set_absent_console_object_fails_with_prerequisite(self):
        result = self.run_script("logout-redirect-set.sh", "absent-object",
                                 self.console_doc())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prerequisite missing", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_set_retries_conflict_then_succeeds(self):
        result = self.run_script("logout-redirect-set.sh",
                                 "conflict-then-success", self.console_doc())
        self.assertEqual(result.returncode, 0, result.stderr)
        gets = [line for line in self.log().splitlines()
                if line.startswith("get ")]
        self.assertEqual(len(gets), 2)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[0]["metadata"]["resourceVersion"], "1597")
        self.assertEqual(patches[1]["metadata"]["resourceVersion"], "1598")
        self.assertIn("retrying", result.stderr)

    def test_set_conflict_exhaustion_fails(self):
        result = self.run_script("logout-redirect-set.sh",
                                 "conflict-exhausted", self.console_doc())
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)

    def test_unset_clears_own_value(self):
        result = self.run_script("logout-redirect-unset.sh", "fresh",
                                 self.console_doc(self.url))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"authentication": {"logoutRedirect": None}},
        })

    def test_unset_leaves_foreign_value(self):
        result = self.run_script("logout-redirect-unset.sh", "fresh",
                                 self.console_doc("https://elsewhere.example.com"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_unset_is_no_op_when_already_unset(self):
        result = self.run_script("logout-redirect-unset.sh", "fresh",
                                 self.console_doc())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_unset_absent_console_object_is_successful_no_op(self):
        result = self.run_script("logout-redirect-unset.sh", "absent-object",
                                 self.console_doc(self.url))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])

    def test_unset_conflict_exhaustion_fails(self):
        result = self.run_script("logout-redirect-unset.sh",
                                 "conflict-exhausted",
                                 self.console_doc(self.url))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)

    def test_set_python3_tool_path(self):
        result = self.run_script("logout-redirect-set.sh", "fresh",
                                 self.console_doc(),
                                 include_jq=False, include_python=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["spec"]["authentication"]
                         ["logoutRedirect"], self.url)

    def test_set_jq_tool_path(self):
        if shutil.which("jq") is None:
            self.skipTest("jq not available on the host")
        result = self.run_script("logout-redirect-set.sh", "fresh",
                                 self.console_doc(),
                                 include_jq=True, include_python=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["spec"]["authentication"]
                         ["logoutRedirect"], self.url)

    def test_unset_python3_tool_path(self):
        result = self.run_script("logout-redirect-unset.sh", "fresh",
                                 self.console_doc(self.url),
                                 include_jq=False, include_python=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertIsNone(
            patches[0]["spec"]["authentication"]["logoutRedirect"])

    def test_unset_jq_tool_path(self):
        if shutil.which("jq") is None:
            self.skipTest("jq not available on the host")
        result = self.run_script("logout-redirect-unset.sh", "fresh",
                                 self.console_doc(self.url),
                                 include_jq=True, include_python=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertIsNone(
            patches[0]["spec"]["authentication"]["logoutRedirect"])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        for script in ("logout-redirect-set.sh", "logout-redirect-unset.sh"):
            with self.subTest(script=script):
                result = self.run_script(script, "fresh",
                                         self.console_doc(),
                                         include_jq=False,
                                         include_python=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("neither jq nor python3", result.stderr)
                self.assertEqual(self.patches(), [])
                self.assertFalse(
                    os.path.exists(os.path.join(self.work, "commands.log")),
                    "oc must not be invoked when no JSON tool is available")


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
        self.assertEqual(meta["version"], "0.4.1")
        self.assertEqual(meta["appVersion"], "0.4.1")

    def test_umbrella_version_and_pin_coherence(self):
        umbrella = self.load(os.path.join(UMBRELLA, "Chart.yaml"))
        self.assertEqual(umbrella["version"], "0.5.2")
        self.assertNotIn("appVersion", umbrella)
        deps = {d["name"]: d["version"] for d in umbrella["dependencies"]}
        self.assertEqual(deps["keycloak-auth"], ">=0.4.0 <0.5.0")
        # The pin's range must contain the chart's new version.
        chart_version = tuple(
            int(p) for p in self.load(os.path.join(CHART, "Chart.yaml"))["version"].split("."))
        self.assertGreaterEqual(chart_version, (0, 4, 0))
        self.assertLess(chart_version, (0, 5, 0))


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
