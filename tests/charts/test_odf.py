"""Focused tests for the odf chart. The module covers the StorageCluster's
enableCephTools field surface (the operator-managed OCSInitialization CR is
never rendered by the chart — the field lives on the StorageCluster), the
platform-objects-never-render contracts (no Node manifests and no
OCSInitialization manifest for any values combination), the storage-node
labeling hook surface (post-install/post-upgrade set and pre-delete remove
Jobs gated on a non-empty disks list, resourceName-scoped Node RBAC, and the
rendered node list), and the disks-entry render validation.

Render tests run the chart through ``helm template`` (odf has no chart
dependencies, so no stubs or registry access are needed). Per the repo's
unit-test policy the script-execution layer is a minimal smoke set: ONE
happy-path run per hook script (on a python3-only restricted PATH built
entirely from symlinks, so neither a real ``oc`` nor the test host's
ambient tools can leak into the run) and ONE fail-closed no-tool check for
the chart. State-machine behavior (409 retries, foreign-value STOP,
walk-back proof, node-wait timeout) is proven by live-cluster validation,
not fixtures.
"""

import os
import unittest

from support import docs
import support

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "odf"))

STORAGE_DEVICE_SETS = [{
    "name": "ocs-deviceset",
    "dataPVCTemplate": {
        "spec": {
            "storageClassName": "placeholder-csi",
            "accessModes": ["ReadWriteOnce"],
            "volumeMode": "Block",
            "resources": {"requests": {"storage": "1Ti"}},
        },
    },
    "replica": 3,
}]

DEVICE_SET_VALUES = {"storageDeviceSets": STORAGE_DEVICE_SETS}

DISKS_VALUES = {
    "monDataDirHostPath": "/mnt/odf-storage",
    "disks": [{"node": "worker-1"}],
}

STANDALONE_VALUES = {
    "enableCephTools": False,
    "multiCloudGateway": {
        "reconcileStrategy": "standalone",
        "dbStorageClassName": "placeholder-csi",
    },
}

LABEL_KEY = "cluster.ocs.openshift.io/openshift-storage"
PROOF_ANNOTATION = "odf/label-proof"

# Fake oc for the hook-script smoke runs: serves a canned Node object for
# get and records patch payloads in both --patch=<value> and
# separate-argument --patch <value> forms. Each get bumps the served
# metadata.resourceVersion (first get serves the canned value, each later
# get serves it incremented), so a retry could only patch successfully with
# a version from a fresh read. Every invocation is logged so tests can
# assert the exact oc call surface.
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
  prev=""
  for arg in "$@"; do
    if [[ "$arg" == --patch=* ]]; then
      printf '%s\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
    elif [[ "$prev" == "--patch" ]]; then
      printf '%s\n' "$arg" >> "$OC_PATCH_LOG"
    fi
    prev="$arg"
  done
  exit 0
else
  echo "unexpected oc invocation: $*" >&2
  exit 64
fi
"""


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "odf-test-"
    values_prefix = "odf-values-"
    # Every chart object pins openshift-storage, so renders default there
    release_namespace = "openshift-storage"

    def label_hook_names(self, by_kind):
        return {d["metadata"]["name"]
                for kind in ("Job", "ServiceAccount", "ClusterRole",
                             "ClusterRoleBinding", "ConfigMap")
                for d in by_kind.get(kind, [])
                if d["metadata"]["name"].startswith("label-storage-nodes")}


class StorageClusterValuesTests(ChartFixture):
    """The enableCephTools knob lands on the StorageCluster's spec: explicit
    true and explicit false are both representable, for every mode."""

    def test_default_true_is_applied_by_value(self):
        cluster = self.pick(self.by_kind(self.render(DEVICE_SET_VALUES)),
                            "StorageCluster", "ocs-storagecluster")
        self.assertIs(cluster["spec"]["enableCephTools"], True)

    def test_explicit_false_is_applied_by_value(self):
        values = {"enableCephTools": False, **DEVICE_SET_VALUES}
        cluster = self.pick(self.by_kind(self.render(values)),
                            "StorageCluster", "ocs-storagecluster")
        self.assertIs(cluster["spec"]["enableCephTools"], False)

    def test_standalone_mode_applies_false(self):
        cluster = self.pick(self.by_kind(self.render(STANDALONE_VALUES)),
                            "StorageCluster", "ocs-storagecluster")
        self.assertIs(cluster["spec"]["enableCephTools"], False)


class PlatformObjectsNeverRenderTests(ChartFixture):
    """Nodes pre-exist on every cluster and OCSInitialization is created and
    managed by the ocs-operator — neither may ever render as a release-owned
    manifest, whatever the values."""

    def test_node_and_ocsinitialization_never_render(self):
        for values in (DEVICE_SET_VALUES,
                       {"enableCephTools": False, **DEVICE_SET_VALUES},
                       DISKS_VALUES,
                       STANDALONE_VALUES):
            by_kind = self.by_kind(self.render(values))
            self.assertNotIn("Node", by_kind, values)
            self.assertNotIn("OCSInitialization", by_kind, values)

    def test_no_node_label_text_outside_hook_scripts(self):
        # The only sanctioned carrier of the label key is the hook script
        # ConfigMap content; release manifests (StorageCluster, RBAC, Jobs)
        # must not carry Node-shaped fragments.
        raw = self.render_stdout(DISKS_VALUES)
        for doc in docs(raw):
            if doc["kind"] == "ConfigMap":
                continue
            self.assertNotIn("kind: Node", raw)


class NodeLabelHookSurfaceTests(ChartFixture):
    """With disks configured, the set/remove hook Jobs, ServiceAccount,
    ClusterRole, ClusterRoleBinding, and script ConfigMap render with exact
    hooks, RBAC scoping, and the rendered node list; without disks, the
    whole surface is absent."""

    def test_no_disks_renders_no_label_surface(self):
        by_kind = self.by_kind(self.render(DEVICE_SET_VALUES))
        self.assertEqual(self.label_hook_names(by_kind), set())

    def test_disks_render_full_label_surface(self):
        by_kind = self.by_kind(self.render(DISKS_VALUES))
        for name in ("label-storage-nodes", "label-storage-nodes-set",
                     "label-storage-nodes-remove"):
            self.assertIn(name, self.label_hook_names(by_kind))
        jobs = [j for j in by_kind["Job"]
                if j["metadata"]["name"].startswith("label-storage-nodes")]
        self.assertEqual(sorted(j["metadata"]["name"] for j in jobs),
                         ["label-storage-nodes-remove",
                          "label-storage-nodes-set"])

    def test_set_job_is_post_install_post_upgrade_hook(self):
        job = self.pick(self.by_kind(self.render(DISKS_VALUES)), "Job",
                        "label-storage-nodes-set")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "label-storage-nodes")
        self.assertEqual(pod["restartPolicy"], "Never")
        container = pod["containers"][0]
        self.assertEqual(container["command"], ["/bin/bash"])
        self.assertEqual(container["args"],
                         ["/app/label-storage-nodes-set.sh"])
        self.assertEqual(container["env"],
                         [{"name": "NODE_WAIT_TIMEOUT", "value": "300"}])
        self.assertEqual(pod["volumes"], [{
            "name": "label-storage-nodes",
            "configMap": {"name": "label-storage-nodes",
                          "defaultMode": 493},
        }])

    def test_remove_job_is_pre_delete_hook(self):
        job = self.pick(self.by_kind(self.render(DISKS_VALUES)), "Job",
                        "label-storage-nodes-remove")
        self.assertEqual(job["metadata"]["annotations"]["helm.sh/hook"],
                         "pre-delete")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(
            job["spec"]["template"]["spec"]["containers"][0]["args"],
            ["/app/label-storage-nodes-remove.sh"])

    def test_rbac_is_get_and_patch_on_named_nodes_only(self):
        values = {"monDataDirHostPath": "/mnt/odf-storage",
                  "disks": [{"node": "worker-1"}, {"node": "worker-2"}]}
        by_kind = self.by_kind(self.render(values))
        role = self.pick(by_kind, "ClusterRole", "label-storage-nodes")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""],
            "resources": ["nodes"],
            "resourceNames": ["worker-1", "worker-2"],
            "verbs": ["get", "patch"],
        }])
        binding = self.pick(by_kind, "ClusterRoleBinding",
                            "label-storage-nodes")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "label-storage-nodes",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "label-storage-nodes",
            "namespace": "openshift-storage",
        }])

    def test_configmap_delivers_scripts_and_node_list(self):
        by_kind = self.by_kind(self.render(DISKS_VALUES))
        cm = self.pick(by_kind, "ConfigMap", "label-storage-nodes")
        data = cm["data"]
        for key in ("hook-preamble.sh", "label-storage-nodes-set.sh",
                    "label-storage-nodes-remove.sh", "nodes.txt"):
            self.assertIn(key, data)
        for key in ("label-storage-nodes-set.sh",
                    "label-storage-nodes-remove.sh"):
            self.assertIn("set -euo pipefail", data[key])
            self.assertIn(LABEL_KEY, data[key])
        self.assertEqual(data["nodes.txt"].split(), ["worker-1"])


class NamespacePinningTests(ChartFixture):
    """The release namespace is free: every chart object pins
    openshift-storage outright, regardless of where the release installs —
    proven by rendering into a deliberately foreign release namespace."""

    def test_every_object_pins_openshift_storage(self):
        values = {"enableConsolePlugin": True, "createStorageSystem": True,
                  **DISKS_VALUES}
        by_kind = self.by_kind(self.render(values, namespace="foreign-ns"))
        for kind in ("StorageCluster", "Job",
                     "ServiceAccount", "ConfigMap"):
            for doc in by_kind.get(kind, []):
                self.assertEqual(doc["metadata"]["namespace"],
                                 "openshift-storage", (kind, doc))
        cluster = self.pick(by_kind, "StorageCluster", "ocs-storagecluster")
        self.assertEqual(cluster["metadata"]["namespace"], "openshift-storage")
        system = self.pick(by_kind, "StorageSystem",
                           "ocs-storagecluster-storagesystem")
        # Both the object's placement and its cross-reference to the
        # StorageCluster pin the canonical namespace
        self.assertEqual(system["spec"]["namespace"], "openshift-storage")
        binding = self.pick(by_kind, "ClusterRoleBinding",
                            "label-storage-nodes")
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "label-storage-nodes",
            "namespace": "openshift-storage",
        }])


class DisksValidationTests(ChartFixture):
    """The disks entries are validated at render: every entry needs a node
    name, and duplicate nodes fail instead of double-patching."""

    def test_entry_without_node_fails(self):
        stderr = self.render_failure(
            {"monDataDirHostPath": "/mnt/odf-storage", "disks": [{"node": ""}]})
        self.assertIn("carries no node name", stderr)

    def test_duplicate_node_fails(self):
        stderr = self.render_failure(
            {"monDataDirHostPath": "/mnt/odf-storage",
             "disks": [{"node": "worker-1"}, {"node": "worker-1"}]})
        self.assertIn("appears more than once", stderr)


class FakeOcScriptFixture(ChartFixture, support.FakeOcScriptFixture):
    """Restricted-PATH harness for the odf hook scripts; the canned oc
    program is this module's FAKE_OC_SCRIPT (which records patch payloads
    in both --patch=<value> and separate-argument --patch <value> forms).
    The hook scripts' wait loops need date on the tool path."""

    work_prefix = "odf-script-"
    bin_tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "date"]
    fake_oc_script = FAKE_OC_SCRIPT
    get_file_name = "node.json"

    def node_doc(self, labels=None, annotations=None,
                 resource_version="1597"):
        meta = {"name": "worker-1", "resourceVersion": resource_version}
        if labels:
            meta["labels"] = labels
        if annotations:
            meta["annotations"] = annotations
        return {"apiVersion": "v1", "kind": "Node", "metadata": meta}


class NodeLabelScriptTests(FakeOcScriptFixture):
    """Smoke set for the rendered node-label hook scripts: one happy-path
    run per script plus the chart's fail-closed no-tool check."""

    def setUp(self):
        super().setUp()
        by_kind = self.by_kind(self.render(DISKS_VALUES))
        cm = self.pick(by_kind, "ConfigMap", "label-storage-nodes")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "wait-for.sh",
                                     "label-storage-nodes-set.sh",
                                     "label-storage-nodes-remove.sh")}
        self.nodes_file = os.path.join(self.work, "nodes.txt")
        with open(self.nodes_file, "w") as f:
            f.write(cm["data"]["nodes.txt"])

    def canned_doc(self):
        return self.node_doc()

    def script_env(self):
        return {"NODES_FILE": self.nodes_file}

    def assert_get_argv(self):
        argv = self.log().splitlines()
        self.assertEqual(
            argv[0],
            "get node/worker-1 -o json --request-timeout=30s")

    def assert_patch_argv(self):
        for line in self.log().splitlines():
            if line.startswith("patch "):
                self.assertIn("patch node/worker-1 --type=merge", line)

    def test_set_success_on_unlabeled_node(self):
        result = self.run_script("label-storage-nodes-set.sh",
                                 self.node_doc())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_get_argv()
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        # Two gets serve the canned object (wait loop, then the
        # conditional-write read): the patch carries the second served
        # resourceVersion.
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1598",
                         "labels": {LABEL_KEY: ""},
                         "annotations": {PROOF_ANNOTATION: "true"}},
        })

    def test_remove_success_under_proof(self):
        result = self.run_script(
            "label-storage-nodes-remove.sh",
            self.node_doc(labels={LABEL_KEY: ""},
                          annotations={PROOF_ANNOTATION: "true"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_patch_argv()
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1598",
                         "labels": {LABEL_KEY: None},
                         "annotations": {PROOF_ANNOTATION: None}},
        })

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("label-storage-nodes-set.sh",
                                 self.node_doc(), include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class ConsolePluginScriptTests(FakeOcScriptFixture):
    """Smoke set for the rendered console-plugin enable script: the append
    path preserving a pre-existing plugin entry (the regression case for
    the list-init race that merge-patched the whole array to []), the
    create path for an absent plugins array, the no-op when the plugin is
    already enabled, and the fail-closed no-tool check. The script's
    patches are JSON patches guarded by a test operation on the read
    resourceVersion, so a concurrent writer's entry can never be lost."""

    get_file_name = "console.json"

    def setUp(self):
        super().setUp()
        by_kind = self.by_kind(self.render(
            {"enableConsolePlugin": True, **DEVICE_SET_VALUES}))
        cm = self.pick(by_kind, "ConfigMap", "enable-odf-console-plugin")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "enable-console-plugin.sh")}

    def console_doc(self, plugins=None, resource_version="1597"):
        meta = {"name": "cluster", "resourceVersion": resource_version}
        spec = {} if plugins is None else {"plugins": plugins}
        return {"apiVersion": "operator.openshift.io/v1", "kind": "Console",
                "metadata": meta, "spec": spec}

    def canned_doc(self):
        return self.console_doc()

    def script_env(self):
        return {"PLUGIN_NAME": "odf-console"}

    def assert_test_op(self, patch, resource_version="1597"):
        self.assertEqual(patch[0], {
            "op": "test", "path": "/metadata/resourceVersion",
            "value": resource_version,
        })

    def test_appends_after_existing_entry(self):
        # The clobber regression: a pre-existing foreign entry must survive
        # as the first element while the ODF plugin appends after it — the
        # old script's substring match made any foreign entry containing
        # the ODF plugin's name as a substring suppress the append.
        result = self.run_script("enable-console-plugin.sh",
                                 self.console_doc(["other-plugin"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assert_test_op(patches[0])
        self.assertEqual(patches[0][1:], [
            {"op": "add", "path": "/spec/plugins/-", "value": "odf-console"},
        ])

    def test_creates_array_when_absent(self):
        # The create path is a single atomic add carrying the plugin — no
        # empty-array intermediate a concurrent writer could be lost to.
        result = self.run_script("enable-console-plugin.sh",
                                 self.console_doc())
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assert_test_op(patches[0])
        self.assertEqual(patches[0][1:], [
            {"op": "add", "path": "/spec/plugins",
             "value": ["odf-console"]},
        ])

    def test_present_plugin_is_a_no_op(self):
        result = self.run_script("enable-console-plugin.sh",
                                 self.console_doc(["other-plugin",
                                                   "odf-console"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already enabled", result.stdout)
        self.assertEqual(self.patches(), [])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("enable-console-plugin.sh",
                                 self.console_doc(), include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class ConsolePluginSanityTests(ChartFixture):
    """The console-plugin surface is release-owned and unchanged by the
    hook rework: it still gates on enableConsolePlugin and keeps its
    post-install/post-upgrade hook shape."""

    def test_console_plugin_job_unchanged(self):
        by_kind = self.by_kind(self.render(DEVICE_SET_VALUES))
        job = self.pick(by_kind, "Job", "enable-odf-console-plugin")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(
            job["spec"]["template"]["spec"]["containers"][0]["args"],
            ["/app/enable-console-plugin.sh"])


if __name__ == "__main__":
    unittest.main()


class HookGuardTests(ChartFixture):
    """Every hook Job container carries the guard-without-pin
    securityContext: the four restricted-pod guards and no
    runAsUser/runAsGroup anywhere in the pod — restricted SCCs assign the
    namespace range UID to UID-less pods, and a pin would fight that
    platform assignment."""

    def test_every_hook_job_is_guarded_without_pin(self):
        jobs = support.hook_jobs(self.render(DISKS_VALUES))
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in jobs),
            ["enable-odf-console-plugin", "label-storage-nodes-remove",
             "label-storage-nodes-set"])
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])
