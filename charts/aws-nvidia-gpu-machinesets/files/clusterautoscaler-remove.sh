#!/bin/bash
# Walks back this chart's ClusterAutoscaler footprint as a Helm pre-delete
# hook: restores the recorded pre-chart values of the fields the set hook
# changed, removes or restores the nvidia.com/gpu entry this hook manages,
# and deletes the ClusterAutoscaler/default singleton ONLY when the proof
# annotation records that the set hook created it. Objects are never modified
# without proof:
#
# - An object without the proof annotation was never configured by this
#   chart — it is left exactly as it is.
# - An object created by this hook (proof records the creation) is deleted
#   outright.
# - Otherwise the recorded priors are restored field by field: a recorded
#   null restores the field's absence (the key is removed). The gpus array
#   is rebuilt whole ONLY when the proof records a gpuEntry — a recorded
#   prior replaces our nvidia.com/gpu entry in place (or is appended when
#   the entry was removed by hand in the meantime), a recorded null removes
#   our entry, and an unrecorded gpuEntry (the entry already equaled this
#   chart's values when first seen) leaves the array untouched. The gpus
#   list is atomic in
#   the CRD, so it is always written as a whole-array read-modify-write
#   carrying the live metadata.resourceVersion: a stale version is rejected
#   with HTTP 409 and retried.
# - The proof annotation is cleared once its state has been restored.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

AUTOSCALER="clusterautoscalers.autoscaling.openshift.io/default"
PROOF_ANNOTATION="aws-nvidia-gpu-machinesets/proof"

require_oc "cannot walk back the ClusterAutoscaler integration"
select_json_tool "cannot manipulate the ClusterAutoscaler spec"

: "${PROOF_ANNOTATION:?PROOF_ANNOTATION must be set}"
export PROOF_ANNOTATION

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT
CA_FILE="$workdir/ca.json"
GET_ERR="$workdir/get.err"

# Prints the value of annotation $2 from the ClusterAutoscaler JSON in $1,
# or an empty string when it is absent.
proof_annotation() {
  case "$JSON_TOOL" in
    jq)
      jq -r --arg ann "$2" '.metadata.annotations[$ann] // ""' "$1"
      ;;
    python3)
      python3 - "$1" "$2" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
print((doc.get("metadata", {}).get("annotations") or {}).get(sys.argv[2]) or "")
PYEOF
      ;;
  esac
}

# Prints the value of key $1 from the proof-annotation JSON read from stdin;
# exits 1 after a diagnostic when the input is not the JSON object the set
# hook writes (fail-closed: an unreadable proof state never authorizes
# writes).
proof_field() {
  case "$JSON_TOOL" in
    jq)
      # -R -s reads the whole annotation as one string so a malformed value
      # fails closed with the same curated diagnostic as the python3 path
      # instead of a raw jq parse error
      jq -r -R -s --arg key "$1" --arg ann "$PROOF_ANNOTATION" '
        (try fromjson catch null) as $proof
        | if ($proof | type) != "object" then
            ("the " + $ann + " annotation on the ClusterAutoscaler is not a "
             + "JSON object — refusing to modify an object whose proof state "
             + "cannot be read")
            | halt_error(1)
          else ($proof[$key] // "false") end'
      ;;
    python3)
      python3 -c 'import json, sys

try:
    proof = json.load(sys.stdin)
except ValueError:
    proof = None
if not isinstance(proof, dict):
    sys.stderr.write(
        "the " + sys.argv[2] + " annotation on the ClusterAutoscaler is not "
        "a JSON object — refusing to modify an object whose proof state "
        "cannot be read\n")
    sys.exit(1)
print(json.dumps(proof.get(sys.argv[1], False)))' "$1" "$PROOF_ANNOTATION"
      ;;
  esac
}

# Computes the restore merge patch from the live ClusterAutoscaler JSON in
# $1: the recorded priors for the managed fields (a recorded null deletes
# the key, restoring its absence), the rebuilt gpus array (rebuilt only
# when the proof records a gpuEntry), and the cleared proof annotation. Emits nothing when nothing of this chart's
# remains to restore; exits 4 after its own diagnostic on a proof annotation
# this chart's hook could not have written (present but not a JSON object).
compute_restore_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg ann "$PROOF_ANNOTATION" '
        . as $doc
        | ($doc.spec // {}) as $spec
        | (($doc.metadata.annotations[$ann] // "{}")
           | try fromjson catch null) as $proof
        | if ($proof | type) != "object" then
            ("the " + $ann + " annotation on the ClusterAutoscaler is not a "
             + "JSON object — refusing to modify an object whose proof state "
             + "cannot be read")
            | halt_error(4)
          else . end
        | ($spec.podPriorityThreshold // null) as $curPrio
        | ($spec.scaleDown // null) as $curSD
        | (($spec.resourceLimits // {}).gpus // []) as $gpus
        | ($proof.podPriorityThreshold) as $prioPrior
        | ($proof.scaleDown) as $sdPrior
        | ($proof.gpuEntry) as $gpuPrior
        | ($proof | has("podPriorityThreshold")) as $prioRecorded
        | ($proof | has("scaleDown")) as $sdRecorded
        | ($proof | has("gpuEntry")) as $gpuRecorded
        | ([($gpus | to_entries[])
            | select(.value.type == "nvidia.com/gpu")][0]) as $gpuAt
        | (if ($gpuRecorded | not) then
             $gpus
           elif $gpuAt != null then
             ($gpus[:$gpuAt.key]
              + (if $gpuPrior != null then [$gpuPrior] else [] end)
              + $gpus[($gpuAt.key + 1):])
           elif $gpuPrior != null then
             $gpus + [$gpuPrior]
           else
             $gpus
           end) as $wantGpus
        | (if $prioRecorded and ($prioPrior != $curPrio)
           then {podPriorityThreshold: $prioPrior} else {} end) as $prioFix
        | (if $sdRecorded and ($sdPrior != $curSD)
           then {scaleDown: $sdPrior} else {} end) as $sdFix
        | (if $gpus != $wantGpus
           then {resourceLimits: {gpus: $wantGpus}} else {} end) as $gpuFix
        | ($prioFix + $sdFix + $gpuFix) as $specFix
        | if $specFix == {} then
            {metadata: {resourceVersion: $doc.metadata.resourceVersion,
                        annotations: {($ann): null}}}
          else
            {metadata: {resourceVersion: $doc.metadata.resourceVersion,
                        annotations: {($ann): null}},
             spec: $specFix}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
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

spec = doc.get("spec") or {}
gpus = (spec.get("resourceLimits") or {}).get("gpus") or []
cur_prio = spec.get("podPriorityThreshold")
cur_sd = spec.get("scaleDown")

prio_prior = proof.get("podPriorityThreshold")
sd_prior = proof.get("scaleDown")
gpu_prior = proof.get("gpuEntry")
prio_recorded = "podPriorityThreshold" in proof
sd_recorded = "scaleDown" in proof
gpu_recorded = "gpuEntry" in proof

gpu_idx = next(
    (i for i, g in enumerate(gpus) if g.get("type") == "nvidia.com/gpu"), None)
if not gpu_recorded:
    # An unrecorded gpuEntry means the entry already equaled this chart's
    # values when the set hook first saw it — a pre-existing entry this
    # hook never changed is never deleted
    want_gpus = gpus
elif gpu_idx is not None:
    want_gpus = (gpus[:gpu_idx]
                 + ([gpu_prior] if gpu_prior is not None else [])
                 + gpus[gpu_idx + 1:])
elif gpu_prior is not None:
    want_gpus = gpus + [gpu_prior]
else:
    want_gpus = gpus

spec_fix = {}
if prio_recorded and prio_prior != cur_prio:
    spec_fix["podPriorityThreshold"] = prio_prior
if sd_recorded and sd_prior != cur_sd:
    spec_fix["scaleDown"] = sd_prior
if want_gpus != gpus:
    spec_fix["resourceLimits"] = {"gpus": want_gpus}

patch = {"metadata": {"resourceVersion": doc["metadata"]["resourceVersion"],
                      "annotations": {ann: None}}}
if spec_fix:
    patch["spec"] = spec_fix
print(json.dumps(patch))
PYEOF
      ;;
  esac
}

present=1
if ! oc get "$AUTOSCALER" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$CA_FILE" 2>"$GET_ERR"; then
  present=0
  if ! grep -q '(NotFound)' "$GET_ERR"; then
    cat "$GET_ERR" >&2
    exit 1
  fi
fi
if [ "$present" -eq 0 ]; then
  echo "The ClusterAutoscaler/default singleton is absent; nothing to remove"
  exit 0
fi

ANNOTATION=$(proof_annotation "$CA_FILE" "$PROOF_ANNOTATION")
if [ -z "$ANNOTATION" ]; then
  echo "The ClusterAutoscaler/default singleton carries no ${PROOF_ANNOTATION} annotation — it was never configured by this chart; leaving it in place"
  exit 0
fi

CREATED=$(printf '%s' "$ANNOTATION" | proof_field "created")
if [ "$CREATED" = "true" ]; then
  delete_rc=0
  delete_object "the ClusterAutoscaler/default singleton" "$AUTOSCALER" \
    || delete_rc=$?
  case $delete_rc in
    0)
      echo "Deleted the ClusterAutoscaler/default singleton this chart created"
      ;;
    3)
      echo "The ClusterAutoscaler/default singleton is already absent; nothing to remove"
      ;;
    *)
      exit 1
      ;;
  esac
  exit 0
fi

write_rc=0
conditional_write "$AUTOSCALER" compute_restore_patch \
  "ClusterAutoscaler recorded priors" "ClusterAutoscaler/default" \
  || write_rc=$?
case $write_rc in
  0)
    echo "Restored the recorded pre-chart values of this chart's managed ClusterAutoscaler fields and cleared the ${PROOF_ANNOTATION} annotation"
    ;;
  3)
    echo "Nothing of this chart's remains on the ClusterAutoscaler/default beyond the proof annotation; cleared it"
    ;;
  2)
    echo "The ClusterAutoscaler/default singleton disappeared mid-run; nothing to restore"
    ;;
  *)
    exit 1
    ;;
esac
