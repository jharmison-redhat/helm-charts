"""Render contracts for the openshift-ai operand configuration hook
surfaces plus the operand hook-script smoke sets against a fake oc
binary."""

import json
import os
import subprocess

import support

from oa_support import ChartFixture


# Fake oc for the operand hook-script smoke runs: reports the CRD wait as
# Established, serves a canned object for get (or NotFound under
# OC_OBJECT_ABSENT, or separate Auth/DSCI/WVA payloads for the cluster-scoped
# singletons and the WVA ConfigMap), captures the created payload, records
# every --patch= payload, and accepts delete. Invocations are logged so
# tests can assert the exact oc call surface.
FAKE_OC_OPERAND = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" && "$2" == crd/* ]]; then
  echo "True"
  exit 0
fi
if [[ "$1" == "get" ]]; then
  # Namespaced gets carry "-n <ns>" before the resource, so the WVA target
  # is matched across all arguments rather than at a fixed position.
  wva_target=""
  for a in "$@"; do
    if [[ "$a" == *workload-variant-autoscaler-saturation-scaling-config* ]]; then
      wva_target="$a"
    fi
  done
  if [[ -n "$wva_target" ]]; then
    if [ -n "$OC_WVA_ABSENT" ]; then
      echo "Error from server (NotFound): $wva_target not found" >&2
      exit 1
    fi
    if [ -n "$OC_WVA_FILE" ]; then
      cat "$OC_WVA_FILE"
      exit 0
    fi
  fi
  if [ -n "$OC_OBJECT_ABSENT" ]; then
    echo "Error from server (NotFound): $2 not found" >&2
    exit 1
  fi
  if [[ -n "$OC_AUTH_FILE" && "$2" == auths* ]]; then
    cat "$OC_AUTH_FILE"
  elif [[ -n "$OC_DSCI_FILE" && "$2" == dscinitializations* ]]; then
    cat "$OC_DSCI_FILE"
  else
    cat "$OC_OBJECT_FILE"
  fi
  exit 0
fi
if [[ "$1" == "create" ]]; then
  prev=""
  for a in "$@"; do
    if [[ "$prev" == "-f" ]]; then cat "$a" > "$OC_CREATE_CAPTURE"; fi
    prev="$a"
  done
  exit 0
fi
if [[ "$1" == "patch" ]]; then
  for a in "$@"; do
    if [[ "$a" == --patch=* ]]; then
      printf '%s\\n' "${a#--patch=}" >> "$OC_PATCH_LOG"
    fi
  done
  exit 0
fi
if [[ "$1" == "delete" ]]; then
  exit 0
fi
echo "unexpected oc invocation: $*" >&2
exit 64
"""


class _OperandScriptFixture(ChartFixture, support.FakeOcScriptFixture):
    """Executes the rendered operand hook scripts against a fake oc binary
    (this module's FAKE_OC_OPERAND): the family's script ConfigMap staged
    into one directory (scripts and JSON payloads side by side, mirroring
    the single ConfigMap mount), a restricted PATH (python3 as the single
    JSON tool, core utilities, the fake oc), and per-test object JSON files
    the fake oc serves."""

    work_prefix = "oa-operand-"
    bin_tools = ["mktemp", "grep", "sleep", "rm", "cat", "date"]
    fake_oc_script = FAKE_OC_OPERAND

    FAMILY_FRAGMENT = ""  # the ConfigMap-name fragment of the family under test

    def setUp(self):
        super().setUp()
        self.object_file = os.path.join(self.work, "object.json")
        self.auth_file = os.path.join(self.work, "auth.json")
        self.dsci_file = os.path.join(self.work, "dsci.json")
        self.wva_file = os.path.join(self.work, "wva.json")
        self.patch_log = os.path.join(self.work, "patches.log")
        self.create_capture = os.path.join(self.work, "created.json")

    def stage_cm(self, values=None):
        by_kind = self.by_kind(self.render(values))
        cms = [d for d in by_kind["ConfigMap"]
               if self.FAMILY_FRAGMENT in d["metadata"]["name"]
               and "hook-preamble.sh" in (d.get("data") or {})]
        self.assertEqual(len(cms), 1, self.FAMILY_FRAGMENT)
        self.stage_scripts(cms[0]["data"])
        return cms[0]["metadata"]["name"]

    def run_script(self, script_name, env_extra, absent=False):
        env = {
            "PATH": self.build_bin_dir(),
            "HOME": self.work,
            "TMPDIR": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_OBJECT_FILE": self.object_file,
            "OC_AUTH_FILE": self.auth_file,
            "OC_DSCI_FILE": self.dsci_file,
            "OC_PATCH_LOG": self.patch_log,
            "OC_CREATE_CAPTURE": self.create_capture,
            "WAIT_TIMEOUT": "2",
        }
        if absent:
            env["OC_OBJECT_ABSENT"] = "1"
        env.update(env_extra)
        return subprocess.run(
            ["/bin/bash", os.path.join(self.work, script_name)],
            capture_output=True, text=True, env=env, timeout=60)

    def captured_create(self):
        with open(self.create_capture) as f:
            return json.load(f)


class OperandHookRenderTests(ChartFixture):
    """One representative render of the operand configuration hook
    surfaces: the platform-owned product objects never render as release
    manifests, and each chart-created family carries its gated hook pair
    with scoped RBAC and its payload ConfigMap."""

    def family(self, by_kind, fragment):
        return {
            kind: self.docs_named(by_kind, kind, fragment)
            for kind in ("Job", "ServiceAccount", "ClusterRole",
                         "ClusterRoleBinding", "ConfigMap")
        }

    def test_platform_objects_never_render(self):
        by_kind = self.by_kind(self.render())
        self.assertNotIn("OdhDashboardConfig", by_kind)
        self.assertNotIn("Auth", by_kind)
        self.assertNotIn("MLflow", by_kind)
        self.assertNotIn("DSCInitialization", by_kind)
        for name in ("odh-segment-key-config",
                     "workload-variant-autoscaler-saturation-scaling-config"):
            self.assertEqual([d for d in by_kind.get("ConfigMap", [])
                              if d["metadata"]["name"] == name], [])

    def test_hook_families_render(self):
        # The chart-created families carry a set Job AND a pre-delete proof
        # -gated delete Job; supporting resources are ordinary release
        # manifests in the release namespace; the RBAC is
        # resourceName-scoped (create is the one unscoped verb, and only
        # where the hook must create the object when absent).
        by_kind = self.by_kind(self.render())
        for fragment in ("operator-configs", "dashboard-config", "mlflow-"):
            fam = self.family(by_kind, fragment)
            self.assertEqual(len(fam["ServiceAccount"]), 1, fragment)
            self.assertEqual(fam["ServiceAccount"][0]["metadata"]["namespace"],
                             "test-release-ns")
            jobs = fam["Job"]
            self.assertEqual(len(jobs), 2, fragment)
            for job in jobs:
                self.assertEqual(
                    job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
                    "before-hook-creation", fragment)
            self.assertEqual(
                {j["metadata"]["annotations"]["helm.sh/hook"] for j in jobs},
                {"post-install,post-upgrade", "pre-delete"}, fragment)
            self.assertEqual(len(fam["ConfigMap"]), 1, fragment)
            self.assertIn("hook-preamble.sh", fam["ConfigMap"][0]["data"],
                          fragment)
        # The dashboard family's ClusterRole is scoped to its singleton;
        # create carries its own unscoped rule (RBAC cannot scope it by
        # name — the set hook must create the object when no default
        # exists).
        rules = self.family(by_kind, "dashboard-config")["ClusterRole"][0][
            "rules"]
        self.assertEqual(rules, [
            {"apiGroups": ["opendatahub.io"], "resources": ["odhdashboardconfigs"],
             "resourceNames": ["odh-dashboard-config"],
             "verbs": ["get", "patch", "delete"]},
            {"apiGroups": ["opendatahub.io"], "resources": ["odhdashboardconfigs"],
             "verbs": ["create"]},
            {"apiGroups": ["apiextensions.k8s.io"],
             "resources": ["customresourcedefinitions"],
             "resourceNames": ["odhdashboardconfigs.opendatahub.io"],
             "verbs": ["get"]},
        ])


class OperandSetScriptTests(_OperandScriptFixture):
    """Happy-path smoke for the rendered set-chart-created-cr script (as
    mounted from each family's ConfigMap) against a fake oc binary: the
    fresh-install create path (object absent after the CRD wait) under the
    chart-created proof payload."""

    def run_set(self, absent=True):
        return self.run_script("set-chart-created-cr.sh", {
            "CRD_NAME": "odhdashboardconfigs.opendatahub.io",
            "RESOURCE": "odhdashboardconfigs.opendatahub.io/odh-dashboard-config",
            "OBJECT_NAME": "odh-dashboard-config",
            "OBJECT_NAMESPACE": "redhat-ods-applications",
            "CREATE_PAYLOAD": os.path.join(self.work, "create.json"),
            "SPEC_PAYLOAD": os.path.join(self.work, "spec.json"),
        }, absent=absent)

    def test_dashboard_create_path_smoke(self):
        self.FAMILY_FRAGMENT = "dashboard-config"
        self.stage_cm()
        result = self.run_set()
        self.assertEqual(result.returncode, 0, result.stderr)
        created = self.captured_create()
        self.assertEqual(created["kind"], "OdhDashboardConfig")
        self.assertEqual(created["metadata"]["annotations"],
                         {"openshift-ai/chart-created": "true"})
        self.assertIn("notebookController", created["spec"])
        # The create path neither patches nor deletes.
        self.assertNotIn(" patch ", self.log())
        self.assertNotIn(" delete ", self.log())


class OperandDeleteScriptTests(_OperandScriptFixture):
    """Happy-path smoke for the rendered delete-chart-created-cr script: the
    proof-gated uninstall path (deletes only under the chart-created proof
    annotation)."""

    def test_proof_gated_delete_smoke(self):
        self.FAMILY_FRAGMENT = "dashboard-config"
        self.stage_cm()
        with open(self.object_file, "w") as f:
            json.dump({"metadata": {
                "name": "odh-dashboard-config",
                "namespace": "redhat-ods-applications",
                "resourceVersion": "43",
                "annotations": {"openshift-ai/chart-created": "true"}},
                "spec": {}}, f)
        result = self.run_script("delete-chart-created-cr.sh", {
            "RESOURCE": "odhdashboardconfigs.opendatahub.io/odh-dashboard-config",
            "OBJECT_NAME": "odh-dashboard-config",
            "OBJECT_NAMESPACE": "redhat-ods-applications",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        delete_lines = [l for l in self.log().splitlines()
                        if l.startswith("delete ")]
        self.assertEqual(len(delete_lines), 1)
        self.assertIn("odhdashboardconfigs.opendatahub.io/odh-dashboard-config",
                      delete_lines[0])
        self.assertIn("redhat-ods-applications", delete_lines[0])


class OperandConvergeScriptTests(_OperandScriptFixture):
    """Happy-path smoke for the rendered converge-operator-configs script:
    the three operator-created singletons present with drifted values
    converge to one merge patch each, carrying the live resourceVersion."""

    def test_converge_smoke(self):
        self.FAMILY_FRAGMENT = "operator-configs"
        self.stage_cm()
        with open(self.auth_file, "w") as f:
            json.dump({"metadata": {"name": "auth", "resourceVersion": "50",
                                    "annotations": {}},
                       "spec": {"adminGroups": ["rhods-admins"],
                                "allowedGroups": ["old-group"]}}, f)
        with open(self.dsci_file, "w") as f:
            json.dump({"metadata": {"name": "default-dsci",
                                    "resourceVersion": "52"},
                       "spec": {"monitoring": {"managementState": "Removed"}}}, f)
        with open(self.object_file, "w") as f:
            json.dump({"metadata": {"name": "odh-segment-key-config",
                                    "namespace": "redhat-ods-applications",
                                    "resourceVersion": "51",
                                    "annotations": {}},
                       "data": {"segmentKeyEnabled": "true"}}, f)
        result = self.run_script("converge-operator-configs.sh", {
            "AUTH_PATCH": os.path.join(self.work, "auth-patch.json"),
            "DSCI_PATCH": os.path.join(self.work, "dsci-patch.json"),
            "SEGMENT_PATCH": os.path.join(self.work, "segment-key-patch.json"),
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 3)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "50"},
            "spec": {"adminGroups": ["rhods-admins"],
                     "allowedGroups": ["system:authenticated"]}})
        # The converge neither creates nor deletes the operator-owned
        # objects.
        self.assertNotIn("create", self.log())
        self.assertNotIn("delete", self.log())

    def test_wva_create_and_converge_smoke(self):
        # The value-gated WVA saturation-scaling section: with wvaOverrides
        # configured, an absent ConfigMap is created under the chart-created
        # proof annotation (the pre-delete hook walks it back on uninstall;
        # an operator-deployed instance carries no proof and is never
        # deleted), and a present one is converged by desired-subset data
        # comparison.
        self.FAMILY_FRAGMENT = "operator-configs"
        self.stage_cm(self.managed_maas() | {
            "wvaOverrides": {"scaleUp": {"pollIntervalSeconds": 30}}})
        with open(self.auth_file, "w") as f:
            json.dump({"metadata": {"name": "auth",
                                    "resourceVersion": "50"},
                       "spec": {"adminGroups": ["rhods-admins"],
                                "allowedGroups": ["old-group"]}}, f)
        with open(self.dsci_file, "w") as f:
            json.dump({"metadata": {"name": "default-dsci",
                                    "resourceVersion": "52"},
                       "spec": {"monitoring": {
                           "managementState": "Managed"}}}, f)
        with open(self.object_file, "w") as f:
            json.dump({"metadata": {"name": "odh-segment-key-config",
                                    "namespace": "redhat-ods-applications",
                                    "resourceVersion": "51"},
                       "data": {"segmentKeyEnabled": "true"}}, f)
        env_extra = {
            "AUTH_PATCH": os.path.join(self.work, "auth-patch.json"),
            "DSCI_PATCH": os.path.join(self.work, "dsci-patch.json"),
            "SEGMENT_PATCH": os.path.join(self.work, "segment-key-patch.json"),
            "WVA_CREATE": os.path.join(self.work, "wva-create.json"),
            "WVA_PATCH": os.path.join(self.work, "wva-patch.json"),
            "OC_WVA_FILE": self.wva_file,
        }
        # Create path: the ConfigMap is absent (the WVA component has not
        # deployed it yet).
        result = self.run_script("converge-operator-configs.sh",
                                 env_extra | {"OC_WVA_ABSENT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Created", result.stdout)
        created = self.captured_create()
        self.assertEqual(created["kind"], "ConfigMap")
        self.assertEqual(created["metadata"]["annotations"],
                         {"openshift-ai/chart-created": "true"})
        self.assertEqual(created["data"],
                         {"scaleUp": "pollIntervalSeconds: 30\n"})
        # The absent ConfigMap is never patched — only the three
        # operator-created surfaces above converge.
        self.assertEqual(len(self.patches()), 3)

        # Converge path: the ConfigMap exists with drifted data. The patch
        # log accumulates across both runs, so it is reset first.
        open(self.patch_log, "w").close()
        with open(self.wva_file, "w") as f:
            json.dump({"metadata": {
                            "name":
                                "workload-variant-autoscaler-saturation-scaling-config",
                            "namespace": "redhat-ods-applications",
                            "resourceVersion": "53"},
                       "data": {"scaleUp": "pollIntervalSeconds: 60"}}, f)
        result = self.run_script("converge-operator-configs.sh", env_extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Converged", result.stdout)
        patches = self.patches()
        self.assertEqual(len(patches), 4)
        self.assertEqual(patches[3], {
            "metadata": {"resourceVersion": "53"},
            "data": {"scaleUp": "pollIntervalSeconds: 30\n"}})
