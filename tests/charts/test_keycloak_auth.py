"""Focused tests for the keycloak-auth chart. The module covers the
default-off kubeadmin-removal surface (no removal Job, ServiceAccount,
Role, or RoleBinding unless explicitly opted in), the exact named-secret
delete-only RBAC and rendered removal command, the OAuth identity-provider
hook surface (the platform OAuth object is never owned — hook Jobs upsert
and remove only this chart's named entry with a resourceVersion-conditional
read-modify-write), the console logout-redirect surface (hook Jobs set and
unset spec.authentication.logoutRedirect on the platform Console object with
the same conditional-write discipline, gated on logoutRedirect.enabled and
global.oidc.secret), and the chart's default-off values and version/appVersion
lockstep (the umbrella example's composition checks live in
test_maas_umbrella).

Render tests run the chart through ``helm template`` (keycloak-auth has
no chart dependencies, so no stubs or registry access are needed). Per the
repo's unit-test policy the script-execution layer is a minimal smoke set:
ONE happy-path run per hook script (on a python3-only restricted PATH built
entirely from symlinks, so neither a real ``oc`` nor the test host's
ambient tools can leak into the run) and ONE fail-closed no-tool check for
the chart. State-machine behavior (409 retries, foreign-value STOP,
walk-back) is proven by live-cluster validation, not fixtures.
"""

import base64
import json
import os
import unittest

import yaml

import support

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "keycloak-auth"))

# Fake oc for the hook-script smoke runs: serves a canned OAuth/Console
# object for get and records every --patch= payload. Each get bumps the
# served metadata.resourceVersion (first get serves the canned value, each
# later get serves it incremented), so a retry could only patch
# successfully with a version from a fresh read. Every invocation is
# logged so tests can assert the exact oc call surface.
FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" ]]; then
  rv_file="$OC_GET_FILE.rv"
  if [[ -f "$rv_file" ]]; then
    next_rv=$(($(cat "$rv_file") + 1))
  else
    next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$OC_GET_FILE" | grep -oE '[0-9]+')
  fi
  echo "$next_rv" > "$rv_file"
  sed "s/\\"resourceVersion\\": *\\"[0-9]*\\"/\\"resourceVersion\\": \\"$next_rv\\"/" "$OC_GET_FILE"
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


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "ka-test-"
    values_prefix = "ka-values-"

    def render_stdout(self, values=None):
        """Asserts success and returns the raw rendered stream."""
        result = self.helm_template(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

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
        names = {d["metadata"]["name"]
                 for kind_docs in by_kind.values() for d in kind_docs}
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
        for key in ("hook-preamble.sh", "oauth-idp-upsert.sh",
                    "oauth-idp-remove.sh", "idp-entry.json"):
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
            raw = self.render_stdout(values)
            self.assertNotIn("test-client-secret", raw)
            self.assertEqual(raw.count(secret_b64), 1,
                             "the secret's encoded value must appear exactly "
                             "once, in the openid-client-secret Secret")


class FakeOcScriptFixture(ChartFixture, support.FakeOcScriptFixture):
    """Restricted-PATH harness for the keycloak-auth hook scripts; the
    canned oc program (rv-bumping get, --patch= capture) is this module's
    FAKE_OC_SCRIPT."""

    work_prefix = "ka-script-"
    fake_oc_script = FAKE_OC_SCRIPT


class OAuthIdpScriptTests(FakeOcScriptFixture):
    """Smoke set for the rendered OAuth hook scripts: one happy-path run
    per script plus the chart's fail-closed no-tool check."""

    get_file_name = "oauth.json"

    def setUp(self):
        super().setUp()
        cm = self.pick(self.configured_render(), "ConfigMap", "oauth-idp-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh", "oauth-idp-upsert.sh",
                                     "oauth-idp-remove.sh")}
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

    def test_upsert_success(self):
        result = self.run_script("oauth-idp-upsert.sh",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_get_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"identityProviders": [self.other_idp, self.entry]},
        })
        # No credential may ride the patch payload in any encoding
        for patch in patches:
            self.assertNotIn("test-client-secret", json.dumps(patch))
            self.assertNotIn(base64.b64encode(b"test-client-secret").decode(),
                             json.dumps(patch))

    def test_remove_success_preserves_other_idps(self):
        result = self.run_script("oauth-idp-remove.sh",
                                 self.oauth_doc([self.other_idp, self.entry]))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"identityProviders": [self.other_idp]},
        })

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("oauth-idp-upsert.sh",
                                 self.oauth_doc([self.other_idp]),
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


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
        for key in ("hook-preamble.sh", "logout-redirect.sh",
                    "logout-redirect-set.sh",
                    "logout-redirect-unset.sh", "logout-redirect-url"):
            self.assertIn(key, data)
        for key in ("logout-redirect-set.sh", "logout-redirect-unset.sh"):
            self.assertIn("set -euo pipefail", data[key])
            self.assertIn('conditional_write "$CONSOLE"', data[key])
        # The Console resource reference the scripts drive lives in the
        # shared logout-redirect scaffold
        self.assertIn("consoles.config.openshift.io/cluster",
                      data["logout-redirect.sh"])

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
    """Smoke set for the rendered logout-redirect hook scripts: one
    happy-path run per script against a fake oc serving a canned Console
    object (the chart's fail-closed no-tool check lives in
    OAuthIdpScriptTests)."""

    get_file_name = "console.json"

    def setUp(self):
        super().setUp()
        cm = self.pick(self.configured_render(), "ConfigMap",
                       "logout-redirect-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh", "logout-redirect.sh",
                                     "logout-redirect-set.sh",
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
        result = self.run_script("logout-redirect-set.sh",
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

    def test_unset_clears_own_value(self):
        result = self.run_script("logout-redirect-unset.sh",
                                 self.console_doc(self.url))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"authentication": {"logoutRedirect": None}},
        })



class ChartDefaultsTests(unittest.TestCase):
    """Static checks on the shipped keycloak-auth chart metadata: the
    kubeadmin-removal surface stays default-off and version/appVersion move
    in lockstep. Umbrella composition checks live in test_maas_umbrella."""

    @staticmethod
    def load(path):
        with open(path) as f:
            return yaml.safe_load(f)

    def test_chart_default_is_safe(self):
        self.assertIs(
            self.load(os.path.join(CHART, "values.yaml"))["removeKubeAdmin"],
            False)

    def test_auth_chart_version_bump(self):
        meta = self.load(os.path.join(CHART, "Chart.yaml"))
        self.assertEqual(meta["version"], "0.4.4")
        self.assertEqual(meta["appVersion"], "0.4.4")


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


class HookGuardTests(ChartFixture):
    """Hook Job containers carry the guard-without-pin securityContext: the
    four restricted-pod guards and no runAsUser/runAsGroup anywhere in the
    pod — restricted SCCs assign the namespace range UID to UID-less pods,
    and a pin would fight that platform assignment. The remove-kubeadmin
    Job is exempt: kube-system is PSA-privileged and bypasses SCC
    assignment, so the guards cannot pass there and it runs as the stock
    image's default user by design."""

    def test_every_guarded_hook_job_is_guarded_without_pin(self):
        jobs = support.hook_jobs(self.render(self.configured_values(remove=True)))
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in jobs),
            ["logout-redirect-set", "logout-redirect-unset",
             "oauth-idp-remove", "oauth-idp-upsert", "remove-kubeadmin"])
        guarded = [j for j in jobs
                   if j["metadata"]["name"] != "remove-kubeadmin"]
        for job in guarded:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])

    def test_kubeadmin_hook_job_has_no_security_context(self):
        # The exemption is a contract: a future "helpful" guard re-addition
        # must fail this suite.
        job = self.pick(
            self.opt_in_render(), "Job", "remove-kubeadmin")
        pod = job["spec"]["template"]["spec"]
        self.assertNotIn("securityContext", pod)
        self.assertNotIn(
            "securityContext", pod["containers"][0])
