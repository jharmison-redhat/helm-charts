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
    """Render contracts for the operand configuration hook surfaces: the
    four CRD-ordering-blocked product objects never render as release
    manifests, each hook family carries its gated Jobs and scoped RBAC, and
    the by-value payload shapes reach the script ConfigMaps."""

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

    def test_hook_jobs(self):
        # The chart-created families carry a set Job AND a pre-delete proof
        # -gated delete Job; the operator-configs family converges by value
        # and creates the WVA ConfigMap only when absent, so its pre-delete
        # hook walks back only a chart-created instance (proof-gated) while
        # operator-created surfaces are never deleted.
        expected_jobs = {"operator-configs": 2, "dashboard-config": 2,
                         "mlflow-": 2}
        by_kind = self.by_kind(self.render())
        for fragment, count in expected_jobs.items():
            fam = self.family(by_kind, fragment)
            # Supporting resources are ordinary release manifests in the
            # release namespace.
            self.assertEqual(len(fam["ServiceAccount"]), 1, fragment)
            self.assertEqual(fam["ServiceAccount"][0]["metadata"]["namespace"],
                             "test-release-ns")
            # Set Jobs run post-install/post-upgrade, delete Jobs pre-delete;
            # every hook Job is recreated via before-hook-creation.
            jobs = {j["metadata"]["name"]: j for j in fam["Job"]}
            self.assertEqual(len(jobs), count, fragment)
            for name, job in jobs.items():
                delete_policy = job["metadata"]["annotations"][
                    "helm.sh/hook-delete-policy"]
                self.assertEqual(delete_policy, "before-hook-creation", name)
                self.assertEqual(job["spec"]["backoffLimit"], 0, name)
            set_jobs = [j for j in fam["Job"]
                        if not j["metadata"]["name"].endswith("-delete")]
            delete_jobs = [j for j in fam["Job"]
                           if j["metadata"]["name"].endswith("-delete")]
            self.assertEqual(len(set_jobs), 1, fragment)
            self.assertEqual(len(delete_jobs), count - 1, fragment)
            self.assertEqual(set_jobs[0]["metadata"]["annotations"]["helm.sh/hook"],
                             "post-install,post-upgrade")
            self.assertEqual(set_jobs[0]["spec"]["activeDeadlineSeconds"], 270)
            for job in delete_jobs:
                self.assertEqual(job["metadata"]["annotations"]["helm.sh/hook"],
                                 "pre-delete", fragment)
                self.assertEqual(job["spec"]["activeDeadlineSeconds"], 300)

    def test_chart_created_cr_rbac(self):
        by_kind = self.by_kind(self.render())
        for fragment, group, plural, crd, name in (
                ("dashboard-config", "opendatahub.io", "odhdashboardconfigs",
                 "odhdashboardconfigs.opendatahub.io", "odh-dashboard-config"),
                ("mlflow-", "mlflow.opendatahub.io", "mlflows",
                 "mlflows.mlflow.opendatahub.io", "mlflow")):
            rules = self.family(by_kind, fragment)["ClusterRole"][0]["rules"]
            self.assertEqual(rules, [
                {"apiGroups": [group], "resources": [plural],
                 "resourceNames": [name],
                 "verbs": ["get", "patch", "delete"]},
                # create is the one grant RBAC cannot scope by name — it
                # cannot match inside a resourceNames-scoped rule, so it
                # carries its own unscoped rule (the set hook must create the
                # object when no default exists).
                {"apiGroups": [group], "resources": [plural],
                 "verbs": ["create"]},
                {"apiGroups": ["apiextensions.k8s.io"],
                 "resources": ["customresourcedefinitions"],
                 "resourceNames": [crd], "verbs": ["get"]},
            ], fragment)

    def test_operator_configs_rbac(self):
        # The operator-created singletons are never created or deleted by
        # this chart: get/patch only, resource-name-scoped. The WVA
        # ConfigMap additionally carries delete (its proof-gated walk-back —
        # unconditional, since the upgrade-after-create case needs it). The
        # unscoped create grant renders only when wvaOverrides is
        # configured: create is the one verb RBAC cannot scope by name,
        # spent on creating the ConfigMap when the operator has not
        # deployed it yet, and an install without configured overrides
        # carries no create grant at all.
        rules = self.family(self.by_kind(self.render()),
                            "operator-configs")["ClusterRole"][0]["rules"]
        self.assertEqual(rules, [
            {"apiGroups": ["services.platform.opendatahub.io"],
             "resources": ["auths"], "resourceNames": ["auth"],
             "verbs": ["get", "patch"]},
            {"apiGroups": ["dscinitialization.opendatahub.io"],
             "resources": ["dscinitializations"],
             "resourceNames": ["default-dsci"],
             "verbs": ["get", "patch"]},
            {"apiGroups": [""], "resources": ["configmaps"],
             "resourceNames": ["odh-segment-key-config"],
             "verbs": ["get", "patch"]},
            {"apiGroups": [""], "resources": ["configmaps"],
             "resourceNames":
                 ["workload-variant-autoscaler-saturation-scaling-config"],
             "verbs": ["get", "patch", "delete"]},
            {"apiGroups": ["apiextensions.k8s.io"],
             "resources": ["customresourcedefinitions"],
             "resourceNames": ["auths.services.platform.opendatahub.io"],
             "verbs": ["get"]},
        ])
        values = self.managed_maas() | {
            "wvaOverrides": {"scaleUp": {"pollIntervalSeconds": 30}}}
        rules = self.family(self.by_kind(self.render(values)),
                            "operator-configs")["ClusterRole"][0]["rules"]
        self.assertIn(
            {"apiGroups": [""], "resources": ["configmaps"],
             "verbs": ["create"]}, rules)

    def test_dashboard_config_payloads(self):
        by_kind = self.by_kind(self.render())
        cm = self.family(by_kind, "dashboard-config")["ConfigMap"][0]
        create = json.loads(cm["data"]["create.json"])
        self.assertEqual(create["apiVersion"], "opendatahub.io/v1alpha")
        self.assertEqual(create["kind"], "OdhDashboardConfig")
        self.assertEqual(create["metadata"]["name"], "odh-dashboard-config")
        self.assertEqual(create["metadata"]["namespace"],
                         "redhat-ods-applications")
        self.assertEqual(create["metadata"]["labels"],
                         {"app.kubernetes.io/part-of": "rhods-dashboard",
                          "app.opendatahub.io/rhods-dashboard": "true"})
        self.assertEqual(create["metadata"]["annotations"],
                         {"openshift-ai/chart-created": "true"})
        self.assertEqual(create["spec"], {
            "notebookController": {"enabled": True,
                                   "notebookNamespace": "rhods-notebooks",
                                   "pvcSize": "20Gi"}})
        self.assertEqual(json.loads(cm["data"]["spec.json"]),
                         {"spec": create["spec"]})

        values = self.managed_maas() | {
            "dashboardConfig": {"disableTracking": True, "drop": None},
            "templateDisablement": ["trustyai-cpu-small"],
            "templateOrder": ["jupyter-minimal-notebook"],
            "showJupyterInEnabledApplications": False,
            "defaultWorkbenchPvcSize": "40Gi",
        }
        by_kind = self.by_kind(self.render(values))
        cm = self.family(by_kind, "dashboard-config")["ConfigMap"][0]
        create = json.loads(cm["data"]["create.json"])
        self.assertEqual(create["spec"], {
            "notebookController": {"enabled": False,
                                   "notebookNamespace": "rhods-notebooks",
                                   "pvcSize": "40Gi"},
            "dashboardConfig": {"disableTracking": True, "drop": None},
            "templateDisablement": ["trustyai-cpu-small"],
            "templateOrder": ["jupyter-minimal-notebook"],
        })
        # An explicit null in dashboardConfig survives to the payload
        # (merge-patch removal semantics).
        self.assertIsNone(create["spec"]["dashboardConfig"]["drop"])

    def test_mlflow_payloads_and_gate(self):
        by_kind = self.by_kind(self.render())
        cm = self.family(by_kind, "mlflow-")["ConfigMap"][0]
        create = json.loads(cm["data"]["create.json"])
        self.assertEqual(create["apiVersion"], "mlflow.opendatahub.io/v1")
        self.assertEqual(create["kind"], "MLflow")
        self.assertEqual(create["metadata"]["name"], "mlflow")
        self.assertEqual(create["metadata"]["annotations"],
                         {"openshift-ai/chart-created": "true"})
        self.assertEqual(create["spec"]["backendStoreUri"],
                         "sqlite:////mlflow/mlflow.db")
        self.assertEqual(json.loads(cm["data"]["spec.json"]),
                         {"spec": create["spec"]})

        # Disabling MLflow removes ONLY the set hook and its create/spec
        # payloads: the proof-gated delete plumbing (delete Job, ServiceAccount,
        # ClusterRole, ClusterRoleBinding, script ConfigMap) renders
        # unconditionally so a chart-created MLflow is still walked back on
        # uninstall instead of being orphaned with its proof annotation.
        values = self.managed_maas() | {"mlflow": {"create": False}}
        by_kind = self.by_kind(self.render(values))
        fam = self.family(by_kind, "mlflow-")
        self.assertEqual(len(fam["Job"]), 1)
        self.assertTrue(fam["Job"][0]["metadata"]["name"].endswith("-delete"))
        self.assertEqual(fam["Job"][0]["metadata"]["annotations"]["helm.sh/hook"],
                         "pre-delete")
        self.assertEqual(len(fam["ServiceAccount"]), 1)
        self.assertEqual(len(fam["ClusterRole"]), 1)
        self.assertEqual(len(fam["ClusterRoleBinding"]), 1)
        self.assertEqual(len(fam["ConfigMap"]), 1)
        cm = fam["ConfigMap"][0]
        self.assertNotIn("create.json", cm["data"])
        self.assertNotIn("spec.json", cm["data"])
        self.assertIn("delete-chart-created-cr.sh", cm["data"])
        # The other families are unaffected.
        self.assertEqual(len(self.family(by_kind,
                                         "dashboard-config")["Job"]), 2)
        self.assertEqual(len(self.family(by_kind,
                                         "operator-configs")["Job"]), 2)

    def test_operator_configs_payloads(self):
        by_kind = self.by_kind(self.render())
        cm = self.family(by_kind, "operator-configs")["ConfigMap"][0]
        self.assertEqual(json.loads(cm["data"]["auth-patch.json"]), {
            "spec": {"adminGroups": ["rhods-admins"],
                     "allowedGroups": ["system:authenticated"]}})
        self.assertEqual(json.loads(cm["data"]["dsci-patch.json"]), {
            "spec": {"monitoring": {
                "managementState": "Managed",
                "namespace": "redhat-ods-monitoring",
                "metrics": {"storage": {"size": "5Gi", "retention": "1d"}}}}})
        self.assertEqual(json.loads(cm["data"]["segment-key-patch.json"]),
                         {"data": {"segmentKeyEnabled": "false"}})
        # The WVA saturation-scaling payloads are value-gated: with no
        # configured overrides the ConfigMap carries none of them, so the
        # converge script's WVA section never runs.
        self.assertNotIn("wva-create.json", cm["data"])
        self.assertNotIn("wva-patch.json", cm["data"])

        values = self.managed_maas() | {
            "userGroups": ["my-admins", "other-group"],
            "enableUserTelemetry": True,
            "wvaOverrides": {"scaleUp": {"pollIntervalSeconds": 30}},
            "dataScienceClusterInitialization": {
                "monitoring": {"managementState": "Removed",
                               "metrics": None,
                               "traces": None}},
        }
        by_kind = self.by_kind(self.render(values))
        cm = self.family(by_kind, "operator-configs")["ConfigMap"][0]
        self.assertEqual(json.loads(cm["data"]["auth-patch.json"]), {
            "spec": {"adminGroups": ["rhods-admins"],
                     "allowedGroups": ["my-admins", "other-group"]}})
        # The DSCI payload carries exactly the configured spec fields. Helm
        # coalescing DELETES a null override of a chart-default leaf
        # (metrics drops out of the payload entirely — value removal), while
        # an explicit null on a key absent from the chart defaults survives
        # (merge-patch removal semantics).
        self.assertEqual(json.loads(cm["data"]["dsci-patch.json"]), {
            "spec": {"monitoring": {
                "managementState": "Removed",
                "namespace": "redhat-ods-monitoring",
                "traces": None}}})
        self.assertEqual(json.loads(cm["data"]["segment-key-patch.json"]),
                         {"data": {"segmentKeyEnabled": "true"}})
        # Each wvaOverrides entry becomes a ConfigMap data key carrying the
        # value as YAML text with a trailing newline: block-scalar values
        # (how the WVA operator's bundle writes them, and how objects
        # created in place by other writers read back) end with one, so
        # compute_wva_patch's exact-string comparison is idempotent instead
        # of re-patching on every hook run. The create payload is the full
        # ConfigMap under the chart-created proof annotation, the patch
        # payload only the desired data.
        self.assertEqual(json.loads(cm["data"]["wva-create.json"]), {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {
                "name":
                    "workload-variant-autoscaler-saturation-scaling-config",
                "namespace": "redhat-ods-applications",
                "annotations": {"openshift-ai/chart-created": "true"}},
            "data": {"scaleUp": "pollIntervalSeconds: 30\n"}})
        self.assertEqual(json.loads(cm["data"]["wva-patch.json"]),
                         {"data": {"scaleUp": "pollIntervalSeconds: 30\n"}})

        # Emptying the values block empties the desired spec: the converge
        # is a no-op rather than an attempt to patch anything.
        values = self.managed_maas() | {"dataScienceClusterInitialization": None}
        by_kind = self.by_kind(self.render(values))
        cm = self.family(by_kind, "operator-configs")["ConfigMap"][0]
        self.assertEqual(json.loads(cm["data"]["dsci-patch.json"]),
                         {"spec": {}})

    def test_enable_user_telemetry_validated(self):
        # The segment-key payload stringifies the value, so a null (which also
        # deletes the values.yaml default) or any non-boolean fails the render
        # with an actionable message instead of rendering "<nil>" or
        # non-boolean text.
        for v in (None, "true", 1, "yes"):
            result = self.render(
                self.managed_maas() | {"enableUserTelemetry": v},
                expect_failure=True)
            self.assertIn("enableUserTelemetry must be a boolean", result.stderr)
        for v, rendered in ((True, "true"), (False, "false")):
            values = self.managed_maas() | {"enableUserTelemetry": v}
            cm = self.family(self.by_kind(self.render(values)),
                             "operator-configs")["ConfigMap"][0]
            self.assertEqual(json.loads(cm["data"]["segment-key-patch.json"]),
                             {"data": {"segmentKeyEnabled": rendered}})

    def test_dsc_component_composition_validated(self):
        # An explicitly Removed component never establishes its CRDs, so the
        # matching hook surface would burn its full wait budget: the render
        # fails naming both keys instead.
        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["mlflowoperator"] = {
            "managementState": "Removed"}
        result = self.render(values, expect_failure=True)
        self.assertIn("dataScienceCluster.components.mlflowoperator", result.stderr)
        self.assertIn("mlflow.create", result.stderr)

        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["dashboard"] = {
            "managementState": "Removed"}
        result = self.render(values, expect_failure=True)
        self.assertIn("dataScienceCluster.components.dashboard", result.stderr)

        # Disabling the MLflow set hook skips its check — the ungated delete
        # plumbing still renders.
        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["mlflowoperator"] = {
            "managementState": "Removed"}
        values["mlflow"] = {"create": False}
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(len(self.family(by_kind, "mlflow-")["Job"]), 1)

        # The chart not rendering the DataScienceCluster leaves the
        # components tree to the operator: nothing fails.
        values = self.managed_maas()
        values["dataScienceCluster"]["components"]["mlflowoperator"] = {
            "managementState": "Removed"}
        values["dataScienceCluster"]["create"] = False
        self.render(values)

    def test_wait_timeout_validation(self):
        for v in (0, -5, "240", 1.5, True, None):
            self.render(self.managed_maas()
                        | {"operandHookWaitTimeoutSeconds": v},
                        expect_failure=True)
        values = self.managed_maas() | {"operandHookWaitTimeoutSeconds": 480}
        by_kind = self.by_kind(self.render(values))
        for fragment in ("operator-configs", "dashboard-config", "mlflow-"):
            for job in self.family(by_kind, fragment)["Job"]:
                env = {e["name"]: e["value"] for e in
                       job["spec"]["template"]["spec"]["containers"][0]["env"]}
                if "WAIT_TIMEOUT" in env:
                    self.assertEqual(env["WAIT_TIMEOUT"], "480", fragment)
                    self.assertEqual(job["spec"]["activeDeadlineSeconds"], 510,
                                     fragment)


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

    def test_mlflow_create_path_smoke(self):
        # The same script delivered by the mlflow family's ConfigMap proves
        # both deliveries against their family-specific payloads.
        self.FAMILY_FRAGMENT = "mlflow-"
        self.stage_cm()
        result = self.run_script("set-chart-created-cr.sh", {
            "CRD_NAME": "mlflows.mlflow.opendatahub.io",
            "RESOURCE": "mlflows.mlflow.opendatahub.io/mlflow",
            "OBJECT_NAME": "mlflow",
            "OBJECT_NAMESPACE": "redhat-ods-applications",
            "CREATE_PAYLOAD": os.path.join(self.work, "create.json"),
            "SPEC_PAYLOAD": os.path.join(self.work, "spec.json"),
        }, absent=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        created = self.captured_create()
        self.assertEqual(created["kind"], "MLflow")
        self.assertEqual(created["metadata"]["name"], "mlflow")
        self.assertIn("backendStoreUri", created["spec"])
        self.assertNotIn(" patch ", self.log())

    def test_converge_desired_subset(self):
        # The converge path compares only the DESIRED subset of the live spec:
        # a live object already carrying the desired values (plus extra fields
        # the dashboard backend writes) produces no patch, while a drifted
        # desired field still yields one merge patch carrying the live
        # resourceVersion.
        self.FAMILY_FRAGMENT = "dashboard-config"
        self.stage_cm()
        desired = {"notebookController": {"enabled": True,
                                          "notebookNamespace": "rhods-notebooks",
                                          "pvcSize": "20Gi"}}
        with open(self.object_file, "w") as f:
            json.dump({"metadata": {"name": "odh-dashboard-config",
                                    "namespace": "redhat-ods-applications",
                                    "resourceVersion": "44"},
                       "spec": desired | {"dashboardBackendOption":
                                          {"logLevel": "info"}}}, f)
        result = self.run_set(absent=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already matches", result.stdout)
        self.assertEqual(self.patches(), [])
        self.assertNotIn(" create ", self.log())

        with open(self.object_file, "w") as f:
            json.dump({"metadata": {"name": "odh-dashboard-config",
                                    "namespace": "redhat-ods-applications",
                                    "resourceVersion": "45"},
                       "spec": desired | {"notebookController":
                                          {"enabled": False,
                                           "notebookNamespace": "rhods-notebooks",
                                           "pvcSize": "20Gi"}}}, f)
        result = self.run_set(absent=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "45"},
            "spec": {"notebookController": desired["notebookController"]}})


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
        # The DSCI converge patches exactly the configured spec fields (the
        # whole desired monitoring block), leaving the rest of the live spec
        # untouched by comparison.
        self.assertEqual(patches[1], {
            "metadata": {"resourceVersion": "52"},
            "spec": {"monitoring": {
                "managementState": "Managed",
                "namespace": "redhat-ods-monitoring",
                "metrics": {"storage": {"size": "5Gi", "retention": "1d"}}}}})
        self.assertEqual(patches[2], {
            "metadata": {"resourceVersion": "51"},
            "data": {"segmentKeyEnabled": "false"}})
        # The converge neither creates nor deletes the operator-owned
        # objects.
        self.assertNotIn("create", self.log())
        self.assertNotIn("delete", self.log())
        # The resource target strings the script invokes, per oc call: the
        # fake oc serves whatever target it is asked, so a wrong resource
        # string smokes green unless the invocation surface itself is
        # asserted. Every DSCI invocation must carry the full
        # "<plural>.<group>/name" form — the API GROUP is singular
        # (dscinitialization), the plural RESOURCE dscinitializations, and
        # the bare "dscinitializations.<singular-group>" form is rejected by
        # the API. The Auth and segment-key invocations must each appear at
        # least once.
        dsci_calls = [line for line in self.log().splitlines()
                      if "dscinitializations" in line]
        self.assertTrue(dsci_calls)
        for line in dsci_calls:
            self.assertIn(
                "dscinitializations.dscinitialization.opendatahub.io/default-dsci",
                line, line)
        for target in ("auths.services.platform.opendatahub.io/auth",
                       "configmap/odh-segment-key-config"):
            self.assertTrue(any(target in line
                                for line in self.log().splitlines()), target)
        # Exactly one -n per namespaced oc invocation, always followed by the
        # segment-key namespace — the class of bug where a "-n <ns>" argument
        # pair leaks into conditional_write's namespace parameter makes every
        # get 404 on a namespace named "-n". The cluster-scoped singletons
        # carry no -n at all.
        namespaced = 0
        for line in self.log().splitlines():
            argv = line.split()
            if any(a.startswith(("auths", "dscinitializations")) for a in argv):
                self.assertNotIn("-n", argv, line)
            self.assertLessEqual(argv.count("-n"), 1, line)
            if "-n" in argv:
                namespaced += 1
                self.assertEqual(argv[argv.index("-n") + 1],
                                 "redhat-ods-applications", line)
        self.assertGreater(namespaced, 0)

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
