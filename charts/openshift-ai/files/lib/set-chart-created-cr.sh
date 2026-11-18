#!/bin/bash
# Creates or converges a chart-created operand custom resource as a Helm
# post-install/post-upgrade hook. The resource's custom resource definition
# comes into existence only after the rhods-operator reconciles the
# DataScienceCluster (and, for some components, only after the component
# itself deploys), so a release manifest of the object fails a fresh install
# on the missing CRD. This hook instead waits a bounded time for the CRD to
# be established, then either creates the object — recording the
# chart-created proof annotation — or converges the live object's managed
# spec by value.
#
# A pre-existing object the chart did not create (the dashboard backend
# creates a default OdhDashboardConfig when absent) is converged but never
# annotated, so the matching pre-delete hook leaves it in place on
# uninstall. A concurrent creation between the converge probe and the create
# is converged by value on the same terms.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried (bounded, in the preamble). Merge-patch semantics preserve live
# spec fields the desired payload does not carry; an explicit null in the
# desired spec removes a field.
#
# Required environment: CRD_NAME, RESOURCE (as an oc get type/name target),
# OBJECT_NAME, OBJECT_NAMESPACE, CREATE_PAYLOAD and SPEC_PAYLOAD (paths to
# the rendered JSON payloads in this script's ConfigMap).
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

: "${CRD_NAME:?CRD_NAME is required}"
: "${RESOURCE:?RESOURCE is required}"
: "${OBJECT_NAME:?OBJECT_NAME is required}"
: "${OBJECT_NAMESPACE:?OBJECT_NAMESPACE is required}"
: "${CREATE_PAYLOAD:?CREATE_PAYLOAD is required}"
: "${SPEC_PAYLOAD:?SPEC_PAYLOAD is required}"
export PROOF_ANNOTATION="${PROOF_ANNOTATION:-openshift-ai/chart-created}"
export WAIT_TIMEOUT="${WAIT_TIMEOUT:-240}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"

require_oc "cannot configure $OBJECT_NAME"
select_json_tool "cannot configure $OBJECT_NAME"

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT

for payload in "$CREATE_PAYLOAD" "$SPEC_PAYLOAD"; do
  if [ ! -s "$payload" ]; then
    echo "ERROR: the payload file is missing or empty at $payload; the ConfigMap delivery is broken" >&2
    exit 1
  fi
done

# Waits bounded for the CRD to be established, against the script-wide
# deadline computed below: the operator's component controllers create it
# only after the DataScienceCluster (or the owning component) is reconciled.
wait_for_crd() {
  local rc=0 established
  while :; do
    established=$(oc get "crd/$CRD_NAME" \
      -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' \
      --request-timeout="$REQUEST_TIMEOUT" 2>"$workdir/oc.err") && rc=0 || rc=$?
    if [ "$rc" -eq 0 ] && [ "$established" = "True" ]; then
      return 0
    fi
    if [ "$rc" -ne 0 ] && ! grep -q '(NotFound)' "$workdir/oc.err"; then
      cat "$workdir/oc.err" >&2
      exit 1
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "ERROR: timed out after ${WAIT_TIMEOUT}s waiting for the $CRD_NAME custom resource definition to be established; the rhods-operator must install and reconcile the owning component first (check the operator state and the DataScienceCluster reconciliation, then re-run this hook)" >&2
      exit 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

# Computes the converge merge patch from the live object JSON in $1: the
# desired spec applied when any desired field differs from the live spec,
# nothing when the live spec already carries the desired values. Only the
# DESIRED subset of the live spec is compared — extra live fields the
# dashboard backend or the API defaults write are neither matched nor
# patched, so a converged object produces no patch on every subsequent run
# (the merge patch itself preserves unmanaged live fields).
compute_converge_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile desired "$SPEC_PAYLOAD" '
        ($desired[0].spec // {}) as $des
        | if ((.spec // {}) | with_entries(.key as $k | select($des | has($k)))) == $des
          then empty
          else {metadata: {resourceVersion: .metadata.resourceVersion},
                spec: $des}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
with open(os.environ["SPEC_PAYLOAD"]) as f:
    desired = json.load(f).get("spec") or {}

live = doc.get("spec") or {}
if {k: live[k] for k in desired if k in live} == desired:
    raise SystemExit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": desired,
}))
PYEOF
      ;;
  esac
}

# Converges the live object by value. Returns 0 when the object exists (at
# the desired values or after a successful patch) and 2 when it is absent.
converge() {
  local write_rc=0
  conditional_write "$RESOURCE" compute_converge_patch \
    "$OBJECT_NAME spec" "$OBJECT_NAME" "$OBJECT_NAMESPACE" || write_rc=$?
  case $write_rc in
    0)
      echo "Converged $OBJECT_NAME to the configured spec"
      return 0
      ;;
    3)
      echo "$OBJECT_NAME already matches the configured spec; no changes needed"
      return 0
      ;;
    2)
      return 2
      ;;
    *)
      exit 1
      ;;
  esac
}

# One deadline shared by every wait phase below (the CRD wait): the Job's
# activeDeadlineSeconds is WAIT_TIMEOUT + 30, so waits draw from one
# script-wide budget rather than each starting a full WAIT_TIMEOUT of its
# own.
DEADLINE=$(( $(date +%s) + WAIT_TIMEOUT ))

wait_for_crd

if converge; then
  exit 0
fi

# The object does not exist: create it under the chart-created proof
# annotation so the pre-delete hook can mirror release ownership.
create_rc=0
create_object "$OBJECT_NAME" -f "$CREATE_PAYLOAD" -n "$OBJECT_NAMESPACE" \
  || create_rc=$?
if [ "$create_rc" -eq 0 ]; then
  echo "Created $OBJECT_NAME (recorded in the ${PROOF_ANNOTATION} annotation)"
  exit 0
fi
if [ "$create_rc" -eq 3 ]; then
  # A concurrent actor created the object between the converge probe and the
  # create. It carries no chart proof, so uninstall leaves it in place.
  echo "$OBJECT_NAME was created concurrently; converging it by value"
  if ! converge; then
    echo "ERROR: $OBJECT_NAME was created concurrently and disappeared again before convergence; re-run this hook" >&2
    exit 1
  fi
  exit 0
fi
exit 1
