"""Focused tests for the odf chart. The module covers the default render's
core object inventory (the StorageCluster with its enableCephTools value,
the label-storage-nodes hook surface gated on the disks list, the
console-plugin hook gated on enableConsolePlugin, and the namespace
pinning), the platform-objects-never-render contracts (no Node manifests
and no OCSInitialization manifest — the field lives on the StorageCluster
and the CR is operator-managed), the disks-entry render validation, the
hook-script smoke set (one happy-path run per script plus the chart's
fail-closed no-tool check), and the guard-without-pin hook contract.

Render tests run the chart through ``helm template`` (odf has no chart
dependencies, so no stubs or registry access are needed). Per the repo's
unit-test policy the script-execution layer is that minimal smoke set, on a
python3-only restricted PATH built entirely from symlinks, so neither a
real ``oc`` nor the test host's ambient tools can leak into the run.
State-machine behavior (409 retries, foreign-value STOP, walk-back proof,
node-wait timeout) is proven by live-cluster validation, not fixtures.
"""

import os
import unittest

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
    def setUp(self):
        super().setUp()
        self.stage_real_dependency("common")

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


class DefaultRenderTests(ChartFixture):
    """One representative render of the chart's core object inventory:
    StorageCluster as the release manifest, the Node-label hook surface
    gated on the disks list, and every object pinning
    openshift-storage (the release namespace is free — proven by rendering
    into a deliberately foreign release namespace)."""

    def test_default_render_inventory(self):
        values = {"enableConsolePlugin": True, **DISKS_VALUES}
        by_kind = self.by_kind(self.render(values, namespace="foreign-ns"))
        # Platform-owned objects never render: Nodes pre-exist on every
        # cluster and OCSInitialization is created and managed by the
        # ocs-operator.
        self.assertNotIn("Node", by_kind)
        self.assertNotIn("OCSInitialization", by_kind)
        cluster = self.pick(by_kind, "StorageCluster", "ocs-storagecluster")
        self.assertIs(cluster["spec"]["enableCephTools"], True)
        self.assertEqual(cluster["metadata"]["namespace"], "openshift-storage")
        # The disks-configured hook surface renders in full.
        for name in ("label-storage-nodes", "label-storage-nodes-set",
                     "label-storage-nodes-remove"):
            self.assertIn(name, self.label_hook_names(by_kind))
        role = self.pick(by_kind, "ClusterRole", "label-storage-nodes")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""],
            "resources": ["nodes"],
            "resourceNames": ["worker-1"],
            "verbs": ["get", "patch"],
        }])
        job = self.pick(by_kind, "Job", "label-storage-nodes-set")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        remove = self.pick(by_kind, "Job", "label-storage-nodes-remove")
        self.assertEqual(
            remove["metadata"]["annotations"]["helm.sh/hook"], "pre-delete")
        # The console-plugin hook gates on enableConsolePlugin.
        self.pick(by_kind, "Job", "enable-odf-console-plugin")
        # Without disks configured, the whole label surface is absent.
        by_kind = self.by_kind(self.render(DEVICE_SET_VALUES))
        self.assertEqual(self.label_hook_names(by_kind), set())
        # Representative render-failure trap: a duplicate disks entry fails
        # the render instead of double-patching the node.
        self.render_failure(
            {"monDataDirHostPath": "/mnt/odf-storage",
             "disks": [{"node": "worker-1"}, {"node": "worker-1"}]})


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

    def test_set_success_on_unlabeled_node(self):
        result = self.run_script("label-storage-nodes-set.sh",
                                 self.node_doc())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log().splitlines()[0],
            "get node/worker-1 -o json --request-timeout=30s")
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
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1598",
                         "labels": {LABEL_KEY: None},
                         "annotations": {PROOF_ANNOTATION: None}},
        })

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        # The chart's fail-closed no-tool check.
        result = self.run_script("label-storage-nodes-set.sh",
                                 self.node_doc(), include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class ConsolePluginScriptTests(FakeOcScriptFixture):
    """Happy-path smoke for the rendered console-plugin enable script: the
    append path preserving a pre-existing plugin entry (the regression case
    for the list-init race that merge-patched the whole array to [])."""

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

    def test_appends_after_existing_entry(self):
        result = self.run_script("enable-console-plugin.sh",
                                 self.console_doc(["other-plugin"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        # The JSON patch is guarded by a test operation on the read
        # resourceVersion, and the pre-existing foreign entry survives as
        # the first element while the ODF plugin appends after it.
        self.assertEqual(patches[0], [
            {"op": "test", "path": "/metadata/resourceVersion",
             "value": "1597"},
            {"op": "add", "path": "/spec/plugins/-", "value": "odf-console"},
        ])


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


if __name__ == "__main__":
    unittest.main()
