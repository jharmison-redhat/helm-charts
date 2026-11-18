#!/bin/bash
# Deletes a chart-created operand custom resource as a Helm pre-delete hook,
# mirroring release ownership without the CRD-ordering hazard: the object is
# deleted only when the chart-created proof annotation (written by the set
# hook at creation time) proves the chart created it. Objects the chart
# merely converged — created by the dashboard backend or another actor, with
# no proof annotation — are left in place, and a missing object is a no-op.
#
# Required environment: RESOURCE (as an oc get type/name target),
# OBJECT_NAME, OBJECT_NAMESPACE.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

: "${RESOURCE:?RESOURCE is required}"
: "${OBJECT_NAME:?OBJECT_NAME is required}"
: "${OBJECT_NAMESPACE:?OBJECT_NAMESPACE is required}"
export PROOF_ANNOTATION="${PROOF_ANNOTATION:-openshift-ai/chart-created}"

require_oc "cannot walk back $OBJECT_NAME"
select_json_tool "cannot walk back $OBJECT_NAME"

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT

if ! oc get "$RESOURCE" -n "$OBJECT_NAMESPACE" -o json \
      --request-timeout="$REQUEST_TIMEOUT" \
      >"$workdir/object.json" 2>"$workdir/oc.err"; then
  if grep -q '(NotFound)' "$workdir/oc.err"; then
    echo "$OBJECT_NAME no longer exists; nothing to walk back"
    exit 0
  fi
  cat "$workdir/oc.err" >&2
  exit 1
fi

case "$JSON_TOOL" in
  jq)
    proof=$(jq -r --arg ann "$PROOF_ANNOTATION" \
      '(.metadata.annotations[$ann] // "")' "$workdir/object.json")
    ;;
  python3)
    proof=$(python3 - "$workdir/object.json" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
ann = os.environ["PROOF_ANNOTATION"]
print((doc.get("metadata", {}).get("annotations") or {}).get(ann, ""))
PYEOF
)
    ;;
esac

if [ "$proof" != "true" ]; then
  echo "Leaving $OBJECT_NAME in place: no ${PROOF_ANNOTATION} proof annotation records the chart as its creator"
  exit 0
fi

delete_rc=0
delete_object "$OBJECT_NAME" "$RESOURCE" -n "$OBJECT_NAMESPACE" || delete_rc=$?
case $delete_rc in
  0)
    echo "Deleted $OBJECT_NAME under its chart-created proof annotation"
    ;;
  3)
    echo "$OBJECT_NAME no longer exists; nothing to walk back"
    ;;
  *)
    exit 1
    ;;
esac
