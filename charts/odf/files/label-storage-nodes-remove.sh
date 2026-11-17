#!/bin/bash
# Removes the ODF storage label (cluster.ocs.openshift.io/openshift-storage)
# from the nodes named in the chart's disks values as a Helm pre-delete hook,
# walking back exactly what the set hook proved it set: a node's label is
# stripped only when its per-node proof annotation (odf/label-proof) records
# that this chart set it, and the proof annotation is cleared with it. Labels
# this chart did not set — including pre-existing empty values and non-empty
# foreign values — are never touched; nodes that no longer exist are skipped.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

LABEL_KEY="cluster.ocs.openshift.io/openshift-storage"
PROOF_ANNOTATION="odf/label-proof"
NODES_FILE="${NODES_FILE:-/app/nodes.txt}"

require_oc "cannot walk back the storage node labels"
select_json_tool "cannot manipulate Node labels"

if [ ! -s "$NODES_FILE" ]; then
  echo "ERROR: the node list is missing or empty at ${NODES_FILE}; the hook ConfigMap delivery is broken" >&2
  exit 1
fi

nodes=()
while IFS= read -r node; do
  if [ -n "$node" ]; then
    nodes+=("$node")
  fi
done < "$NODES_FILE"
if [ "${#nodes[@]}" -eq 0 ]; then
  echo "ERROR: the node list at ${NODES_FILE} carries no node names" >&2
  exit 1
fi

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT

# Computes the walk-back merge patch from the live Node JSON in $1: the
# storage label removed and the proof annotation cleared when the annotation
# proves this chart set the label; only the proof annotation cleared when the
# label is absent or was overwritten by a foreign value; nothing when there
# is no proof.
compute_unlabel_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg key "$LABEL_KEY" --arg ann "$PROOF_ANNOTATION" '
        ((.metadata.annotations // {})[$ann] // "") as $proof
        | (try (.metadata.labels[$key]) catch null) as $label
        | if $proof != "true" then empty
          elif $label == "" then
            {metadata: {resourceVersion: .metadata.resourceVersion,
                        labels: {($key): null},
                        annotations: {($ann): null}}}
          else
            {metadata: {resourceVersion: .metadata.resourceVersion,
                        annotations: {($ann): null}}}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
key = os.environ["LABEL_KEY"]
ann = os.environ["PROOF_ANNOTATION"]

proof = (doc.get("metadata", {}).get("annotations") or {}).get(ann)
label = (doc.get("metadata", {}).get("labels") or {}).get(key)
if proof != "true":
    sys.exit(0)
patch = {"metadata": {
    "resourceVersion": doc["metadata"]["resourceVersion"],
    "annotations": {ann: None},
}}
if label == "":
    patch["metadata"]["labels"] = {key: None}
print(json.dumps(patch))
PYEOF
      ;;
  esac
}

for node in "${nodes[@]}"; do
  if ! oc get "node/$node" -o json --request-timeout="$REQUEST_TIMEOUT" \
        >"$workdir/$node.json" 2>"$workdir/oc.err"; then
    if grep -q '(NotFound)' "$workdir/oc.err"; then
      echo "Node $node no longer exists; nothing to walk back"
      continue
    fi
    cat "$workdir/oc.err" >&2
    exit 1
  fi
  export LABEL_KEY PROOF_ANNOTATION
  write_rc=0
  conditional_write "node/$node" compute_unlabel_patch \
    "storage label walk-back" "node $node" || write_rc=$?
  case $write_rc in
    0)
      echo "Walked back the storage label on node $node under proof; cleared the ${PROOF_ANNOTATION} annotation"
      ;;
    3)
      echo "Nothing to walk back on node $node (no proof this chart set its label)"
      ;;
    2)
      echo "Node $node disappeared mid-run; nothing to walk back"
      ;;
    *)
      exit 1
      ;;
  esac
done
