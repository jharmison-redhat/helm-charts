"""Render contracts for the openshift-ai replicas management surface plus
the replica hook-script smoke set against a fake oc binary."""

import json
import os
import subprocess

import support

from oa_support import ChartFixture


class _ReplicasValuesMixin:
    """The replicas values block with per-test overrides."""

    def replicas_values(self, **overrides):
        values = self.managed_maas()
        values["replicas"] = {"dashboard": None, "operator": None,
                              "asCronJob": True, "schedule": "*/15 * * * *",
                              "waitTimeoutSeconds": 240}
        values["replicas"].update(overrides)
        return values


# Fake oc for the replicas hook-script smoke runs: serves a canned
# Deployment for get and records every --patch= payload. Invocations are
# logged so tests can assert the exact oc call surface.
FAKE_OC_REPLICAS = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" ]]; then
  cat "$OC_DEPLOYMENT_FILE"
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


class _ReplicasScriptFixture(_ReplicasValuesMixin, ChartFixture,
                             support.FakeOcScriptFixture):
    """Executes the rendered replicas hook scripts against a fake oc binary
    (this module's FAKE_OC_REPLICAS): a restricted PATH built solely from
    symlinks in an isolated bin directory (python3 as the single JSON tool,
    core utilities, and the fake oc), the scripts staged in one directory
    (they source the shared preamble from their own directory, mirroring
    the single ConfigMap mount), and an explicit minimal child
    environment."""

    work_prefix = "oa-replicas-"
    bin_tools = ["mktemp", "grep", "sleep", "rm", "cat", "date"]
    fake_oc_script = FAKE_OC_REPLICAS

    def setUp(self):
        super().setUp()
        self.deployment_file = os.path.join(self.work, "deployment.json")
        self.patch_log = os.path.join(self.work, "patches.log")
        for stale in (self.deployment_file, self.patch_log,
                      os.path.join(self.work, "commands.log")):
            if os.path.exists(stale):
                os.unlink(stale)

    def stage_scripts(self, values):
        by_kind = self.by_kind(self.render(values))
        cms = [d for d in by_kind["ConfigMap"]
               if (d.get("data") or {}).get("components.txt") is not None]
        self.assertEqual(len(cms), 1)
        super().stage_scripts(cms[0]["data"])

    def run_script(self, script_name, deployment_json):
        bin_dir = self.build_bin_dir()
        with open(self.deployment_file, "w") as f:
            f.write(deployment_json)
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "TMPDIR": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_DEPLOYMENT_FILE": self.deployment_file,
            "OC_PATCH_LOG": self.patch_log,
            "REPLICAS_COMPONENTS_FILE": os.path.join(self.work,
                                                     "components.txt"),
            "REPLICAS_WAIT_TIMEOUT": "2",
            "REPLICAS_POLL_INTERVAL": "1",
            "REPLICAS_WAIT_FOR_MISSING": "true",
        }
        return subprocess.run(
            ["/bin/bash", os.path.join(self.work, script_name)],
            capture_output=True, text=True, env=env, timeout=60)

    @staticmethod
    def deployment_doc(replicas, annotations=None, rv="42"):
        return json.dumps({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "rhods-dashboard",
                         "namespace": "redhat-ods-applications",
                         "resourceVersion": rv,
                         "annotations": annotations or {}},
            "spec": {"replicas": replicas},
        })


class ReplicaSurfaceTests(_ReplicasValuesMixin, ChartFixture):
    """The replicas surface's ownership and gating contract: by default the
    chart manages nothing (operator-created Deployments are never release
    manifests), and when a count is managed the RBAC is scoped to exactly
    the managed Deployments."""

    def surface(self, values):
        by_kind = self.by_kind(self.render(values))
        role = self.pick(by_kind, "ClusterRole",
                         [r["metadata"]["name"] for r in by_kind["ClusterRole"]
                          if "replicas" in r["metadata"]["name"]][0])
        return by_kind, role

    def components_file(self, by_kind):
        cms = [d for d in by_kind["ConfigMap"]
               if (d.get("data") or {}).get("components.txt") is not None]
        self.assertEqual(len(cms), 1)
        return cms[0]["data"]["components.txt"]

    def test_default_hands_off(self):
        # All-null counts render nothing for the replicas surface and never
        # a Deployment: the chart manages nothing by default.
        by_kind = self.by_kind(self.render())
        self.assertNotIn("CronJob", by_kind)
        self.assertNotIn("Deployment", by_kind)
        self.assertEqual(
            [c for c in by_kind.get("ClusterRole", [])
             if "replicas" in c["metadata"]["name"]], [])
        self.assertEqual([j for j in by_kind.get("Job", [])
                          if "replicas" in j["metadata"]["name"]], [])

    def test_rbac_scoped_to_managed_deployments(self):
        values = self.replicas_values(dashboard=1)
        by_kind, role = self.surface(values)
        # The replicas surface contributes exactly one ClusterRole (the
        # chart's other hook families contribute their own).
        self.assertEqual(len(self.docs_named(by_kind, "ClusterRole",
                                             "replicas")), 1)
        self.assertEqual(role["rules"], [{
            "apiGroups": ["apps"], "resources": ["deployments"],
            "resourceNames": ["rhods-dashboard"], "verbs": ["get", "patch"],
        }])
        binding = self.pick(by_kind, "ClusterRoleBinding",
                            role["metadata"]["name"])
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": role["metadata"]["name"],
            "namespace": "test-release-ns"}])
        # Only the managed Deployment is in scope: managing the operator
        # adds exactly its resourceName.
        values["replicas"]["operator"] = 0
        _, role = self.surface(values)
        self.assertEqual(role["rules"][0]["resourceNames"],
                         ["rhods-dashboard", "rhods-operator"])


class EnforceReplicasScriptTests(_ReplicasScriptFixture):
    """Minimal smoke set for the replica hook scripts, per the repo's
    unit-test policy: one happy-path run per script (python3-only tool path)
    plus the chart's fail-closed no-tool check. State-machine behavior
    (drift correction, recreate re-baselining, 409 retries, foreign-proof
    walk-back) is proven by live-cluster validation, not fixtures."""

    def test_enforce_happy_path_smoke(self):
        self.stage_scripts(self.replicas_values(dashboard=1))
        result = self.run_script("enforce-replicas.sh",
                                 self.deployment_doc(replicas=2))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        # The prior (2) is recorded under proof and the count patched to 1,
        # conditionally on the live resourceVersion.
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "42",
                         "annotations": {"openshift-ai/replicas-prior": "2"}},
            "spec": {"replicas": 1},
        })
        # The chart neither creates, adopts, nor deletes Deployments.
        self.assertNotIn("create", self.log())
        self.assertNotIn("delete", self.log())
        self.assertNotIn("apply", self.log())

    def test_restore_happy_path_smoke(self):
        self.stage_scripts(self.replicas_values(dashboard=1))
        result = self.run_script(
            "restore-replicas.sh",
            self.deployment_doc(
                replicas=1, rv="43",
                annotations={"openshift-ai/replicas-prior": "2"}))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        # The recorded prior is restored and the proof annotation cleared.
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "43",
                         "annotations": {"openshift-ai/replicas-prior": None}},
            "spec": {"replicas": 2},
        })
        self.assertNotIn("delete", self.log())

    def test_no_oc_fails_closed(self):
        # The chart's fail-closed no-tool check: with no oc on PATH the
        # enforce script fails before any mutation.
        self.stage_scripts(self.replicas_values(dashboard=1))
        bin_dir = self.build_bin_dir(include_oc=False)
        with open(self.deployment_file, "w") as f:
            f.write(self.deployment_doc(replicas=2))
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "TMPDIR": self.work,
            "REPLICAS_COMPONENTS_FILE": os.path.join(self.work,
                                                     "components.txt"),
            "REPLICAS_WAIT_TIMEOUT": "2",
            "REPLICAS_POLL_INTERVAL": "1",
            "REPLICAS_WAIT_FOR_MISSING": "true",
        }
        result = subprocess.run(
            ["/bin/bash", os.path.join(self.work, "enforce-replicas.sh")],
            capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self.patch_log))
