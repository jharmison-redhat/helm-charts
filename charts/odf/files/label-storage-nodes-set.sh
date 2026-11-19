#!/bin/bash
# Labels the nodes named in the chart's disks values with
# cluster.ocs.openshift.io/openshift-storage: "" as a Helm
# post-install/post-upgrade hook. Nodes always pre-exist (they may register
# late), so this release never renders Node manifests: the script waits a
# bounded time for every named Node, then sets the label ONLY when absent,
# recording a per-node proof annotation when it sets it. A node already
# carrying the empty label value needs no change; a node carrying a non-empty
# foreign value on that label stops the hook without writing.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried. The remove hook strips the label only where the proof annotation
# proves this hook set it.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"
source "${BASH_SOURCE[0]%/*}/wait-for.sh"

LABEL_KEY="cluster.ocs.openshift.io/openshift-storage"
PROOF_ANNOTATION="odf/label-proof"
NODES_FILE="${NODES_FILE:-/app/nodes.txt}"
NODE_WAIT_TIMEOUT="${NODE_WAIT_TIMEOUT:-300}"
NODE_POLL_INTERVAL="${NODE_POLL_INTERVAL:-5}"

require_oc "cannot label the storage nodes"
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

# Waits with one shared deadline until every named Node exists. Nodes may
# register late (e.g. freshly provisioned machines joining the cluster after
# the release was applied).
check_nodes_registered() {
  local missing=() node
  for node in "${nodes[@]}"; do
    if ! oc get "node/$node" -o json --request-timeout="$REQUEST_TIMEOUT" \
          >/dev/null 2>"$workdir/oc.err"; then
      if grep -q '(NotFound)' "$workdir/oc.err"; then
        missing+=("$node")
      else
        cat "$workdir/oc.err" >&2
        exit 1
      fi
    fi
  done
  if [ "${#missing[@]}" -eq 0 ]; then
    return 0
  fi
  WAIT_DETAIL="${missing[*]}"
  return 3
}

wait_for_nodes() {
  local deadline=$(( $(date +%s) + NODE_WAIT_TIMEOUT ))
  if ! wait_for "$deadline" "$NODE_POLL_INTERVAL" \
        'Node(s) %s not registered yet; retrying\n' \
        check_nodes_registered; then
    echo "ERROR: timed out after ${NODE_WAIT_TIMEOUT}s waiting for Node(s) ${WAIT_DETAIL} to register; nodes named in the disks values must exist on the cluster before the storage label can be applied (check the node names and cluster state), then re-run this hook" >&2
    exit 1
  fi
}

# Computes the label merge patch from the live Node JSON in $1: the storage
# label set to its empty value plus the proof annotation. Emits nothing when
# the label is already present with its empty value; exits 4 after its own
# diagnostic when the label carries a non-empty foreign value.
compute_label_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg node "$NODE_NAME" --arg key "$LABEL_KEY" \
            --arg ann "$PROOF_ANNOTATION" '
        (try (.metadata.labels[$key]) catch null) as $label
        | if ($label != null and $label != "") then
            ("node " + $node + " already carries the label " + $key
             + " with foreign value " + $label + " — refusing to overwrite "
             + "a value this chart did not set; remove or correct that label "
             + "manually, then re-run this hook")
            | halt_error(4)
          elif $label == null then
            {metadata: {resourceVersion: .metadata.resourceVersion,
                        labels: {($key): ""},
                        annotations: {($ann): "true"}}}
          else empty end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
key = os.environ["LABEL_KEY"]
ann = os.environ["PROOF_ANNOTATION"]

label = (doc.get("metadata", {}).get("labels") or {}).get(key)
if label is not None and label != "":
    sys.stderr.write(
        "node " + os.environ["NODE_NAME"] + " already carries the label "
        + key + " with foreign value " + label + " — refusing to overwrite "
        "a value this chart did not set; remove or correct that label "
        "manually, then re-run this hook\n")
    sys.exit(4)
if label is None:
    print(json.dumps({
        "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"],
                     "labels": {key: ""},
                     "annotations": {ann: "true"}},
    }))
PYEOF
      ;;
  esac
}

wait_for_nodes

for node in "${nodes[@]}"; do
  export NODE_NAME="$node" LABEL_KEY PROOF_ANNOTATION
  write_rc=0
  conditional_write "node/$node" compute_label_patch "storage label" \
    "node $node" || write_rc=$?
  case $write_rc in
    0)
      echo "Labeled node $node (proof recorded in the ${PROOF_ANNOTATION} annotation)"
      ;;
    3)
      echo "Node $node already carries the storage label; no changes needed"
      ;;
    2)
      echo "ERROR: node $node disappeared mid-run; re-run this hook" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
done
