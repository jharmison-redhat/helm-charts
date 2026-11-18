"""Focused tests for the install-operators chart. The module covers the
platform-object boundary (operator Namespaces are never release manifests —
they are ensured by a pre-install/pre-upgrade conditional-create hook over
the unique explicitly-configured namespace set, and uninstall never deletes
them), the shared-namespace dedupe and its render-time conflict diagnostic
(two entries sharing a namespace must declare identical
namespaceLabels/namespaceAnnotations values, or the render fails naming both
entries), the Subscription ownership flip (Subscriptions are hook-maintained
by default — a set Job converges the chart-managed spec fields by value on
every install/upgrade and a pre-delete Job removes the Subscription only
under its creation-proof annotation; ownSubscription: true opts back into a
release-owned Subscription), and the hook RBAC (get/patch/delete
resourceName-scoped, with the one documented unscoped `create` grant on each
resource kind).

Render tests run the chart through ``helm template`` (the chart has no
dependencies). Per the repo's unit-test policy the script-execution layer is
a minimal smoke set: happy-path runs of the rendered hook scripts (namespace
ensure — create and the upgrade merge path with its resource-policy keep
stamp — plus subscription set and remove) on the python3-only tool path, and
one fail-closed no-tool check for the chart, each against a fake ``oc`` on a
restricted PATH built entirely from symlinks, so neither a real ``oc`` nor
the test host's ambient tools can leak into the run. Hook-script
state-machine behavior (409 retries, foreign-value STOP, walk-back,
concurrency races) is proven by live-cluster deploys, not fixtures.
"""

import json
import os
import unittest

import yaml

from support import docs
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


class NamespaceNeverOwnedTests(ChartFixture):
    """Operator Namespaces frequently pre-exist (operator-suggested), so no
    values combination may ever render a Namespace release manifest — Helm
    uninstall deletes release-owned Namespaces and the deletion cascades to
    everything inside them."""

    def test_no_namespace_manifest_for_any_values(self):
        shared = self.hook_operator()
        shared["operators"]["other-operator"] = dict(HOOK_OPERATOR,
                                                     channel="stable")
        cases = (
            {},
            self.hook_operator(),
            self.hook_operator(ownSubscription=True),
            self.hook_operator(enabled=False),
            shared,
            {"operators": {
                "default-ns-operator": {"channel": "stable"},
            }},
        )
        for values in cases:
            by_kind = self.by_kind(self.render(values))
            self.assertNotIn("Namespace", by_kind, values)

    def test_default_values_render_nothing(self):
        self.assertEqual(self.render(), [])


class NamespaceHookTests(ChartFixture):
    """With an explicitly-configured operator namespace, the
    ensure-operator-namespaces surface renders: the release-namespace
    ServiceAccount, ClusterRole, ClusterRoleBinding, script/payload
    ConfigMap, and the pre-install/pre-upgrade hook Job. Operators without an
    explicit namespace install into the shared openshift-operators namespace,
    which the hook deliberately never touches."""

    def setUp(self):
        super().setUp()
        self.default = self.by_kind(self.render(self.hook_operator()))

    def test_hook_surface_gates_on_explicit_namespace(self):
        names = {d["metadata"]["name"]
                 for kind in ("ServiceAccount", "ClusterRole",
                              "ClusterRoleBinding", "ConfigMap", "Job")
                 for d in self.default.get(kind, [])}
        self.assertIn("ensure-operator-namespaces", names)
        # An operator without an explicit namespace installs into the shared
        # openshift-operators namespace, which the hook never touches — but
        # the Subscription hook surface still renders there.
        empty = self.by_kind(self.render(
            {"operators": {"default-ns-operator": {"channel": "stable"}}}))
        names = {d["metadata"]["name"]
                 for kind in ("ServiceAccount", "ClusterRole",
                              "ClusterRoleBinding", "ConfigMap", "Job")
                 for d in empty.get(kind, [])}
        self.assertNotIn("ensure-operator-namespaces", names)
        self.assertIn("install-default-ns-operator-set", names)

    def test_disabled_operator_renders_no_namespace_surface(self):
        empty = self.by_kind(self.render(
            self.hook_operator(enabled=False)))
        for kind in ("ServiceAccount", "ClusterRole", "ClusterRoleBinding",
                     "Job"):
            self.assertNotIn(kind, empty)

    def test_job_is_pre_install_pre_upgrade_hook(self):
        job = self.pick(self.default, "Job", "ensure-operator-namespaces")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "pre-install,pre-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        # Default weight 0: after the -10 delivery hooks (below).
        self.assertNotIn("helm.sh/hook-weight",
                         job["metadata"]["annotations"])
        self.assertEqual(job["spec"]["backoffLimit"], 4)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"],
                         "ensure-operator-namespaces")
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(container["command"],
                         ["/mnt/ensure-namespaces.sh"])
        self.assertEqual(
            container["image"],
            "image-registry.openshift-image-registry.svc:5000/openshift/tools"
            ":latest")
        self.assertEqual(pod["volumes"], [{
            "name": "ensure-operator-namespaces-script",
            "configMap": {"name": "ensure-operator-namespaces",
                          "defaultMode": 493},
        }])

    def test_delivery_objects_are_weighted_pre_hooks(self):
        # Pre-install hooks are created before release resources, so the
        # Job's delivery objects must be hooks themselves at a lower weight
        # than the Job's default 0 — a fresh install fails pod admission on
        # a missing ServiceAccount otherwise. hook-succeeded cleans them up
        # after each successful run; before-hook-creation removes them after
        # a failed run so the retry never hits AlreadyExists.
        for kind in ("ServiceAccount", "ClusterRole", "ClusterRoleBinding",
                     "ConfigMap"):
            obj = self.pick(self.default, kind,
                            "ensure-operator-namespaces")
            annotations = obj["metadata"]["annotations"]
            self.assertEqual(annotations["helm.sh/hook"],
                             "pre-install,pre-upgrade")
            self.assertEqual(annotations["helm.sh/hook-weight"], "-10")
            self.assertEqual(
                annotations["helm.sh/hook-delete-policy"],
                "before-hook-creation,hook-succeeded")

    def test_cluster_role_scopes_everything_but_create(self):
        # `create` cannot be resourceName-scoped — the documented RBAC
        # exception; every mutating read-back verb is scoped to the managed
        # namespace names.
        role = self.pick(self.default, "ClusterRole",
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

    def test_cluster_role_binds_only_the_hook_service_account(self):
        binding = self.pick(self.default, "ClusterRoleBinding",
                            "ensure-operator-namespaces")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "ensure-operator-namespaces",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "ensure-operator-namespaces",
            "namespace": "test-release-ns",
        }])

    def test_configmap_delivers_scripts_and_payload(self):
        cm = self.pick(self.default, "ConfigMap",
                       "ensure-operator-namespaces")
        for key in ("hook-preamble.sh", "ensure-namespaces.sh",
                    "namespaces.json"):
            self.assertIn(key, cm["data"])
        payload = json.loads(cm["data"]["namespaces.json"])
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["name"], "test-operators")
        self.assertEqual(payload[0]["labels"], {})
        self.assertEqual(payload[0]["annotations"], {})
        self.assertIn("helm.sh/chart", payload[0]["chartLabels"])

    def test_shared_namespace_dedupes_into_one_entry(self):
        values = self.hook_operator()
        values["operators"]["other-operator"] = dict(HOOK_OPERATOR,
                                                     channel="stable")
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "ensure-operator-namespaces")
        payload = json.loads(cm["data"]["namespaces.json"])
        self.assertEqual([entry["name"] for entry in payload],
                         ["test-operators"])

    def test_distinct_namespaces_render_sorted_entries(self):
        values = self.hook_operator()
        values["operators"]["other-operator"] = dict(HOOK_OPERATOR,
                                                     namespace="aaa-operators",
                                                     channel="stable")
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "ensure-operator-namespaces")
        payload = json.loads(cm["data"]["namespaces.json"])
        self.assertEqual([entry["name"] for entry in payload],
                         ["aaa-operators", "test-operators"])

    def test_configured_labels_and_annotations_reach_the_payload(self):
        values = self.hook_operator(
            namespaceLabels={"monitoring": "true"},
            namespaceAnnotations={"owner": "platform-team"})
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "ensure-operator-namespaces")
        payload = json.loads(cm["data"]["namespaces.json"])
        self.assertEqual(payload[0]["labels"], {"monitoring": "true"})
        self.assertEqual(payload[0]["annotations"],
                         {"owner": "platform-team"})

    def test_resource_names_follow_the_deduped_namespace_set(self):
        values = self.hook_operator()
        values["operators"]["other-operator"] = dict(HOOK_OPERATOR,
                                                     namespace="aaa-operators",
                                                     channel="stable")
        role = self.pick(self.by_kind(self.render(values)), "ClusterRole",
                         "ensure-operator-namespaces")
        self.assertEqual(role["rules"][1]["resourceNames"],
                         ["aaa-operators", "test-operators"])


class NamespaceConflictTests(ChartFixture):
    """Two entries sharing a namespace with conflicting configured metadata
    fail the render naming both entries and the key (the F14 dedupe guard)."""

    def conflicting(self, a_key="monitoring", b_key="monitoring",
                    surface="namespaceLabels"):
        values = self.hook_operator(**{surface: {a_key: "on"}})
        values["operators"]["other-operator"] = dict(
            HOOK_OPERATOR, channel="stable", **{surface: {b_key: "off"}})
        return values

    def test_conflicting_labels_fail_the_render(self):
        stderr = self.render_failure(self.conflicting())
        self.assertIn('"test-operator"', stderr)
        self.assertIn('"other-operator"', stderr)
        self.assertIn('share namespace "test-operators"', stderr)
        self.assertIn('conflicting namespaceLabels for key "monitoring"',
                      stderr)
        # Both conflicting values appear (iteration order over the operator
        # map determines which side is quoted first).
        self.assertIn('"on"', stderr)
        self.assertIn('"off"', stderr)

    def test_conflicting_annotations_fail_the_render(self):
        stderr = self.render_failure(
            self.conflicting(surface="namespaceAnnotations"))
        self.assertIn("namespaceAnnotations", stderr)

    def test_conflicting_keys_are_per_key(self):
        # Different keys on the same namespace merge instead of conflicting.
        values = self.conflicting(a_key="monitoring", b_key="other")
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "ensure-operator-namespaces")
        payload = json.loads(cm["data"]["namespaces.json"])
        self.assertEqual(payload[0]["labels"],
                         {"monitoring": "on", "other": "off"})

    def test_identical_values_dedupe_cleanly(self):
        values = self.hook_operator(
            namespaceLabels={"monitoring": "true"})
        values["operators"]["other-operator"] = dict(
            HOOK_OPERATOR, channel="stable",
            namespaceLabels={"monitoring": "true"})
        rendered = self.render(values)
        cm = self.pick(self.by_kind(rendered), "ConfigMap",
                       "ensure-operator-namespaces")
        self.assertEqual(
            json.loads(cm["data"]["namespaces.json"])[0]["labels"],
            {"monitoring": "true"})


class SubscriptionOwnershipTests(ChartFixture):
    """The Subscription is hook-maintained by default (the one-shot apply Job
    left the Subscription drifting after its first run) and release-owned
    only under ownSubscription: true."""

    def setUp(self):
        super().setUp()
        self.default = self.by_kind(self.render(self.hook_operator()))

    def test_default_renders_no_subscription_manifest(self):
        self.assertNotIn("Subscription", self.default)

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

    def test_explicit_false_matches_the_default(self):
        explicit = self.by_kind(self.render(
            self.hook_operator(ownSubscription=False)))
        self.assertNotIn("Subscription", explicit)
        self.assertIn("Job", explicit)

    def test_hook_surface_names_and_hooks(self):
        for name, hook in (("install-test-operator-set",
                            "post-install,post-upgrade"),
                           ("install-test-operator-remove", "pre-delete")):
            job = self.pick(self.default, "Job", name,
                            namespace="test-operators")
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook"], hook)
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
                "before-hook-creation")
            pod = job["spec"]["template"]["spec"]
            self.assertEqual(pod["serviceAccountName"],
                             "install-test-operator")
        set_container = self.pick(
            self.default, "Job", "install-test-operator-set",
            namespace="test-operators")["spec"]["template"]["spec"][
                "containers"][0]
        self.assertEqual(set_container["command"],
                         ["/mnt/subscription-set.sh"])

    def test_role_is_scoped_except_create(self):
        role = self.pick(self.default, "Role", "install-test-operator",
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

    def test_configmap_delivers_scripts_and_payload(self):
        cm = self.pick(self.default, "ConfigMap", "install-test-operator-config",
                       namespace="test-operators")
        for key in ("hook-preamble.sh", "subscription-set.sh",
                    "subscription-remove.sh", "subscription.json"):
            self.assertIn(key, cm["data"])

    def test_payload_carries_the_managed_spec_by_value(self):
        values = self.hook_operator(
            installPlanApproval="Manual",
            startingCSV="test-operator.v1.0.0",
            catalog="certified-operators",
            env={"B": "2", "A": "1"})
        cm = self.pick(
            self.by_kind(self.render(values)), "ConfigMap",
            "install-test-operator-config", namespace="test-operators")
        payload = json.loads(cm["data"]["subscription.json"])
        self.assertEqual(payload["name"], "test-operator")
        self.assertEqual(payload["namespace"], "test-operators")
        self.assertEqual(payload["spec"], {
            "channel": "stable",
            "installPlanApproval": "Manual",
            "name": "test-operator",
            "source": "certified-operators",
            "sourceNamespace": "openshift-marketplace",
            "startingCSV": "test-operator.v1.0.0",
            "config": {"env": [{"name": "A", "value": "1"},
                               {"name": "B", "value": "2"}]},
        })

    def test_payload_converges_unconfigured_optionals_to_null(self):
        cm = self.pick(self.default, "ConfigMap",
                       "install-test-operator-config",
                       namespace="test-operators")
        payload = json.loads(cm["data"]["subscription.json"])
        self.assertIsNone(payload["spec"]["startingCSV"])
        self.assertIsNone(payload["spec"]["config"]["env"])
        # The create manifest omits them, matching the release-owned shape.
        self.assertNotIn("startingCSV", payload["manifest"]["spec"])
        self.assertNotIn("config", payload["manifest"]["spec"])

    def test_create_manifest_carries_proof_annotation_and_labels(self):
        cm = self.pick(self.default, "ConfigMap",
                       "install-test-operator-config",
                       namespace="test-operators")
        manifest = json.loads(cm["data"]["subscription.json"])["manifest"]
        self.assertEqual(manifest["apiVersion"],
                         "operators.coreos.com/v1alpha1")
        self.assertEqual(manifest["kind"], "Subscription")
        self.assertEqual(
            manifest["metadata"]["annotations"][PROOF_ANNOTATION], "true")
        self.assertEqual(manifest["metadata"]["labels"]["helm.sh/chart"],
                         "install-operators-0.3.2")

    def test_disabled_operator_renders_no_subscription_surface(self):
        empty = self.by_kind(self.render(self.hook_operator(enabled=False)))
        for kind in ("Role", "RoleBinding", "Job"):
            self.assertNotIn(kind, empty)

    def test_chart_metadata_is_in_lockstep(self):
        with open(os.path.join(CHART, "Chart.yaml")) as f:
            meta = yaml.safe_load(f)
        self.assertEqual(meta["version"], "0.3.2")
        self.assertEqual(meta["appVersion"], "0.3.2")


class HookOrderingTests(ChartFixture):
    """Helm executes hooks in ascending weight order; within a weight, order
    is the stable file-path order the manifests were collected in
    (pkg/action/hooks.go sorts stably by weight only, over hooks gathered in
    manifest file-path order by pkg/releaseutil/manifest_sorter.go) — there
    is no same-weight name tiebreak. At equal weight 0, the approve Jobs in
    install-plan-approvals.yaml sorted before the set Jobs in
    subscriptions.yaml purely by file-path order, so under
    installPlanApproval: Manual an approve Job polled forever for an
    InstallPlan the not-yet-created Subscription would never produce. The
    approve Job therefore runs at weight 1, which no file-path order can
    outrank."""

    MANUAL_OPERATOR = {
        "channel": "stable",
        "namespace": "test-operators",
        "installPlanApproval": "Manual",
        "startingCSV": "test-operator.v1.0.0",
    }

    def test_approve_job_runs_after_the_subscription_set_job(self):
        values = {"operators": {
            "test-operator": dict(self.MANUAL_OPERATOR),
            "plain-operator": {"channel": "stable",
                               "namespace": "test-operators"},
        }}
        by_kind = self.by_kind(self.render(values))
        approve = self.pick(by_kind, "Job", "approve-test-operator",
                            namespace="test-operators")
        self.assertEqual(
            approve["metadata"]["annotations"]["helm.sh/hook-weight"], "1")
        for job in by_kind["Job"]:
            if job["metadata"]["name"].endswith("-set"):
                self.assertNotIn("helm.sh/hook-weight",
                                 job["metadata"]["annotations"],
                                 job["metadata"]["name"])

    def test_pre_delete_hooks_carry_no_weight(self):
        # Remove-side ordering: the pre-delete remove Jobs are the only
        # pre-delete hooks, have no ordering dependency among themselves,
        # and must all run at the default weight 0.
        values = {"operators": {
            "test-operator": dict(self.MANUAL_OPERATOR),
            "plain-operator": {"channel": "stable",
                               "namespace": "test-operators"},
        }}
        for job in self.by_kind(self.render(values))["Job"]:
            if job["metadata"]["annotations"]["helm.sh/hook"] == "pre-delete":
                self.assertNotIn("helm.sh/hook-weight",
                                 job["metadata"]["annotations"],
                                 job["metadata"]["name"])


class DocumentIntegrityTests(ChartFixture):
    """Regression guard for the 0.3.0 YAML-fusion defect: a right-trim
    marker on the hook-branch comment in subscriptions.yaml ate the newline
    before each range iteration's leading `---`, fusing the previous
    operator's documents into one (the fused-out remove Job silently vanished
    from the release and the SSA apply failed on the merged document). The
    guard renders the fusion-prone orderings and asserts the documents stay
    separate, nothing vanishes, and delivery objects never gain foreign
    top-level fields."""

    # No explicit namespace: without the ensure-namespaces surface the
    # document counts are exactly the per-operator surfaces.
    HOOK = {"channel": "stable"}
    OWN = {"channel": "stable", "ownSubscription": True}

    def render_ordering(self, first, second):
        values = {"operators": {"op-one": first, "op-two": second}}
        raw = self.render_raw(values).stdout
        # No document separator may be glued to content: a line ending in
        # `<non-space>---` is the signature of the trim-marker fusion.
        for line in raw.splitlines():
            self.assertNotRegex(line, r"\S---$", raw)
        return [d for d in docs(raw) if d is not None]

    def assert_delivery_objects_clean(self, rendered):
        # The 0.3.0 fusion merged the following document's apiVersion/kind
        # into the previous document (duplicate top-level keys, last-wins),
        # which surfaced as a `spec` on a ServiceAccount at SSA time.
        for doc in rendered:
            if doc["kind"] == "ServiceAccount":
                self.assertEqual(set(doc), {"apiVersion", "kind", "metadata"},
                                 doc["metadata"]["name"])
            elif doc["kind"] == "Role":
                self.assertEqual(set(doc), {"apiVersion", "kind", "metadata",
                                            "rules"}, doc["metadata"]["name"])
            elif doc["kind"] == "RoleBinding":
                self.assertEqual(set(doc), {"apiVersion", "kind", "metadata",
                                            "roleRef", "subjects"},
                                 doc["metadata"]["name"])

    def test_hook_then_hook_renders_all_twelve_documents(self):
        rendered = self.render_ordering(self.HOOK, self.HOOK)
        # Exact document count: a fusion silently removes objects from the
        # release (the 0.3.0 defect merged each remove Job into the next
        # operator's ServiceAccount document).
        self.assertEqual(len(rendered), 12)
        names = {d["metadata"]["name"] for d in rendered}
        # The silently-vanished object class: both operators' pre-delete
        # remove Jobs must be present in full.
        for op in ("op-one", "op-two"):
            self.assertIn(f"install-{op}-remove", names)
            self.assertIn(f"install-{op}-set", names)
        self.assert_delivery_objects_clean(rendered)

    def test_own_then_hook_renders_all_seven_documents(self):
        rendered = self.render_ordering(self.OWN, self.HOOK)
        self.assertEqual(len(rendered), 7)
        names = {d["metadata"]["name"] for d in rendered}
        self.assertIn("install-op-two-remove", names)
        self.assertIn("op-one", names)  # the release-owned Subscription
        self.assert_delivery_objects_clean(rendered)

    def test_own_then_own_renders_exactly_two_subscriptions(self):
        rendered = self.render_ordering(self.OWN, self.OWN)
        self.assertEqual(len(rendered), 2)
        names = {d["metadata"]["name"] for d in rendered}
        self.assertEqual(names, {"op-one", "op-two"})
        for doc in rendered:
            self.assertEqual(doc["kind"], "Subscription")

    def test_hook_then_own_renders_all_seven_documents(self):
        rendered = self.render_ordering(self.HOOK, self.OWN)
        self.assertEqual(len(rendered), 7)
        names = {d["metadata"]["name"] for d in rendered}
        self.assertIn("install-op-one-remove", names)
        self.assertIn("install-op-one-set", names)
        self.assertIn("op-two", names)  # the release-owned Subscription
        self.assert_delivery_objects_clean(rendered)


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
if [[ "$1" == "get" ]]; then
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
  for arg in "$@"; do
    if [[ "$arg" == --patch=* ]]; then
      printf '%s\\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
    fi
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
    """Smoke run for the rendered namespace-ensure hook script: the
    happy-path create of an absent namespace."""

    get_file_name = "namespace.json"

    def setUp(self):
        super().setUp()
        cm = self.render_config(self.hook_operator())
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "ensure-namespaces.sh")}
        self.payload = cm["data"]["namespaces.json"]
        self.script_json = os.path.join(self.work, "namespaces.json")
        with open(self.script_json, "w") as f:
            f.write(self.payload)

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
        self.assertIn("helm.sh/chart", manifest["metadata"]["labels"])
        self.assertEqual(self.patches(), [])

    def test_merge_path_stamps_keep_only_on_provenance_namespaces(self):
        # Upgrading from a chart version that shipped Namespaces as release
        # manifests: the merge path stamps helm.sh/resource-policy: keep in
        # the same write as the configured keys, but only when the namespace
        # carries this release's provenance labels — the stamp is what stops
        # Helm's upgrade deletion pass from removing the namespace (and
        # cascading its contents).
        cm = self.render_config(self.hook_operator(
            namespaceLabels={"monitoring": "true"}))
        scripts = {name: cm["data"][name]
                   for name in ("hook-preamble.sh", "ensure-namespaces.sh")}
        with open(self.script_json, "w") as f:
            f.write(cm["data"]["namespaces.json"])

        provenance = {"app.kubernetes.io/name": "install-operators",
                      "app.kubernetes.io/instance": "test-release"}

        def canned(labels):
            return {"apiVersion": "v1", "kind": "Namespace",
                    "metadata": {"name": "test-operators",
                                 "resourceVersion": "421",
                                 "labels": labels}}

        result = self.run_script("ensure-namespaces.sh", scripts,
                                 canned_doc=canned(dict(provenance)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.patches()), 1)
        patch = self.patches()[0]
        self.assertEqual(patch["metadata"]["resourceVersion"], "421")
        self.assertEqual(patch["metadata"]["labels"], {"monitoring": "true"})
        self.assertEqual(patch["metadata"]["annotations"],
                         {"helm.sh/resource-policy": "keep"})

        # An already-converged 0.2.x namespace: the keep stamp is the only
        # change, so the patch must still be emitted for it.
        result = self.run_script(
            "ensure-namespaces.sh", scripts,
            canned_doc=canned(dict(provenance, monitoring="true")))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [{
            "metadata": {"resourceVersion": "421",
                         "annotations": {"helm.sh/resource-policy": "keep"}},
        }])

        # A namespace without the release's provenance labels is foreign —
        # the configured keys merge, the stamp never lands.
        result = self.run_script("ensure-namespaces.sh", scripts,
                                 canned_doc=canned({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        patch = self.patches()[0]
        self.assertEqual(patch["metadata"]["labels"], {"monitoring": "true"})
        self.assertNotIn("annotations", patch["metadata"])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("ensure-namespaces.sh", self.scripts,
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class SubscriptionScriptTests(FakeOcScriptFixture):
    """Smoke runs for the rendered Subscription hook scripts: the set hook
    converging an existing Subscription by value, and the remove hook
    deleting a proof-annotated Subscription."""

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

    def canned_subscription(self, annotations=None, **spec):
        doc = {
            "apiVersion": "operators.coreos.com/v1alpha1",
            "kind": "Subscription",
            "metadata": {"name": "test-operator",
                         "namespace": "test-operators",
                         "resourceVersion": "1597"},
            "spec": dict({
                "channel": "old-channel",
                "installPlanApproval": "Automatic",
                "name": "test-operator",
                "source": "redhat-operators",
                "sourceNamespace": "openshift-marketplace",
                "startingCSV": "test-operator.v0.9.0",
                "config": {"env": [{"name": "STALE", "value": "x"}]},
            }, **spec),
        }
        if annotations is not None:
            doc["metadata"]["annotations"] = annotations
        return doc

    def test_set_converges_an_existing_subscription_by_value(self):
        result = self.run_script("subscription-set.sh", self.scripts,
                                 canned_doc=self.canned_subscription())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log_lines()[0],
            "get -n test-operators subscriptions.operators.coreos.com/"
            "test-operator -o json --request-timeout=30s")
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


if __name__ == "__main__":
    unittest.main()


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
