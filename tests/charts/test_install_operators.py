"""Focused tests for the install-operators chart. The module covers the
platform-object boundary (operator Namespaces are never release manifests —
they are ensured by a pre-install/pre-upgrade conditional-create hook, and
uninstall never deletes them), the Subscription ownership flip (hook-
maintained by default; ownSubscription: true opts back into a release-owned
Subscription), the namespace-conflict render diagnostic, and the hook
RBAC (resourceName-scoped with the one documented unscoped `create` grant).

Render tests run the chart through ``helm template`` (the chart has no
dependencies). Per the repo's unit-test policy the script-execution layer is
a minimal smoke set: happy-path runs of the rendered hook scripts (namespace
ensure, subscription set and remove) plus one fail-closed no-tool check for
the chart, each against a fake ``oc`` on a restricted PATH built entirely
from symlinks, so neither a real ``oc`` nor the test host's ambient tools
can leak into the run. Hook-script state-machine behavior (409 retries,
foreign-value STOP, walk-back, concurrency races) is proven by live-cluster
deploys, not fixtures.
"""

import json
import os
import unittest

import support

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts",
    "install-operators"))

PROOF_ANNOTATION = "install-operators/proof"

# An enabled operator installing into an explicitly-configured (frequently
# pre-existing, operator-suggested) namespace, with an OperatorGroup.
HOOK_OPERATOR = {
    "channel": "stable",
    "namespace": "test-operators",
    "operatorGroup": {"enabled": True},
}


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "io-test-"
    values_prefix = "io-values-"

    @staticmethod
    def hook_operator(**overrides):
        config = json.loads(json.dumps(HOOK_OPERATOR))
        config.update(overrides)
        return {"operators": {"test-operator": config}}


class RenderTests(ChartFixture):
    def test_default_and_hook_render(self):
        # Default values render nothing.
        self.assertEqual(self.render(), [])
        # With an explicitly-configured operator namespace, the
        # ensure-operator-namespaces surface and the Subscription hook
        # surface render; no Namespace manifest renders for any values
        # combination (operator Namespaces frequently pre-exist — Helm
        # uninstall deletes release-owned Namespaces and the deletion
        # cascades to everything inside them).
        by_kind = self.by_kind(self.render(self.hook_operator()))
        self.assertNotIn("Namespace", by_kind)
        self.assertNotIn("Subscription", by_kind)
        for kind in ("ServiceAccount", "ClusterRole", "ClusterRoleBinding",
                     "ConfigMap", "Job"):
            self.assertIn(kind, by_kind)
        # The ensure-ns hook Job is a pre-install/pre-upgrade hook.
        job = self.pick(by_kind, "Job", "ensure-operator-namespaces")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "pre-install,pre-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        # The ClusterRole carries the one documented unscoped create grant;
        # every mutating read-back verb is scoped to the managed namespace
        # names.
        role = self.pick(by_kind, "ClusterRole",
                         "ensure-operator-namespaces")
        self.assertEqual(role["rules"], [
            {"apiGroups": [""],
             "resources": ["namespaces"],
             "verbs": ["create"]},
            {"apiGroups": [""],
             "resources": ["namespaces"],
             "resourceNames": ["test-operators"],
             "verbs": ["get", "patch"]},
        ])
        # The Subscription hook surface: set (post-install,post-upgrade) and
        # remove (pre-delete) Jobs in the operator's namespace, with the
        # Role scoped to the named Subscription plus the unscoped create.
        for name, hook in (("install-test-operator-set",
                            "post-install,post-upgrade"),
                           ("install-test-operator-remove", "pre-delete")):
            job = self.pick(by_kind, "Job", name, namespace="test-operators")
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook"], hook)
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
                "before-hook-creation")
        role = self.pick(by_kind, "Role", "install-test-operator",
                         namespace="test-operators")
        self.assertEqual(role["rules"], [
            {"apiGroups": ["operators.coreos.com"],
             "resources": ["subscriptions"],
             "resourceNames": ["test-operator"],
             "verbs": ["get", "patch", "delete"]},
            {"apiGroups": ["operators.coreos.com"],
             "resources": ["subscriptions"],
             "verbs": ["create"]},
        ])

    def test_own_subscription_opt_in_renders_release_manifest(self):
        by_kind = self.by_kind(self.render(
            self.hook_operator(ownSubscription=True)))
        subscription = self.pick(by_kind, "Subscription", "test-operator",
                                 namespace="test-operators")
        self.assertEqual(subscription["spec"], {
            "channel": "stable",
            "installPlanApproval": "Automatic",
            "name": "test-operator",
            "source": "redhat-operators",
            "sourceNamespace": "openshift-marketplace",
        })
        # The opt-in replaces the hook surface for that operator.
        for kind in ("Role", "RoleBinding"):
            self.assertNotIn(kind, by_kind)
        names = {d["metadata"]["name"] for d in by_kind.get("Job", [])}
        self.assertNotIn("install-test-operator-set", names)
        self.assertNotIn("install-test-operator-remove", names)

    def test_conflicting_labels_fail_the_render(self):
        # Two entries sharing a namespace must declare identical
        # namespaceLabels, or the render fails naming both entries.
        values = self.hook_operator(namespaceLabels={"monitoring": "on"})
        values["operators"]["other-operator"] = dict(
            HOOK_OPERATOR, channel="stable",
            namespaceLabels={"monitoring": "off"})
        self.render_failure(values)


# Fake oc for the hook-script smoke runs: serves a canned object for get
# (or NotFound when OC_NOT_FOUND is set), records every invocation, logs
# --patch= payloads, captures `create -f -` manifests from stdin, and
# succeeds silently for delete. Each get bumps the served
# metadata.resourceVersion (first get serves the canned value, each later
# get serves it incremented), so a retry could only patch successfully with
# a version from a fresh read.
FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" && "$2" == "installplan" ]]; then
  # The approve script's discovery surface: `oc get installplan
  # -ogo-template=...` prints plan names, so the fake serves canned names.
  cat "$OC_PLAN_NAMES"
  exit 0
elif [[ "$1" == "get" ]]; then
  if [[ -n "$OC_NOT_FOUND" ]]; then
    echo "Error from server (NotFound)" >&2
    exit 1
  fi
  rv_file="$OC_GET_FILE.rv"
  if [[ -f "$rv_file" ]]; then
    next_rv=$(($(cat "$rv_file") + 1))
  else
    next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$OC_GET_FILE" | grep -oE '[0-9]+')
  fi
  echo "$next_rv" > "$rv_file"
  sed "s/\\"resourceVersion\\": *\\"[0-9]*\\"/\\"resourceVersion\\": \\"$next_rv\\"/" "$OC_GET_FILE"
elif [[ "$1" == "patch" ]]; then
  prev=""
  for arg in "$@"; do
    if [[ "$arg" == --patch=* ]]; then
      printf '%s\\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
    elif [[ "$prev" == "--patch" ]]; then
      printf '%s\\n' "$arg" >> "$OC_PATCH_LOG"
    fi
    prev="$arg"
  done
  exit 0
elif [[ "$1" == "create" ]]; then
  cat > "$OC_CREATE_FILE"
  exit 0
elif [[ "$1" == "delete" ]]; then
  exit 0
else
  echo "unexpected oc invocation: $*" >&2
  exit 64
fi
"""


class FakeOcScriptFixture(ChartFixture, support.FakeOcScriptFixture):
    """Restricted-PATH harness for the install-operators hook scripts; the
    canned oc program (NotFound switch, rv-bumping get, patch capture,
    create-from-stdin capture, silent delete) is this module's
    FAKE_OC_SCRIPT."""

    work_prefix = "io-script-"
    fake_oc_script = FAKE_OC_SCRIPT

    def created(self):
        path = os.path.join(self.work, "created.json")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def render_config(self, values):
        return self.pick(self.by_kind(self.render(values)), "ConfigMap",
                         "ensure-operator-namespaces")

    def run_script(self, script_name, scripts, canned_doc=None,
                   include_python=True, extra_env=None):
        # Single tool path for the smoke set: python3-only restricted PATH.
        bin_dir = self.build_bin_dir(include_python=include_python)
        self.stage_scripts(scripts)
        self.clear_run_state("created.json")
        if canned_doc is not None:
            with open(os.path.join(self.work, self.get_file_name), "w") as f:
                json.dump(canned_doc, f)
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_GET_FILE": os.path.join(self.work, self.get_file_name),
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
            "OC_CREATE_FILE": os.path.join(self.work, "created.json"),
        }
        env.update(self.script_env())
        env.update(extra_env or {})
        return self.run_child(os.path.join(self.work, script_name), env)


class EnsureNamespacesScriptTests(FakeOcScriptFixture):
    """Happy-path smoke for the rendered namespace-ensure hook script: the
    create of an absent namespace."""

    get_file_name = "namespace.json"

    def setUp(self):
        super().setUp()
        cm = self.render_config(self.hook_operator())
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "ensure-namespaces.sh")}
        self.script_json = os.path.join(self.work, "namespaces.json")
        with open(self.script_json, "w") as f:
            f.write(cm["data"]["namespaces.json"])

    def script_env(self):
        return {"NAMESPACE_LIST": self.script_json}

    def test_create_absent_namespace(self):
        result = self.run_script("ensure-namespaces.sh", self.scripts,
                                 extra_env={"OC_NOT_FOUND": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log_lines()[0],
            "get namespace/test-operators -o json --request-timeout=30s")
        self.assertTrue(any(line.startswith("create ")
                            for line in self.log_lines()),
                        self.log())
        manifest = self.created()
        self.assertEqual(manifest["apiVersion"], "v1")
        self.assertEqual(manifest["kind"], "Namespace")
        self.assertEqual(manifest["metadata"]["name"], "test-operators")
        self.assertEqual(
            manifest["metadata"]["annotations"][PROOF_ANNOTATION], "true")
        self.assertEqual(self.patches(), [])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        # The chart's fail-closed no-tool check.
        result = self.run_script("ensure-namespaces.sh", self.scripts,
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class SubscriptionScriptTests(FakeOcScriptFixture):
    """Happy-path smoke runs for the rendered Subscription hook scripts:
    the set hook converging an existing Subscription by value, and the
    remove hook deleting a proof-annotated Subscription."""

    get_file_name = "live-subscription.json"

    def setUp(self):
        super().setUp()
        cm = self.pick(self.by_kind(self.render(self.hook_operator())),
                       "ConfigMap", "install-test-operator-config",
                       namespace="test-operators")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh", "subscription-set.sh",
                                     "subscription-remove.sh")}
        self.payload = json.loads(cm["data"]["subscription.json"])
        self.desired_file = os.path.join(self.work, "subscription.json")
        with open(self.desired_file, "w") as f:
            json.dump(self.payload, f)

    def script_env(self):
        return {"DESIRED_FILE": self.desired_file}

    def canned_subscription(self, annotations=None):
        doc = {
            "apiVersion": "operators.coreos.com/v1alpha1",
            "kind": "Subscription",
            "metadata": {"name": "test-operator",
                         "namespace": "test-operators",
                         "resourceVersion": "1597"},
            "spec": {
                "channel": "old-channel",
                "installPlanApproval": "Automatic",
                "name": "test-operator",
                "source": "redhat-operators",
                "sourceNamespace": "openshift-marketplace",
                "startingCSV": "test-operator.v0.9.0",
                "config": {"env": [{"name": "STALE", "value": "x"}]},
            },
        }
        if annotations is not None:
            doc["metadata"]["annotations"] = annotations
        return doc

    def test_set_converges_an_existing_subscription_by_value(self):
        result = self.run_script("subscription-set.sh", self.scripts,
                                 canned_doc=self.canned_subscription())
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {
                "channel": "stable",
                "startingCSV": None,
                "config": {"env": None},
            },
        })

    def test_remove_deletes_only_under_proof(self):
        result = self.run_script(
            "subscription-remove.sh", self.scripts,
            canned_doc=self.canned_subscription(
                annotations={PROOF_ANNOTATION: "true"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            any(line.startswith("delete ") and
                "subscriptions.operators.coreos.com/test-operator" in line
                for line in self.log_lines()), self.log())


class InstallPlanApprovalScriptTests(FakeOcScriptFixture):
    """Happy-path smoke for the rendered InstallPlan approval hook script:
    the approve of the plan the script discovers."""

    get_file_name = "installplans.json"

    def setUp(self):
        super().setUp()
        cm = self.pick(self.by_kind(self.render(
            self.hook_operator(approveCSVs=["test-csv"]))),
            "ConfigMap", "approve-test-operator-script",
            namespace="test-operators")
        self.scripts = {"approve-operator.sh":
                        cm["data"]["approve-operator.sh"]}

    def test_approves_discovered_install_plan(self):
        plan_names = os.path.join(self.work, "plan-names")
        with open(plan_names, "w") as f:
            f.write("test-operator-plan\n")
        result = self.run_script("approve-operator.sh", self.scripts,
                                 extra_env={"OC_PLAN_NAMES": plan_names})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [{"spec": {"approved": True}}])
        patch_calls = [line for line in self.log_lines()
                       if line.startswith("patch ")]
        # The patch carries --type merge, and the script's only other call
        # is the read-only plan discovery (the multi-line go-template
        # argument means the log cannot be split one line per call).
        self.assertEqual(
            len([line for line in self.log_lines()
                 if line.startswith("get installplan")]), 1, self.log())
        self.assertEqual(len(patch_calls), 1, self.log())
        self.assertIn("--type merge", patch_calls[0])


class HookGuardTests(ChartFixture):
    """Every hook Job container carries the guard-without-pin
    securityContext: the four restricted-pod guards and no
    runAsUser/runAsGroup anywhere in the pod — restricted SCCs assign the
    namespace range UID to UID-less pods, and a pin would fight that
    platform assignment."""

    def test_every_hook_job_is_guarded_without_pin(self):
        jobs = support.hook_jobs(
            self.render(self.hook_operator(approveCSVs=["test-csv"])))
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in jobs),
            ["approve-test-operator", "ensure-operator-namespaces",
             "install-test-operator-remove", "install-test-operator-set"])
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])
