"""Focused tests for the keycloak-auth chart: the default render gates
every hook surface off, the platform OAuth object is never owned, the
opt-in kubeadmin-removal and configured OIDC/logout-redirect surfaces
render with resourceName-scoped RBAC, and the hook-script smoke set (one
happy-path run per script plus the chart's fail-closed no-tool check).

Render tests run the chart through ``helm template`` (keycloak-auth has
no chart dependencies, so no stubs or registry access are needed).
State-machine behavior (409 retries, foreign-value STOP, walk-back) is
proven by live-cluster validation, not fixtures.
"""

import base64
import json
import os
import unittest

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

    @staticmethod
    def configured_values(remove=False):
        return {
            "global": {"oidc": {"secret": "test-client-secret"}},
            "removeKubeAdmin": remove,
        }

    def configured_render(self):
        return self.by_kind(self.render(self.configured_values()))

    def opt_in_render(self):
        return self.by_kind(self.render({"removeKubeAdmin": True}))


class RenderTests(ChartFixture):
    def test_default_render_gates_every_surface_off(self):
        by_kind = self.by_kind(self.render())
        # The kubeadmin-removal, OAuth IdP, and logout-redirect surfaces are
        # all opt-in: no hook Jobs, no ServiceAccounts, no Roles.
        for kind in ("Job", "ServiceAccount", "Role", "RoleBinding",
                     "ClusterRole"):
            self.assertNotIn(kind, by_kind)
        # The platform OAuth object always exists on OpenShift, so the chart
        # must never render it as a release-owned manifest — configured or
        # not.
        self.assertNotIn("OAuth", by_kind)
        # The chart's always-on surface: the cluster-admins Group binding
        # (chart-prefixed so it cannot collide with the platform's own
        # bootstrap binding; the Group it targets is deliberately
        # unprefixed — OAuth group-mapping target).
        binding = self.pick(by_kind, "ClusterRoleBinding",
                            "keycloak-auth-cluster-admins")
        self.assertEqual(binding["roleRef"]["name"], "cluster-admin")
        self.assertEqual(binding["subjects"], [{
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Group",
            "name": "cluster-admins",
        }])

    def test_opt_in_and_configured_surfaces_render(self):
        # The kubeadmin-removal surface is opt-in only: the removal grant is
        # delete-only and named to the kubeadmin secret, and the rendered
        # command is direct oc argv — no shell layer, no xtrace, no error
        # suppression.
        by_kind = self.opt_in_render()
        for kind in ("Job", "ServiceAccount", "Role", "RoleBinding"):
            self.assertIn(kind, by_kind)
            doc = by_kind[kind][0]
            self.assertEqual(doc["metadata"]["name"], "remove-kubeadmin")
            self.assertEqual(doc["metadata"]["namespace"], "kube-system")
        role = self.pick(by_kind, "Role", "remove-kubeadmin")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["kubeadmin"],
            "verbs": ["delete"],
        }])
        container = self.pick(
            by_kind, "Job", "remove-kubeadmin"
        )["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual(container["command"], ["oc"])
        self.assertEqual(container["args"], [
            "delete", "secret", "kubeadmin",
            "--namespace", "kube-system",
            "--ignore-not-found", "--wait=false",
        ])

        # With the OIDC secret configured, the OAuth IdP hook surface
        # renders: upsert (post-install,post-upgrade) and remove
        # (pre-delete) hook Jobs, script ConfigMap, ServiceAccount, and a
        # ClusterRole scoped get/patch to the named OAuth object.
        by_kind = self.configured_render()
        for name, hook in (("oauth-idp-upsert", "post-install,post-upgrade"),
                           ("oauth-idp-remove", "pre-delete")):
            job = self.pick(by_kind, "Job", name)
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook"], hook)
        role = self.pick(by_kind, "ClusterRole", "oauth-idp")
        self.assertEqual(role["rules"], [{
            "apiGroups": ["config.openshift.io"],
            "resources": ["oauths"],
            "resourceNames": ["cluster"],
            "verbs": ["get", "patch"],
        }])
        cm = self.pick(by_kind, "ConfigMap", "oauth-idp-config")
        for key in ("hook-preamble.sh", "oauth-idp-upsert.sh",
                    "oauth-idp-remove.sh", "idp-entry.json"):
            self.assertIn(key, cm["data"])
        # The logout-redirect surface renders alongside (the OIDC secret is
        # configured), with its own ClusterRole scoped get/patch to the
        # named Console.
        for name in ("logout-redirect-config", "logout-redirect",
                     "logout-redirect-set", "logout-redirect-unset"):
            self.assertIn(name, {d["metadata"]["name"]
                                 for kind in ("ConfigMap", "ServiceAccount",
                                              "ClusterRole",
                                              "ClusterRoleBinding", "Job")
                                 for d in by_kind.get(kind, [])
                                 if d["metadata"]["name"].startswith("logout-")})
        role = self.pick(by_kind, "ClusterRole", "logout-redirect")
        self.assertEqual(role["rules"], [{
            "apiGroups": ["config.openshift.io"],
            "resources": ["consoles"],
            "resourceNames": ["cluster"],
            "verbs": ["get", "patch"],
        }])
        # The client secret renders only inside the client Secret resource.
        raw_secret_b64 = base64.b64encode(b"test-client-secret").decode()
        secret = self.pick(by_kind, "Secret", "openid-client-secret")
        self.assertEqual(secret["data"]["clientSecret"], raw_secret_b64)
        # The IdP entry carries no secret material.
        self.assertNotIn("test-client-secret", cm["data"]["idp-entry.json"])


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

    def test_upsert_success(self):
        result = self.run_script("oauth-idp-upsert.sh",
                                 self.oauth_doc([self.other_idp]))
        self.assertEqual(result.returncode, 0, result.stderr)
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
        # The chart's fail-closed no-tool check.
        result = self.run_script("oauth-idp-upsert.sh",
                                 self.oauth_doc([self.other_idp]),
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class LogoutRedirectScriptTests(FakeOcScriptFixture):
    """Happy-path smoke for the rendered logout-redirect hook scripts: one
    run per script against a fake oc serving a canned Console object (the
    chart's fail-closed no-tool check lives in OAuthIdpScriptTests)."""

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

    def test_set_success_on_unset_field(self):
        result = self.run_script("logout-redirect-set.sh",
                                 self.console_doc())
        self.assertEqual(result.returncode, 0, result.stderr)
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
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"authentication": {"logoutRedirect": None}},
        })


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
        for job in jobs:
            if job["metadata"]["name"] == "remove-kubeadmin":
                # The exemption is a contract: a future "helpful" guard
                # re-addition must fail this suite.
                pod = job["spec"]["template"]["spec"]
                self.assertNotIn("securityContext", pod)
                self.assertNotIn(
                    "securityContext", pod["containers"][0])
                continue
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])
