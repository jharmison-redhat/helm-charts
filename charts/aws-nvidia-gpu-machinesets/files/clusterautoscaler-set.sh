#!/bin/bash
# Configures the platform ClusterAutoscaler singleton
# (autoscaling.openshift.io/v1, name "default") with this chart's GPU
# autoscaling policy, as a Helm post-install/post-upgrade hook. The
# singleton may pre-exist with foreign configuration, so this release never
# owns the object and takes over ONLY the fields it manages:
# spec.podPriorityThreshold, spec.scaleDown, and the nvidia.com/gpu entry of
# spec.resourceLimits.gpus.
#
# Record-and-restore contract: when the object is absent, the hook creates
# it with the full desired spec and a proof annotation marking the creation.
# When it exists, each managed field that differs from the desired value has
# its pre-change value recorded in the proof annotation (the record merges
# priors across runs and never overwrites an already-recorded prior — the
# earliest observed state wins), and our value is then set by value. Fields
# this hook does not manage (maxNodesTotal, the cores/memory limits, gpus
# entries for other accelerator types) are never touched, so an existing
# autoscaler configuration coexists with this chart. The remove hook
# restores the recorded priors and deletes the object only when the proof
# annotation records that this hook created it.
#
# spec.resourceLimits.gpus carries no x-kubernetes-list-type in the CRD, so
# the list is atomic: it is always modified as a whole-array read-modify-
# write carrying the live metadata.resourceVersion (a stale version is
# rejected with HTTP 409 and retried), preserving every foreign entry in
# place.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

AUTOSCALER="clusterautoscalers.autoscaling.openshift.io/default"
CONFIG_FILE="${CONFIG_FILE:-/mnt/autoscaler.json}"
PROOF_ANNOTATION="aws-nvidia-gpu-machinesets/proof"

require_oc "cannot configure the ClusterAutoscaler"
select_json_tool "cannot manipulate the ClusterAutoscaler spec"

: "${PROOF_ANNOTATION:?PROOF_ANNOTATION must be set}"
export PROOF_ANNOTATION CONFIG_FILE
if [ ! -f "$CONFIG_FILE" ]; then
  echo "ERROR: the desired ClusterAutoscaler configuration is missing at ${CONFIG_FILE}; the hook ConfigMap delivery is broken" >&2
  exit 1
fi

# Computes the set merge patch from the live ClusterAutoscaler JSON in $1:
# the changed managed fields, the whole gpus array, and the merged proof
# annotation. Emits nothing when every managed field already equals the
# desired value and the record needs no update; exits 4 after its own
# diagnostic on a proof annotation this chart's hook could not have written
# (present but not a JSON object).
compute_set_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg ann "$PROOF_ANNOTATION" --slurpfile cfg "$CONFIG_FILE" '
        . as $doc
        | ($cfg[0].config) as $cfg
        | ($doc.spec // {}) as $spec
        | (($doc.metadata.annotations[$ann] // "{}")
           | try fromjson catch null) as $proof
        | if ($proof | type) != "object" then
            ("the " + $ann + " annotation on the ClusterAutoscaler is not a "
             + "JSON object — refusing to modify an object whose proof state "
             + "cannot be read")
            | halt_error(4)
          else . end
        | ($proof.created == true) as $created
        | ($spec.podPriorityThreshold // null) as $curPrio
        | ($cfg.podPriorityThreshold // null) as $wantPrio
        | ($spec.scaleDown // null) as $curSD
        | ($cfg.scaleDown // null) as $wantSD
        | ([(($spec.resourceLimits // {}).gpus // [])[]
            | select(.type == "nvidia.com/gpu")][0] // null) as $curGpu
        | ($cfg.gpuEntry // null) as $wantGpu
        | ($curPrio != $wantPrio) as $prioChanged
        | ($curSD != $wantSD) as $sdChanged
        | ($curGpu != $wantGpu) as $gpuChanged
        | ($proof
           + (if $prioChanged and ($created | not)
                 and ($proof | has("podPriorityThreshold") | not)
              then {podPriorityThreshold: $curPrio} else {} end)
           + (if $sdChanged and ($created | not)
                 and ($proof | has("scaleDown") | not)
              then {scaleDown: $curSD} else {} end)
           + (if $gpuChanged and ($created | not)
                 and ($proof | has("gpuEntry") | not)
              then {gpuEntry: $curGpu} else {} end)) as $newProof
        | (if $prioChanged then {podPriorityThreshold: $wantPrio} else {} end) as $prioPart
        | (if $sdChanged then {scaleDown: $wantSD} else {} end) as $sdPart
        | (if $gpuChanged
           then {resourceLimits:
                   {gpus: ((($spec.resourceLimits // {}).gpus // [])
                           | map(select(.type != "nvidia.com/gpu"))
                           + [$wantGpu])}}
           else {} end) as $gpuPart
        | if ($prioChanged or $sdChanged or $gpuChanged)
              or ($newProof != $proof)
          then
            {metadata:
               {resourceVersion: $doc.metadata.resourceVersion,
                annotations: {($ann): ($newProof | tojson)}},
             spec: ($prioPart + $sdPart + $gpuPart)}
            | with_entries(select(.value != {}))
          else empty end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
with open(os.environ["CONFIG_FILE"]) as f:
    cfg = json.load(f)["config"]
ann = os.environ["PROOF_ANNOTATION"]

proof = None
try:
    proof = json.loads(
        (doc.get("metadata", {}).get("annotations") or {}).get(ann) or "{}")
except ValueError:
    proof = None
if not isinstance(proof, dict):
    sys.stderr.write(
        "the " + ann + " annotation on the ClusterAutoscaler is not a JSON "
        "object — refusing to modify an object whose proof state cannot be "
        "read\n")
    sys.exit(4)
created = proof.get("created") is True
spec = doc.get("spec") or {}

cur_prio = spec.get("podPriorityThreshold")
want_prio = cfg.get("podPriorityThreshold")
cur_sd = spec.get("scaleDown")
want_sd = cfg.get("scaleDown")
gpus = (spec.get("resourceLimits") or {}).get("gpus") or []
cur_gpu = next((g for g in gpus if g.get("type") == "nvidia.com/gpu"), None)
want_gpu = cfg.get("gpuEntry")

prio_changed = cur_prio != want_prio
sd_changed = cur_sd != want_sd
gpu_changed = cur_gpu != want_gpu

new_proof = dict(proof)
if not created:
    if prio_changed and "podPriorityThreshold" not in proof:
        new_proof["podPriorityThreshold"] = cur_prio
    if sd_changed and "scaleDown" not in proof:
        new_proof["scaleDown"] = cur_sd
    if gpu_changed and "gpuEntry" not in proof:
        new_proof["gpuEntry"] = cur_gpu

spec_patch = {}
if prio_changed:
    spec_patch["podPriorityThreshold"] = want_prio
if sd_changed:
    spec_patch["scaleDown"] = want_sd
if gpu_changed:
    spec_patch["resourceLimits"] = {
        "gpus": [g for g in gpus if g.get("type") != "nvidia.com/gpu"]
                + [want_gpu],
    }
if (not spec_patch) and new_proof == proof:
    sys.exit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"],
                 "annotations": {ann: json.dumps(new_proof)}},
    "spec": spec_patch,
}))
PYEOF
      ;;
  esac
}

# Prints the create manifest (with the creation-proof annotation baked in)
# from the hook configuration.
print_manifest() {
  case "$JSON_TOOL" in
    jq)
      jq -r '.manifest' "$CONFIG_FILE"
      ;;
    python3)
      python3 -c 'import json, os

with open(os.environ["CONFIG_FILE"]) as f:
    print(json.dumps(json.load(f)["manifest"]))'
      ;;
  esac
}

create_singleton() {
  create_rc=0
  print_manifest \
    | create_object "the ClusterAutoscaler/default singleton" -f - \
    || create_rc=$?
  case $create_rc in
    0)
      echo "Created the ClusterAutoscaler/default singleton with this chart's GPU autoscaling policy"
      ;;
    3)
      echo "ERROR: the ClusterAutoscaler/default singleton was created concurrently; re-running this hook merges into it" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
}

write_rc=0
conditional_write "$AUTOSCALER" compute_set_patch \
  "ClusterAutoscaler GPU policy" "ClusterAutoscaler/default" || write_rc=$?
case $write_rc in
  0)
    echo "Configured this chart's managed ClusterAutoscaler fields; pre-existing values of the changed fields are recorded in the ${PROOF_ANNOTATION} annotation"
    ;;
  3)
    echo "The ClusterAutoscaler/default already carries this chart's configuration; no changes needed"
    ;;
  2)
    # The singleton is absent: create it with the creation-proof annotation
    create_singleton
    ;;
  *)
    exit 1
    ;;
esac
