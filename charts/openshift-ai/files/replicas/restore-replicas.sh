#!/bin/bash
# Restores the pre-existing replica counts of the rhods Dashboard and operator
# Deployments as a Helm pre-delete hook, walking back exactly what the
# enforcement surface proved it changed: a Deployment's replicas are restored
# only when its proof annotation (openshift-ai/replicas-prior) records the
# prior count, and the annotation is cleared with the restore. A prior
# recorded as "unset" means the Deployment carried no spec.replicas field when
# the chart first managed it, so the restore removes the field again.
# Deployments this chart never annotated are never touched, and Deployments
# that no longer exist are skipped.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried (bounded, in the preamble).
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

PROOF_ANNOTATION="openshift-ai/replicas-prior"
COMPONENTS_FILE="${REPLICAS_COMPONENTS_FILE:-/config/components.txt}"

require_oc "cannot walk back the replica counts"
select_json_tool "cannot restore Deployment replicas"

if [ ! -s "$COMPONENTS_FILE" ]; then
  echo "ERROR: the component list is missing or empty at ${COMPONENTS_FILE}; the ConfigMap delivery is broken" >&2
  exit 1
fi

components=()
while IFS= read -r line; do
  if [ -n "$line" ]; then
    components+=("$line")
  fi
done < "$COMPONENTS_FILE"
if [ "${#components[@]}" -eq 0 ]; then
  echo "ERROR: the component list at ${COMPONENTS_FILE} carries no components" >&2
  exit 1
fi

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT

# Reads the recorded prior from the live Deployment JSON in $1 (empty when
# the proof annotation is absent).
prior_proof() {
  case "$JSON_TOOL" in
    jq)
      jq -r --arg ann "$PROOF_ANNOTATION" \
        '(.metadata.annotations[$ann] // "")' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
ann = os.environ["PROOF_ANNOTATION"]
print((doc.get("metadata", {}).get("annotations") or {}).get(ann, ""))
PYEOF
      ;;
  esac
}

# Fails closed with a targeted diagnostic when the recorded prior is neither
# the "unset" token nor a non-negative integer: a malformed proof must stop
# the restore loudly instead of surfacing as a generic patch-computation
# failure. Exit 4 is the conditional-write contract for a compute function's
# own fatal condition.
validate_prior() {
  local proof="$1"
  case "$proof" in
    unset) return 0 ;;
  esac
  if [[ "$proof" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  echo "ERROR: deployment ${REPLICAS_DEPLOYMENT} carries a malformed prior in the ${PROOF_ANNOTATION} annotation ('${proof}' — expected a non-negative integer or the token 'unset'); refusing to restore an uninterpretable replica count" >&2
  return 4
}

# Computes the walk-back merge patch from the live Deployment JSON in $1: the
# recorded prior restored (or the field removed for an "unset" prior) and the
# proof annotation cleared when the annotation proves this chart managed the
# replicas; nothing when there is no proof. A malformed prior stops the
# restore with its own diagnostic before any patch is computed.
compute_restore_patch() {
  local proof
  proof=$(prior_proof "$1")
  if [ -n "$proof" ]; then
    validate_prior "$proof" || return "$?"
  fi
  case "$JSON_TOOL" in
    jq)
      jq -c --arg ann "$PROOF_ANNOTATION" '
        ((.metadata.annotations // {})[$ann] // "") as $proof
        | if $proof == "" then empty
          elif $proof == "unset" then
            {metadata: {resourceVersion: .metadata.resourceVersion,
                        annotations: {($ann): null}},
             spec: {replicas: null}}
          else
            {metadata: {resourceVersion: .metadata.resourceVersion,
                        annotations: {($ann): null}},
             spec: {replicas: ($proof | tonumber)}}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
ann = os.environ["PROOF_ANNOTATION"]

proof = (doc.get("metadata", {}).get("annotations") or {}).get(ann)
if not proof:
    sys.exit(0)
patch = {"metadata": {
    "resourceVersion": doc["metadata"]["resourceVersion"],
    "annotations": {ann: None},
}}
if proof == "unset":
    patch["spec"] = {"replicas": None}
else:
    patch["spec"] = {"replicas": int(proof)}
print(json.dumps(patch))
PYEOF
      ;;
  esac
}

for component in "${components[@]}"; do
  read -r namespace name _ <<< "$component"
  if ! oc get "deployment/$name" -n "$namespace" -o json \
        --request-timeout="$REQUEST_TIMEOUT" \
        >"$workdir/deployment.json" 2>"$workdir/oc.err"; then
    if grep -q '(NotFound)' "$workdir/oc.err"; then
      echo "Deployment $namespace/$name no longer exists; nothing to walk back"
      continue
    fi
    cat "$workdir/oc.err" >&2
    exit 1
  fi
  export PROOF_ANNOTATION REPLICAS_DEPLOYMENT="$namespace/$name"
  write_rc=0
  conditional_write "deployment/$name" compute_restore_patch \
    "replicas walk-back" "deployment $namespace/$name" "$namespace" || write_rc=$?
  case $write_rc in
    0)
      echo "Restored the recorded prior replicas on deployment $namespace/$name under proof; cleared the ${PROOF_ANNOTATION} annotation"
      ;;
    3)
      echo "Nothing to walk back on deployment $namespace/$name (no proof this chart managed its replicas)"
      ;;
    2)
      echo "Deployment $namespace/$name disappeared mid-run; nothing to walk back" >&2
      ;;
    *)
      exit 1
      ;;
  esac
done
