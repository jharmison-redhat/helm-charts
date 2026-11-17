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

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

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


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


class ChartFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="odf-test-")
        shutil.copytree(CHART, self.dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("charts", "Chart.lock"))
        self.addCleanup(shutil.rmtree, self.dir)

    def values_file(self, values):
        fd, path = tempfile.mkstemp(prefix="odf-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(values, f)
        self.addCleanup(os.unlink, path)
        return path

    def render_raw(self, values=None, namespace="openshift-storage"):
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", namespace]
        if values is not None:
            cmd += ["--values", self.values_file(values)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def render(self, values=None, namespace="openshift-storage"):
        return [d for d in docs(self.render_raw(values, namespace=namespace))
                if d is not None]

    def render_failure(self, values):
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", "openshift-storage",
               "--values", self.values_file(values)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        return result.stderr

    @staticmethod
    def by_kind(rendered):
        by_kind = {}
        for d in rendered:
            by_kind.setdefault(d["kind"], []).append(d)
        return by_kind

    @staticmethod
    def pick(by_kind, kind, name):
        matches = [d for d in by_kind[kind] if d["metadata"]["name"] == name]
        assert len(matches) == 1, (kind, name, by_kind[kind])
        return matches[0]

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
        raw = self.render_raw(DISKS_VALUES)
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


class FakeOcScriptFixture(ChartFixture):
    """Shared plumbing for executing rendered hook scripts against a fake
    oc binary. Each run gets a restricted PATH built solely from symlinks
    in an isolated bin directory (the requested JSON tool, core utilities,
    and the fake oc), so no ambient host tool or real oc can be reached.
    The child environment is an explicit minimal set — no parent variables
    are inherited."""

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="odf-script-")
        self.addCleanup(shutil.rmtree, self.work)

    def build_bin_dir(self, include_python=True):
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for name in os.listdir(bin_dir):
            os.unlink(os.path.join(bin_dir, name))
        tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "date"]
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

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def patches(self):
        path = os.path.join(self.work, "patches.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f.read().splitlines() if line]

    def run_child(self, script_path, env):
        return subprocess.run(
            ["/bin/bash", script_path], capture_output=True, text=True,
            env=env, timeout=120)

    def node_doc(self, labels=None, annotations=None,
                 resource_version="1597"):
        meta = {"name": "worker-1", "resourceVersion": resource_version}
        if labels:
            meta["labels"] = labels
        if annotations:
            meta["annotations"] = annotations
        return {"apiVersion": "v1", "kind": "Node", "metadata": meta}

    def canned_doc(self):
        raise NotImplementedError

    def script_env(self):
        raise NotImplementedError

    def run_script(self, script_name, canned_doc=None,
                   include_python=True):
        # Single tool path for the smoke set: python3-only restricted PATH.
        bin_dir = self.build_bin_dir(include_python=include_python)
        # Every staged file lands in the same directory as the entry script
        # because the scripts source their shared preamble from their own
        # directory, mirroring the single ConfigMap mount.
        for name, content in self.scripts.items():
            with open(os.path.join(self.work, name), "w") as f:
                f.write(content)
        if canned_doc is None:
            canned_doc = self.canned_doc()
        get_file = os.path.join(self.work, "node.json")
        with open(get_file, "w") as f:
            json.dump(canned_doc, f)
        # Each run is self-contained: drop logs and the fake oc's
        # resourceVersion counter left by any earlier run.
        for stale in ("commands.log", "patches.log", get_file + ".rv"):
            stale_path = os.path.join(self.work, stale)
            if os.path.exists(stale_path):
                os.unlink(stale_path)
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            **self.script_env(),
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_GET_FILE": get_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
        }
        return self.run_child(os.path.join(self.work, script_name), env)


class NodeLabelScriptTests(FakeOcScriptFixture):
    """Smoke set for the rendered node-label hook scripts: one happy-path
    run per script plus the chart's fail-closed no-tool check."""

    def setUp(self):
        super().setUp()
        by_kind = self.by_kind(self.render(DISKS_VALUES))
        cm = self.pick(by_kind, "ConfigMap", "label-storage-nodes")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
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
    """Happy-path smoke run for the rendered console-plugin enable script.
    The canned Console cluster object carries the plugins section without
    the ODF plugin, so the script's add-op patch is the expected outcome."""

    def setUp(self):
        super().setUp()
        by_kind = self.by_kind(self.render(
            {"enableConsolePlugin": True, **DEVICE_SET_VALUES}))
        cm = self.pick(by_kind, "ConfigMap", "enable-odf-console-plugin")
        self.scripts = {"enable-console-plugin.sh":
                        cm["data"]["enable-console-plugin.sh"]}

    def canned_doc(self):
        return {
            "apiVersion": "operator.openshift.io/v1",
            "kind": "Console",
            "metadata": {"name": "cluster", "resourceVersion": "1597"},
            "spec": {"plugins": []},
        }

    def script_env(self):
        return {"PLUGIN_NAME": "odf-console"}

    def test_adds_plugin_when_absent(self):
        result = self.run_script("enable-console-plugin.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], [{
            "op": "add", "path": "/spec/plugins/-", "value": "odf-console",
        }])


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
