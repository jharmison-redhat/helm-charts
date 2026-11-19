"""Focused tests for the aws-nvidia-gpu-machinesets chart. The module covers
the autoscaling gate (the ClusterAutoscaler/default singleton is never a
release manifest — with autoscaling disabled nothing of the surface renders,
and with it enabled the hook pair renders and the autoscaler owns the
MachineSet replica counts), the resourceName-scoped hook RBAC with the one
documented unscoped `create` grant, the namespace pinning, one
representative render-failure trap, the hook-script smoke set (one
happy-path run per script plus the chart's fail-closed no-tool check), and
the guard-without-pin hook contract.

Render tests run the chart through ``helm template`` (the chart has no
dependencies). The script smoke runs execute the rendered hook scripts
against a fake ``oc`` on a restricted PATH built entirely from symlinks, so
neither a real ``oc`` nor the test host's ambient tools (jq included) can
leak into the run. Hook-script state-machine behavior (retries, foreign-
value STOP, walk-back) is proven by live deploys, not here.
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
    def setUp(self):
        super().setUp()
        self.stage_real_dependency("common")

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


class RenderTests(ChartFixture):
    def test_autoscaling_gate_and_inventory(self):
        # With autoscaling disabled (the default) nothing of the
        # ClusterAutoscaler surface renders; the MachineSets carry the
        # replica counts.
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

        # With it enabled the hook pair renders, no ClusterAutoscaler
        # release manifest exists, and the autoscaler owns the replica
        # counts.
        by_kind = self.render_enabled()
        self.assertNotIn("ClusterAutoscaler", by_kind)
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in by_kind["Job"]),
            ["clusterautoscaler-remove", "clusterautoscaler-set"])
        self.assertEqual(len(by_kind["MachineAutoscaler"]), 3)
        for ms in by_kind["MachineSet"]:
            self.assertNotIn("replicas", ms["spec"])
        # The release namespace is free: every namespaced chart object pins
        # openshift-machine-api outright, regardless of where the release
        # installs — proven by rendering into a deliberately foreign release
        # namespace. The ClusterRole/ClusterRoleBinding are cluster-scoped
        # and carry no namespace; the binding's subject still pins the
        # canonical namespace for its ServiceAccount.
        by_kind = self.render_enabled(namespace="foreign-ns")
        for kind in ("MachineSet", "MachineAutoscaler", "Job",
                     "ServiceAccount", "ConfigMap"):
            for doc in by_kind[kind]:
                self.assertEqual(doc["metadata"]["namespace"],
                                 "openshift-machine-api", (kind, doc))
        # The RBAC is resourceName-scoped to the default singleton, with the
        # one documented unscoped create grant (create cannot be
        # resourceName-scoped; the set hook must create the singleton when
        # absent).
        role = self.pick(by_kind, "ClusterRole", "clusterautoscaler")
        self.assertEqual(role["rules"], [
            {
                "apiGroups": ["autoscaling.openshift.io"],
                "resources": ["clusterautoscalers"],
                "resourceNames": ["default"],
                "verbs": ["get", "patch", "delete"],
            },
            {
                "apiGroups": ["autoscaling.openshift.io"],
                "resources": ["clusterautoscalers"],
                "verbs": ["create"],
            },
        ])
        binding = self.pick(by_kind, "ClusterRoleBinding", "clusterautoscaler")
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "clusterautoscaler",
            "namespace": "openshift-machine-api",
        }])

    def test_az_outside_azs_fails_render(self):
        values = copy.deepcopy(BASE_VALUES)
        values["aws"]["az"] = "us-east-1"
        self.render_failure(values)


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
        # The chart's fail-closed no-tool check.
        result = self.run_script("clusterautoscaler-set.sh",
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


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
            [d for docs_ in self.render_enabled().values() for d in docs_])
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in jobs),
            ["clusterautoscaler-remove", "clusterautoscaler-set"])
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])
