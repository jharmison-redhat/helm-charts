"""Focused tests for the aws-nvidia-gpu-machinesets chart. The module covers
the per-zone MachineAutoscaler distribution (totals preserved exactly: the
first `total mod zoneCount` zones take `floor(total/zoneCount) + 1`, the rest
take `floor(total/zoneCount)`, independently for min and max), the
render-time `aws.az` membership trap (an `aws.az` outside `aws.azs` used to
silently leave every MachineSet at zero replicas), the autoscaling gate (the
ClusterAutoscaler/default singleton is never a release manifest — it is
configured by a post-install/upgrade set hook and walked back by a pre-delete
remove hook with record-and-restore semantics), the resourceName-scoped hook
RBAC (with the one documented unscoped `create` grant), and script execution
for both hooks against a fake ``oc`` on both JSON tool paths.

Render tests run the chart through ``helm template`` (the chart has no
dependencies). Script tests run the rendered hook scripts against a fake
``oc`` on a restricted PATH built entirely from symlinks, so neither a real
``oc`` nor the test host's ambient tools can leak into the run.
"""

import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts",
    "aws-nvidia-gpu-machinesets"))

PROOF_ANNOTATION = "aws-nvidia-gpu-machinesets/proof"

BASE_VALUES = {
    "cluster": {"infraID": "cluster-9w6jz"},
    "aws": {"region": "us-west-2",
            "azs": ["us-west-2a", "us-west-2b", "us-west-2c"],
            "ami": "ami-0123456789abcdef0"},
}

# A pre-existing ClusterAutoscaler with foreign configuration on every
# surface: foreign podPriorityThreshold and maxNodesTotal, cores limits, and
# an amd.com/gpu entry — none of which this chart manages.
FOREIGN_SPEC = {
    "podPriorityThreshold": -100,
    "maxNodesTotal": 150,
    "resourceLimits": {
        "cores": {"min": 8, "max": 100},
        "gpus": [{"type": "amd.com/gpu", "min": 0, "max": 2}],
    },
}

AMD_ENTRY = {"type": "amd.com/gpu", "min": 0, "max": 2}


def autoscaler_values(min_replicas=None, max_replicas=None, **extra):
    values = copy.deepcopy(BASE_VALUES)
    autoscaling = {"enabled": True}
    if min_replicas is not None:
        autoscaling["minReplicas"] = min_replicas
    # Default to the zone count: the render-time validation enforces the
    # MachineAutoscaler CRD minimum of maxReplicas >= 1 per zone, so the
    # shipped default of 1 only renders for a single-zone fixture
    if max_replicas is not None:
        autoscaling["maxReplicas"] = max_replicas
    else:
        autoscaling["maxReplicas"] = len(BASE_VALUES["aws"]["azs"])
    autoscaling.update(extra)
    values["autoscaling"] = autoscaling
    return values


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


class ChartFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ca-test-")
        shutil.copytree(CHART, self.dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("charts", "Chart.lock"))
        self.addCleanup(shutil.rmtree, self.dir)

    def values_file(self, values):
        fd, path = tempfile.mkstemp(prefix="ca-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(values, f)
        self.addCleanup(os.unlink, path)
        return path

    def render_raw(self, values=None):
        merged = copy.deepcopy(BASE_VALUES)
        if values is not None:
            for key, val in values.items():
                if isinstance(val, dict) and isinstance(merged.get(key), dict):
                    merged[key].update(val)
                else:
                    merged[key] = val
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", "openshift-machine-api",
               "--values", self.values_file(merged)]
        return subprocess.run(cmd, capture_output=True, text=True)

    def render(self, values=None):
        result = self.render_raw(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [d for d in docs(result.stdout) if d is not None]

    def render_failure(self, values):
        """Renders values expected to trip validation and returns stderr."""
        result = self.render_raw(values)
        self.assertNotEqual(result.returncode, 0,
                            "expected a validation failure, but rendering "
                            "succeeded")
        return result.stderr

    def by_kind(self, rendered):
        by_kind = {}
        for d in rendered:
            by_kind.setdefault(d["kind"], []).append(d)
        return by_kind

    @staticmethod
    def pick(by_kind, kind, name):
        matches = [d for d in by_kind[kind] if d["metadata"]["name"] == name]
        assert len(matches) == 1, (kind, name, by_kind.get(kind))
        return matches[0]

    def render_enabled(self, **kwargs):
        return self.by_kind(self.render(autoscaler_values(**kwargs)))


class GatingTests(ChartFixture):
    """The autoscaling gate: with autoscaling disabled nothing of the
    ClusterAutoscaler surface renders at all; with it enabled the hook pair
    renders and no ClusterAutoscaler release manifest exists."""

    def test_autoscaling_disabled_renders_no_hook_surface(self):
        by_kind = self.by_kind(self.render())
        for kind in ("ClusterAutoscaler", "MachineAutoscaler", "Job",
                     "ServiceAccount", "ClusterRole", "ClusterRoleBinding",
                     "ConfigMap"):
            self.assertNotIn(kind, by_kind)
        machinesets = by_kind["MachineSet"]
        self.assertEqual(len(machinesets), 3)
        # The chosen zone carries desiredReplicas; the others sit at zero
        replicas = {ms["metadata"]["name"]: ms["spec"].get("replicas")
                    for ms in machinesets}
        self.assertEqual(
            replicas,
            {"cluster-9w6jz-gpu-worker-us-west-2a": 0,
             "cluster-9w6jz-gpu-worker-us-west-2b": 0,
             "cluster-9w6jz-gpu-worker-us-west-2c": 1})

    def test_autoscaling_enabled_renders_hook_pair_and_no_manifest(self):
        by_kind = self.render_enabled()
        self.assertNotIn("ClusterAutoscaler", by_kind)
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in by_kind["Job"]),
            ["clusterautoscaler-remove", "clusterautoscaler-set"])
        self.assertEqual(len(by_kind["MachineAutoscaler"]), 3)
        # The autoscaler owns the replica count: no MachineSet renders one
        for ms in by_kind["MachineSet"]:
            self.assertNotIn("replicas", ms["spec"])

    def test_hook_job_shapes(self):
        by_kind = self.render_enabled()
        for name, hook in (("clusterautoscaler-set",
                            "post-install,post-upgrade"),
                           ("clusterautoscaler-remove", "pre-delete")):
            job = self.pick(by_kind, "Job", name)
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook"], hook)
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
                "before-hook-creation")
            self.assertEqual(job["spec"]["backoffLimit"], 4)
            pod = job["spec"]["template"]["spec"]
            self.assertEqual(pod["serviceAccountName"], "clusterautoscaler")
            self.assertEqual(pod["restartPolicy"], "Never")
            self.assertEqual(pod["securityContext"],
                             {"runAsNonRoot": True,
                              "seccompProfile": {"type": "RuntimeDefault"}})
            container = pod["containers"][0]
            self.assertEqual(container["command"], [f"/mnt/{name}.sh"])
            self.assertEqual(container["image"],
                             "image-registry.openshift-image-registry.svc:"
                             "5000/openshift/tools:latest")
            self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
            self.assertEqual(container["securityContext"],
                             {"capabilities": {"drop": ["ALL"]}})
            self.assertEqual(container["volumeMounts"],
                             [{"name": "clusterautoscaler-config",
                               "mountPath": "/mnt"}])
            self.assertEqual(pod["volumes"], [{
                "name": "clusterautoscaler-config",
                "configMap": {"name": "clusterautoscaler-config",
                              "defaultMode": 493},
            }])

    def test_service_account_renders_without_namespace(self):
        # The hook surface installs into the release namespace
        # (openshift-machine-api), which the release supplies
        sa = self.pick(self.render_enabled(), "ServiceAccount",
                       "clusterautoscaler")
        self.assertNotIn("namespace", sa["metadata"])

    def test_rbac_is_resource_name_scoped(self):
        by_kind = self.render_enabled()
        role = self.pick(by_kind, "ClusterRole", "clusterautoscaler")
        self.assertEqual(role["rules"], [
            {
                "apiGroups": ["autoscaling.openshift.io"],
                "resources": ["clusterautoscalers"],
                "resourceNames": ["default"],
                "verbs": ["get", "patch", "delete"],
            },
            {
                # create cannot be resourceName-scoped in RBAC; the set hook
                # must create the singleton when absent (README: unscoped-
                # create honesty)
                "apiGroups": ["autoscaling.openshift.io"],
                "resources": ["clusterautoscalers"],
                "verbs": ["create"],
            },
        ])
        binding = self.pick(by_kind, "ClusterRoleBinding", "clusterautoscaler")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "clusterautoscaler",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "clusterautoscaler",
            "namespace": "openshift-machine-api",
        }])

    def test_configmap_delivers_scripts_preamble_and_config(self):
        cm = self.pick(self.render_enabled(), "ConfigMap",
                       "clusterautoscaler-config")
        self.assertEqual(
            sorted(cm["data"]),
            ["autoscaler.json", "clusterautoscaler-remove.sh",
             "clusterautoscaler-set.sh", "hook-preamble.sh"])
        preamble = cm["data"]["hook-preamble.sh"]
        self.assertIn("conditional_write", preamble)
        self.assertIn("create_object", preamble)
        self.assertIn("delete_object", preamble)
        self.assertIn("MAX_ATTEMPTS=5", preamble)
        for script in ("clusterautoscaler-set.sh",
                       "clusterautoscaler-remove.sh"):
            self.assertIn("set -euo pipefail", cm["data"][script])
            self.assertIn('source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"',
                          cm["data"][script])
            self.assertIn(PROOF_ANNOTATION, cm["data"][script])
        self.assertIn("created concurrently", cm["data"]["clusterautoscaler-set.sh"])
        self.assertIn("deleted", cm["data"]["clusterautoscaler-remove.sh"])

    def test_config_follows_values(self):
        values = autoscaler_values(min_replicas=4, max_replicas=7)
        values["autoscaling"]["scaleDown"] = {
            "enabled": False, "unneededTime": "10m"}
        # helm deep-merges values from multiple -f files, so the passed
        # scaleDown keys override the shipped defaults and the rest carry
        # through
        scale_down = {"enabled": False, "unneededTime": "10m",
                      "delayAfterAdd": "30m", "delayAfterDelete": "5m",
                      "delayAfterFailure": "30s",
                      "utilizationThreshold": "0.7"}
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "clusterautoscaler-config")
        payload = json.loads(cm["data"]["autoscaler.json"])
        self.assertEqual(payload["config"], {
            "podPriorityThreshold": -10,
            "gpuEntry": {"type": "nvidia.com/gpu", "min": 4, "max": 7},
            "scaleDown": scale_down,
        })
        self.assertEqual(payload["manifest"], {
            "apiVersion": "autoscaling.openshift.io/v1",
            "kind": "ClusterAutoscaler",
            "metadata": {
                "name": "default",
                "annotations": {PROOF_ANNOTATION: '{"created":true}'},
            },
            "spec": {
                "podPriorityThreshold": -10,
                "resourceLimits": {"gpus": [
                    {"type": "nvidia.com/gpu", "min": 4, "max": 7}]},
                "scaleDown": scale_down,
            },
        })

    def test_config_omits_scale_down_when_empty(self):
        values = autoscaler_values()
        values["autoscaling"]["scaleDown"] = None
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "clusterautoscaler-config")
        payload = json.loads(cm["data"]["autoscaler.json"])
        self.assertNotIn("scaleDown", payload["config"])
        self.assertNotIn("scaleDown", payload["manifest"]["spec"])


class DistributionTests(ChartFixture):
    """The per-zone MachineAutoscaler distribution preserves the chart-level
    totals exactly."""

    def bounds(self, zones, min_replicas, max_replicas):
        values = autoscaler_values(min_replicas=min_replicas,
                                   max_replicas=max_replicas)
        values["aws"]["azs"] = zones
        by_kind = self.by_kind(self.render(values))
        autoscalers = {}
        for ma in by_kind["MachineAutoscaler"]:
            autoscalers[ma["spec"]["scaleTargetRef"]["name"]] = {
                "min": ma["spec"]["minReplicas"],
                "max": ma["spec"]["maxReplicas"],
            }
        return autoscalers

    def assert_totals(self, zones, min_replicas, max_replicas, expected_min,
                      expected_max):
        autoscalers = self.bounds(zones, min_replicas, max_replicas)
        mins = [autoscalers[f"cluster-9w6jz-gpu-worker-{az}"]["min"]
                for az in zones]
        maxes = [autoscalers[f"cluster-9w6jz-gpu-worker-{az}"]["max"]
                 for az in zones]
        self.assertEqual(mins, expected_min)
        self.assertEqual(maxes, expected_max)
        self.assertEqual(sum(mins), min_replicas)
        self.assertEqual(sum(maxes), max_replicas)

    def test_divisible_totals(self):
        self.assert_totals(["a", "b", "c"], 3, 6, [1, 1, 1], [2, 2, 2])

    def test_remainder_with_min_ne_max(self):
        # 4 and 7 over three zones: the first 4 % 3 zones take floor + 1
        self.assert_totals(["a", "b", "c"], 4, 7, [2, 1, 1], [3, 2, 2])

    def test_more_zones_than_replicas_fails_render(self):
        # The MachineAutoscaler CRD enforces maxReplicas >= 1 per zone, so a
        # max total below the zone count would render maxReplicas: 0 zones
        # the API server rejects — the render fails instead
        values = autoscaler_values(min_replicas=2, max_replicas=2)
        values["aws"]["azs"] = ["a", "b", "c", "d"]
        stderr = self.render_failure(values)
        self.assertIn("autoscaling.maxReplicas (2)", stderr)
        self.assertIn("aws.azs (4)", stderr)
        self.assertIn("maxReplicas >= 1", stderr)

    def test_max_replicas_equals_zone_count_renders_all_ones(self):
        # The minimum viable max total for the zone count: every zone takes
        # exactly 1
        self.assert_totals(["a", "b", "c"], 0, 3, [0, 0, 0], [1, 1, 1])

    def test_single_zone(self):
        self.assert_totals(["a"], 0, 1, [0], [1])

    def test_remainders_are_independent(self):
        # min and max distribute their own remainders: 5 and 8 over 3 zones
        self.assert_totals(["a", "b", "c"], 5, 8, [2, 2, 1], [3, 3, 2])


class ValidationTests(ChartFixture):
    """Render-time validation traps."""

    def test_az_outside_azs_fails_render(self):
        values = copy.deepcopy(BASE_VALUES)
        values["aws"]["az"] = "us-east-1"
        stderr = self.render_failure(values)
        self.assertIn("aws.az 'us-east-1' is not a member of aws.azs", stderr)
        self.assertIn("aws.azs", stderr)

    def test_az_member_renders(self):
        values = copy.deepcopy(BASE_VALUES)
        values["aws"]["az"] = "us-west-2b"
        by_kind = self.by_kind(self.render(values))
        chosen = self.pick(by_kind, "MachineSet",
                           "cluster-9w6jz-gpu-worker-us-west-2b")
        self.assertEqual(chosen["spec"]["replicas"], 1)

    def test_az_unset_defaults_to_last_zone(self):
        by_kind = self.by_kind(self.render())
        for name in ("us-west-2a", "us-west-2b", "us-west-2c"):
            ms = self.pick(by_kind, "MachineSet",
                           f"cluster-9w6jz-gpu-worker-{name}")
            expected = 1 if name == "us-west-2c" else 0
            self.assertEqual(ms["spec"].get("replicas", 0), expected)


# Fake oc for the hook scripts: serves the canned ClusterAutoscaler for get
# and records every patch payload, with per-scenario API behavior for get,
# patch, create, and delete. Each get bumps the served
# metadata.resourceVersion (first get serves the canned value, each later get
# serves it incremented), so a retry can only patch successfully with a
# version from a fresh read. Every invocation is logged.
FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
env | LC_ALL=C sort > "$OC_ENV"
if [[ "$1" == "get" ]]; then
  case "$OC_SCENARIO" in
    absent-object)
      echo 'Error from server (NotFound): "default" not found' >&2
      exit 1 ;;
    get-error)
      echo 'Error from server (Forbidden): is forbidden' >&2
      exit 1 ;;
    *)
      rv_file="$OC_GET_FILE.rv"
      if [[ -f "$rv_file" ]]; then
        next_rv=$(($(cat "$rv_file") + 1))
      else
        next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$OC_GET_FILE" | head -1 | grep -oE '[0-9]+')
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
  case "$OC_PATCH_SCENARIO" in
    conflict-then-success)
      if [ "$attempts" -ge 2 ]; then
        exit 0
      fi
      echo 'Error from server (Conflict): Operation cannot be fulfilled' >&2
      exit 1 ;;
    conflict-exhausted)
      echo 'Error from server (Conflict): Operation cannot be fulfilled' >&2
      exit 1 ;;
    patch-error)
      echo 'Error from server (Forbidden): is forbidden' >&2
      exit 1 ;;
    *)
      exit 0 ;;
  esac
elif [[ "$1" == "create" ]]; then
  cat > "$OC_CREATE_LOG"
  case "${OC_CREATE_SCENARIO:-ok}" in
    ok)
      exit 0 ;;
    already-exists)
      echo 'Error from server (AlreadyExists): clusterautoscalers "default" already exists' >&2
      exit 1 ;;
    error)
      echo 'Error from server (Forbidden): is forbidden' >&2
      exit 1 ;;
  esac
elif [[ "$1" == "delete" ]]; then
  case "${OC_DELETE_SCENARIO:-ok}" in
    ok)
      exit 0 ;;
    not-found)
      echo 'Error from server (NotFound): clusterautoscalers "default" not found' >&2
      exit 1 ;;
    error)
      echo 'Error from server (Forbidden): is forbidden' >&2
      exit 1 ;;
  esac
else
  echo "unexpected oc invocation: $*" >&2
  exit 64
fi
"""


class FakeOcFixture(ChartFixture):
    """Shared plumbing for executing rendered hook scripts against a fake
    oc binary, mirroring the cert-manager fixture of the same name.

    Each run gets a restricted PATH built solely from symlinks in an
    isolated bin directory (the JSON tool chosen per test path, core
    utilities, and the fake oc), so no ambient host tool or real oc can be
    reached. The child environment is an explicit minimal set — no parent
    variables are inherited."""
    MAX_ATTEMPTS = 5
    work_prefix = "ca-script-"
    bin_tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env",
                 "sort", "head"]
    fake_oc_script = FAKE_OC_SCRIPT

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix=self.work_prefix)
        self.addCleanup(shutil.rmtree, self.work)
        cm = self.pick(self.render_enabled(), "ConfigMap",
                       "clusterautoscaler-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "clusterautoscaler-set.sh",
                                     "clusterautoscaler-remove.sh")}
        self.config = json.loads(cm["data"]["autoscaler.json"])

    def build_bin_dir(self, include_jq=None, include_python=None):
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for name in os.listdir(bin_dir):
            os.unlink(os.path.join(bin_dir, name))
        if include_jq is None:
            include_jq = shutil.which("jq") is not None
        if include_python is None:
            include_python = shutil.which("python3") is not None
        tools = list(self.bin_tools)
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
            f.write(self.fake_oc_script)
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

    def child_env(self):
        with open(os.path.join(self.work, "env.log")) as f:
            return dict(
                line.split("=", 1) for line in f.read().splitlines() if "=" in line)

    def patch_lines(self):
        path = os.path.join(self.work, "patches.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [line for line in f.read().splitlines() if line]

    def patches(self):
        return [json.loads(line) for line in self.patch_lines()]

    def create_payload(self):
        path = os.path.join(self.work, "create.log")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)

    def deleted(self):
        return any(line.startswith("delete ") for line in
                   self.log().splitlines())

    def stage_scripts(self):
        # Every staged file lands in the same directory as the entry script
        # because the scripts source their shared libs from their own
        # directory, mirroring the single ConfigMap mount
        for name, content in self.scripts.items():
            with open(os.path.join(self.work, name), "w") as f:
                f.write(content)
        with open(os.path.join(self.work, "autoscaler.json"), "w") as f:
            f.write(json.dumps(self.config))

    def clear_run_state(self):
        # Each run is self-contained: drop logs and the fake oc's
        # resourceVersion counters left by any earlier run
        for stale in ("commands.log", "env.log", "patches.log", "create.log"):
            stale_path = os.path.join(self.work, stale)
            if os.path.exists(stale_path):
                os.unlink(stale_path)
        for stale in os.listdir(self.work):
            if stale.startswith("attempts") or stale.endswith(".rv"):
                os.unlink(os.path.join(self.work, stale))

    def run_child(self, script_path, env):
        with mock.patch.dict(os.environ,
                             {"CA_PARENT_SENTINEL": "must-not-leak"}):
            return subprocess.run(
                ["/bin/bash", script_path], capture_output=True, text=True,
                env=env, timeout=120)


class ScriptFixture(FakeOcFixture):
    """Runs one hook script against one canned ClusterAutoscaler document."""

    def ca_doc(self, spec=None, annotations=None, resource_version="1597"):
        metadata = {"name": "default", "resourceVersion": resource_version}
        if annotations:
            metadata["annotations"] = annotations
        return {
            "apiVersion": "autoscaling.openshift.io/v1",
            "kind": "ClusterAutoscaler",
            "metadata": metadata,
            "spec": FOREIGN_SPEC if spec is None else spec,
        }

    def run_script(self, script_name, scenario="ok", doc=None,
                   patch_scenario="ok", create_scenario="ok",
                   delete_scenario="ok", config_file=None,
                   include_jq=None, include_python=None):
        bin_dir = self.build_bin_dir(include_jq=include_jq,
                                     include_python=include_python)
        self.stage_scripts()
        get_file = os.path.join(self.work, "ca.json")
        with open(get_file, "w") as f:
            json.dump(doc if doc is not None else self.ca_doc(), f)
        self.clear_run_state()
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "OC_SCENARIO": scenario,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_ENV": os.path.join(self.work, "env.log"),
            "OC_GET_FILE": get_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
            "OC_PATCH_SCENARIO": patch_scenario,
            "OC_ATTEMPTS": os.path.join(self.work, "attempts"),
            "OC_CREATE_LOG": os.path.join(self.work, "create.log"),
            "OC_CREATE_SCENARIO": create_scenario,
            "OC_DELETE_SCENARIO": delete_scenario,
            "CONFIG_FILE": (config_file if config_file is not None
                            else os.path.join(self.work, "autoscaler.json")),
        }
        return self.run_child(os.path.join(self.work, script_name), env)


class SetScriptTests(ScriptFixture):
    """Script-execution coverage for clusterautoscaler-set.sh: the
    record-and-restore state machine (create-on-absent with the creation
    proof, prior recording on the first change, equal no-ops, unmanaged-field
    preservation, created-object value changes without recording, and the
    bounded 409 retry) on both JSON tool paths."""

    def test_child_environment_is_minimal_and_isolated(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 doc=self.ca_doc(spec={}))
        self.assertEqual(result.returncode, 0, result.stderr)
        child = self.child_env()
        self.assertNotIn("CA_PARENT_SENTINEL", child)
        self.assertEqual(child["PATH"], os.path.join(self.work, "bin"))
        self.assertEqual(
            {k for k in child if k.startswith("OC_")},
            {"OC_SCENARIO", "OC_LOG", "OC_ENV", "OC_GET_FILE",
             "OC_PATCH_LOG", "OC_ATTEMPTS", "OC_CREATE_LOG",
             "OC_CREATE_SCENARIO", "OC_DELETE_SCENARIO",
             "OC_PATCH_SCENARIO"})
        self.assertIn(
            "get clusterautoscalers.autoscaling.openshift.io/default "
            "-o json --request-timeout=30s", self.log())

    def test_create_on_absent_carries_creation_proof(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script("clusterautoscaler-set.sh",
                                         scenario="absent-object", **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = self.create_payload()
                self.assertEqual(manifest["metadata"]["name"], "default")
                self.assertEqual(
                    manifest["metadata"]["annotations"],
                    {PROOF_ANNOTATION: '{"created":true}'})
                self.assertEqual(
                    manifest["spec"], self.config["manifest"]["spec"])
                self.assertEqual(self.patches(), [])
                self.assertNotIn("patch ", self.log())

    def test_create_race_fails_for_the_job_retry_to_merge(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 scenario="absent-object",
                                 create_scenario="already-exists")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("created concurrently", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_records_priors_and_sets_values(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script("clusterautoscaler-set.sh",
                                         **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                patches = self.patches()
                self.assertEqual(len(patches), 1)
                patch = patches[0]
                # The annotation payload compares parsed: the two JSON tool
                # paths serialize the same record with different spacing
                self.assertEqual(
                    json.loads(patch["metadata"]["annotations"]
                               [PROOF_ANNOTATION]),
                    {"podPriorityThreshold": -100,
                     "scaleDown": None,
                     "gpuEntry": None})
                spec = patch["spec"]
                self.assertEqual(spec["podPriorityThreshold"], -10)
                self.assertEqual(spec["scaleDown"],
                                 self.config["config"]["scaleDown"])
                # The whole gpus array is rewritten atomically: the foreign
                # amd entry is preserved in place and ours is appended
                self.assertEqual(spec["resourceLimits"]["gpus"],
                                 [AMD_ENTRY,
                                  self.config["config"]["gpuEntry"]])
                # Unmanaged fields are never touched
                self.assertNotIn("maxNodesTotal", spec)
                self.assertNotIn("cores",
                                 spec.get("resourceLimits", {}))

    def test_equal_state_is_no_op(self):
        # Prior output: every managed field equals the desired value and the
        # proof annotation records what the set hook changed to get there
        annotations = {
            PROOF_ANNOTATION: json.dumps({
                "podPriorityThreshold": -100, "scaleDown": None,
                "gpuEntry": None})}
        spec = {"podPriorityThreshold": -10, "maxNodesTotal": 150,
                "resourceLimits": {"cores": {"min": 8, "max": 100},
                                   "gpus": [AMD_ENTRY,
                                            self.config["config"]["gpuEntry"]]},
                "scaleDown": self.config["config"]["scaleDown"]}
        result = self.run_script("clusterautoscaler-set.sh",
                                 doc=self.ca_doc(spec=spec,
                                                 annotations=annotations))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)
        self.assertIn("no changes needed", result.stdout)

    def test_equal_state_without_annotation_writes_nothing(self):
        # Nothing differs, so nothing is changed and nothing is recorded:
        # the coexistence contract never annotates an object it did not
        # modify
        spec = {"podPriorityThreshold": -10,
                "resourceLimits": {"gpus": [
                    self.config["config"]["gpuEntry"]]},
                "scaleDown": self.config["config"]["scaleDown"]}
        result = self.run_script("clusterautoscaler-set.sh",
                                 doc=self.ca_doc(spec=spec))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])

    def test_created_object_value_change_does_not_record(self):
        # The object carries the creation proof: it never had pre-chart
        # values to restore, so changed values are set without recording
        annotations = {PROOF_ANNOTATION: '{"created":true}'}
        spec = {"podPriorityThreshold": -10,
                "resourceLimits": {"gpus": [
                    {"type": "nvidia.com/gpu", "min": 0, "max": 5}]}}
        result = self.run_script("clusterautoscaler-set.sh",
                                 doc=self.ca_doc(spec=spec,
                                                 annotations=annotations))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        patch = patches[0]
        self.assertEqual(patch["spec"]["resourceLimits"]["gpus"],
                         [self.config["config"]["gpuEntry"]])
        new_proof = json.loads(patch["metadata"]["annotations"]
                               [PROOF_ANNOTATION])
        self.assertEqual(new_proof, {"created": True})

    def test_second_run_records_additional_priors(self):
        # A values change between upgrades takes over another field: the new
        # prior merges into the existing record and earlier priors survive
        annotations = {
            PROOF_ANNOTATION: json.dumps({"podPriorityThreshold": -100})}
        spec = {"podPriorityThreshold": -10,
                "scaleDown": {"enabled": False},
                "resourceLimits": {"gpus": [
                    self.config["config"]["gpuEntry"]]}}
        result = self.run_script("clusterautoscaler-set.sh",
                                 doc=self.ca_doc(spec=spec,
                                                 annotations=annotations))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        proof = json.loads(patches[0]["metadata"]["annotations"]
                           [PROOF_ANNOTATION])
        self.assertEqual(proof, {"podPriorityThreshold": -100,
                                 "scaleDown": {"enabled": False}})
        # podPriorityThreshold already equals the desired value: unchanged,
        # and its prior record is not overwritten
        self.assertNotIn("podPriorityThreshold", patches[0]["spec"])

    def test_unparseable_proof_annotation_stops(self):
        result = self.run_script(
            "clusterautoscaler-set.sh",
            doc=self.ca_doc(annotations={PROOF_ANNOTATION: "not-json"}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a JSON object", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_missing_config_file_fails(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 config_file="/nonexistent/autoscaler.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing at", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_get_error_propagates(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 scenario="get-error")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertIsNone(self.create_payload())

    def test_conflict_retry_is_bounded_and_rewrites(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 patch_scenario="conflict-then-success")
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 2)
        # The fake oc bumps the served resourceVersion on every GET, so the
        # failed attempt carried the stale version and the successful retry
        # carried the fresh one from its own re-read
        self.assertEqual(patches[0]["metadata"]["resourceVersion"], "1597")
        self.assertEqual(patches[1]["metadata"]["resourceVersion"], "1598")
        self.assertIn("retrying", result.stderr)

    def test_conflict_exhaustion_fails(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 patch_scenario="conflict-exhausted")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)
        self.assertIn("Conflict", result.stderr)

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 include_jq=False, include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class RemoveScriptTests(ScriptFixture):
    """Script-execution coverage for clusterautoscaler-remove.sh: the
    walk-back state machine (absent no-op, leave-unannotated,
    delete-only-under-create-proof, recorded-prior restore, gpus rebuild
    preserving foreign entries, annotation clear, and the bounded 409
    retry)."""

    def test_absent_object_is_no_op(self):
        result = self.run_script("clusterautoscaler-remove.sh",
                                 scenario="absent-object")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("absent", result.stdout)
        self.assertEqual(self.patches(), [])
        self.assertFalse(self.deleted())

    def test_unannotated_object_is_left_in_place(self):
        result = self.run_script("clusterautoscaler-remove.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("never configured by this chart", result.stdout)
        self.assertEqual(self.patches(), [])
        self.assertFalse(self.deleted())
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_created_object_is_deleted(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "clusterautoscaler-remove.sh",
                    doc=self.ca_doc(
                        spec={"podPriorityThreshold": -10},
                        annotations={PROOF_ANNOTATION: '{"created":true}'}),
                    **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(self.deleted())
                self.assertEqual(self.patches(), [])
                self.assertIn("delete ", self.log())

    def test_restores_recorded_priors(self):
        annotations = {
            PROOF_ANNOTATION: json.dumps({
                "podPriorityThreshold": -100, "scaleDown": None,
                "gpuEntry": None})}
        spec = {"podPriorityThreshold": -10, "maxNodesTotal": 150,
                "resourceLimits": {"cores": {"min": 8, "max": 100},
                                   "gpus": [AMD_ENTRY,
                                            self.config["config"]["gpuEntry"]]},
                "scaleDown": self.config["config"]["scaleDown"]}
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "clusterautoscaler-remove.sh",
                    doc=self.ca_doc(spec=spec, annotations=annotations),
                    **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                patches = self.patches()
                self.assertEqual(len(patches), 1)
                patch = patches[0]
                # The recorded priors come back: the foreign threshold, the
                # absent scaleDown (deleted by the null merge patch), and a
                # gpus array with only the foreign amd entry left
                self.assertEqual(patch["spec"], {
                    "podPriorityThreshold": -100,
                    "scaleDown": None,
                    "resourceLimits": {"gpus": [AMD_ENTRY]},
                })
                # The proof annotation is cleared
                self.assertEqual(patch["metadata"]["annotations"],
                                 {PROOF_ANNOTATION: None})
                # Unmanaged fields are never touched
                self.assertNotIn("maxNodesTotal", patch["spec"])
                self.assertNotIn("cores",
                                 patch["spec"].get("resourceLimits", {}))
                self.assertFalse(self.deleted())

    def test_restores_recorded_gpu_entry_in_place(self):
        prior_gpu = {"type": "nvidia.com/gpu", "min": 0, "max": 4}
        annotations = {PROOF_ANNOTATION: json.dumps({"gpuEntry": prior_gpu})}
        spec = {"podPriorityThreshold": -10,
                "resourceLimits": {"gpus": [
                    AMD_ENTRY, self.config["config"]["gpuEntry"]]}}
        result = self.run_script("clusterautoscaler-remove.sh",
                                 doc=self.ca_doc(spec=spec,
                                                 annotations=annotations))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["spec"]["resourceLimits"]["gpus"],
                         [AMD_ENTRY, prior_gpu])

    def test_appends_recorded_gpu_entry_when_hand_removed(self):
        # The recorded prior survives even if the entry this hook set was
        # removed by hand in the meantime
        prior_gpu = {"type": "nvidia.com/gpu", "min": 0, "max": 4}
        annotations = {PROOF_ANNOTATION: json.dumps({"gpuEntry": prior_gpu})}
        spec = {"podPriorityThreshold": -10, "resourceLimits": {"gpus": []}}
        result = self.run_script("clusterautoscaler-remove.sh",
                                 doc=self.ca_doc(spec=spec,
                                                 annotations=annotations))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches()[0]["spec"]["resourceLimits"]["gpus"],
                         [prior_gpu])

    def test_unrecorded_nvidia_entry_leaves_gpus_untouched(self):
        # A proof annotation with no gpuEntry record (the entry already
        # equaled this chart's values when the set hook first saw it): a
        # pre-existing nvidia.com/gpu entry this hook never changed is
        # never deleted — the gpus array is left untouched while recorded
        # priors for other fields still restore
        annotations = {
            PROOF_ANNOTATION: json.dumps({"podPriorityThreshold": -100})}
        spec = {"podPriorityThreshold": -10,
                "resourceLimits": {"gpus": [
                    AMD_ENTRY, self.config["config"]["gpuEntry"]]}}
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "clusterautoscaler-remove.sh",
                    doc=self.ca_doc(spec=spec, annotations=annotations),
                    **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                patches = self.patches()
                self.assertEqual(len(patches), 1)
                patch = patches[0]
                # The recorded threshold restores; the gpus array (with the
                # unrecorded nvidia entry and the foreign entry) is absent
                # from the patch entirely
                self.assertEqual(patch["spec"],
                                 {"podPriorityThreshold": -100})
                self.assertNotIn("resourceLimits", patch["spec"])

    def test_clears_annotation_when_priors_already_hold(self):
        # Everything recorded already matches the live object: only the
        # annotation clear is left to write
        annotations = {PROOF_ANNOTATION: '{"podPriorityThreshold":-10}'}
        result = self.run_script(
            "clusterautoscaler-remove.sh",
            doc=self.ca_doc(spec={"podPriorityThreshold": -10},
                            annotations=annotations))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1598",
                         "annotations": {PROOF_ANNOTATION: None}},
        })
        self.assertNotIn("spec", patches[0])

    def test_unparseable_proof_annotation_fails_closed(self):
        # Both JSON tool paths fail closed with the same curated diagnostic
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "clusterautoscaler-remove.sh",
                    doc=self.ca_doc(
                        annotations={PROOF_ANNOTATION: "not-json"}),
                    **variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("not a JSON object", result.stderr)
                self.assertEqual(self.patches(), [])
                self.assertFalse(self.deleted())

    def test_delete_race_not_found_succeeds(self):
        result = self.run_script(
            "clusterautoscaler-remove.sh",
            doc=self.ca_doc(annotations={PROOF_ANNOTATION:
                                         '{"created":true}'}),
            delete_scenario="not-found")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_conflict_retry_is_bounded_and_rewrites(self):
        result = self.run_script(
            "clusterautoscaler-remove.sh",
            doc=self.ca_doc(spec={"podPriorityThreshold": -10},
                            annotations={PROOF_ANNOTATION:
                                         json.dumps(
                                             {"podPriorityThreshold": -100})}),
            patch_scenario="conflict-then-success")
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 2)
        # The remove hook's presence read precedes the conditional write, so
        # the fake oc has bumped the served resourceVersion once before the
        # first patch: the failed attempt carried that version and the
        # successful retry carried the fresh one from its own re-read
        self.assertEqual(patches[0]["metadata"]["resourceVersion"], "1598")
        self.assertEqual(patches[1]["metadata"]["resourceVersion"], "1599")
        self.assertIn("retrying", result.stderr)

    def test_conflict_exhaustion_fails(self):
        annotations = {
            PROOF_ANNOTATION: json.dumps({"podPriorityThreshold": -100})}
        result = self.run_script(
            "clusterautoscaler-remove.sh",
            doc=self.ca_doc(spec={"podPriorityThreshold": -10},
                            annotations=annotations),
            patch_scenario="conflict-exhausted")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("clusterautoscaler-remove.sh",
                                 include_jq=False, include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


if __name__ == "__main__":
    unittest.main()
