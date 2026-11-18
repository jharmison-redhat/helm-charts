"""Focused tests for the aws-nvidia-gpu-machinesets chart. The module covers
the per-zone MachineAutoscaler distribution (totals preserved exactly: the
first `total mod zoneCount` zones take `floor(total/zoneCount) + 1`, the rest
take `floor(total/zoneCount)`, independently for min and max), the
render-time `aws.az` membership trap (an `aws.az` outside `aws.azs` used to
silently leave every MachineSet at zero replicas), the autoscaling gate (the
ClusterAutoscaler/default singleton is never a release manifest — it is
configured by a post-install/upgrade set hook and walked back by a pre-delete
remove hook with record-and-restore semantics), and the resourceName-scoped
hook RBAC (with the one documented unscoped `create` grant).

Render tests run the chart through ``helm template`` (the chart has no
dependencies). Per the repo's unit-test policy the script-execution layer is
a minimal smoke set: one happy-path run per rendered hook script on the
python3-only tool path and one fail-closed no-tool check for the chart, each
against a fake ``oc`` on a restricted PATH built entirely from symlinks, so
neither a real ``oc`` nor the test host's ambient tools (jq included) can
leak into the run. Hook-script state-machine behavior is proven by live
deploys, not here.
"""

import copy
import json
import os
import unittest

import support

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


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "ca-test-"
    values_prefix = "ca-values-"
    # Every chart object pins openshift-machine-api, so renders default there
    release_namespace = "openshift-machine-api"

    def render_raw(self, values=None, namespace=None):
        merged = copy.deepcopy(BASE_VALUES)
        if values is not None:
            for key, val in values.items():
                if isinstance(val, dict) and isinstance(merged.get(key), dict):
                    merged[key].update(val)
                else:
                    merged[key] = val
        return self.helm_template(merged, namespace)

    def render_enabled(self, namespace=None, **kwargs):
        return self.by_kind(
            self.render(autoscaler_values(**kwargs), namespace=namespace))


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

    def test_every_object_pins_openshift_machine_api(self):
        # The release namespace is free: every namespaced chart object pins
        # openshift-machine-api outright, regardless of where the release
        # installs — proven by rendering into a deliberately foreign release
        # namespace. The ClusterRole/ClusterRoleBinding are cluster-scoped
        # and carry no namespace; the binding's subject still pins the
        # canonical namespace for its ServiceAccount.
        by_kind = self.render_enabled(namespace="foreign-ns")
        expected = {
            "MachineSet": by_kind["MachineSet"],
            "MachineAutoscaler": by_kind["MachineAutoscaler"],
            "Job": by_kind["Job"],
            "ServiceAccount": by_kind["ServiceAccount"],
            "ConfigMap": by_kind["ConfigMap"],
        }
        for kind, docs in expected.items():
            for doc in docs:
                self.assertEqual(doc["metadata"]["namespace"],
                                 "openshift-machine-api", (kind, doc))
        binding = self.pick(by_kind, "ClusterRoleBinding", "clusterautoscaler")
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "clusterautoscaler",
            "namespace": "openshift-machine-api",
        }])

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


# Fake oc for the hook-script smoke runs: serves the canned ClusterAutoscaler
# for get and records every patch payload. Each invocation is logged.
FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" ]]; then
  cat "$OC_GET_FILE"
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


class FakeOcScriptFixture(ChartFixture, support.FakeOcScriptFixture):
    """Restricted-PATH harness for the hook-script smoke set; the canned oc
    program is this module's FAKE_OC_SCRIPT (plain get — the smoke set
    makes one conditional write per run, so no resourceVersion bumping)."""

    work_prefix = "ca-script-"
    fake_oc_script = FAKE_OC_SCRIPT

    def setUp(self):
        super().setUp()
        cm = self.pick(self.render_enabled(), "ConfigMap",
                       "clusterautoscaler-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "clusterautoscaler-set.sh",
                                     "clusterautoscaler-remove.sh")}
        self.config = json.loads(cm["data"]["autoscaler.json"])

    def ca_doc(self, spec=None, annotations=None):
        """Canned pre-existing ClusterAutoscaler the fake oc serves for
        get."""
        metadata = {"name": "default", "resourceVersion": "1597"}
        if annotations:
            metadata["annotations"] = annotations
        return {
            "apiVersion": "autoscaling.openshift.io/v1",
            "kind": "ClusterAutoscaler",
            "metadata": metadata,
            "spec": FOREIGN_SPEC if spec is None else spec,
        }

    def run_script(self, script_name, doc=None, include_python=True):
        bin_dir = self.build_bin_dir(include_python=include_python)
        self.stage_scripts()
        with open(os.path.join(self.work, "autoscaler.json"), "w") as f:
            f.write(json.dumps(self.config))
        ca_file = os.path.join(self.work, "ca.json")
        with open(ca_file, "w") as f:
            json.dump(doc if doc is not None else self.ca_doc(), f)
        self.clear_run_state()
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_GET_FILE": ca_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
            "CONFIG_FILE": os.path.join(self.work, "autoscaler.json"),
        }
        return self.run_child(os.path.join(self.work, script_name), env)


class HookScriptSmokeTests(FakeOcScriptFixture):
    """Policy smoke set for the rendered hook scripts: one happy-path run
    per script on the python3-only tool path plus the chart's fail-closed
    no-tool check. The state-machine behavior (retries, foreign-value STOP,
    walk-back) is proven by live deploys, not here."""

    def test_set_success_records_priors_and_sets_values(self):
        result = self.run_script("clusterautoscaler-set.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        patch = patches[0]
        self.assertEqual(patch["metadata"]["resourceVersion"], "1597")
        self.assertEqual(
            json.loads(patch["metadata"]["annotations"][PROOF_ANNOTATION]),
            {"podPriorityThreshold": -100, "scaleDown": None,
             "gpuEntry": None})
        self.assertEqual(patch["spec"]["podPriorityThreshold"], -10)
        # The whole gpus array is rewritten atomically: the foreign amd
        # entry is preserved in place and ours is appended
        self.assertEqual(patch["spec"]["resourceLimits"]["gpus"],
                         [AMD_ENTRY, self.config["config"]["gpuEntry"]])
        # Unmanaged fields are never touched
        self.assertNotIn("maxNodesTotal", patch["spec"])
        self.assertNotIn("cores", patch["spec"].get("resourceLimits", {}))

    def test_remove_success_restores_recorded_priors(self):
        annotations = {
            PROOF_ANNOTATION: json.dumps({
                "podPriorityThreshold": -100, "scaleDown": None,
                "gpuEntry": None})}
        spec = {"podPriorityThreshold": -10, "maxNodesTotal": 150,
                "resourceLimits": {"cores": {"min": 8, "max": 100},
                                   "gpus": [AMD_ENTRY,
                                            self.config["config"]
                                            ["gpuEntry"]]},
                "scaleDown": self.config["config"]["scaleDown"]}
        result = self.run_script("clusterautoscaler-remove.sh",
                                 doc=self.ca_doc(spec=spec,
                                                 annotations=annotations))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        patch = patches[0]
        # The recorded priors come back: the foreign threshold, the absent
        # scaleDown (deleted by the null merge patch), and a gpus array with
        # only the foreign amd entry left; the proof annotation is cleared
        self.assertEqual(patch["spec"], {
            "podPriorityThreshold": -100,
            "scaleDown": None,
            "resourceLimits": {"gpus": [AMD_ENTRY]},
        })
        self.assertEqual(patch["metadata"]["annotations"],
                         {PROOF_ANNOTATION: None})
        # Unmanaged fields are never touched
        self.assertNotIn("maxNodesTotal", patch["spec"])
        self.assertNotIn("cores", patch["spec"].get("resourceLimits", {}))

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("clusterautoscaler-set.sh",
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


if __name__ == "__main__":
    unittest.main()

