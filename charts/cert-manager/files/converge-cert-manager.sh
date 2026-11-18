#!/bin/bash
# Converges the operator-created CertManager cluster singleton by value as a
# Helm post-install/post-upgrade hook. The cert-manager Operator for Red Hat
# OpenShift creates the CertManager/cluster object at CSV install — before
# any chart release exists — so the chart never renders it as a release
# manifest, never creates or deletes it, and records no proof annotation:
# this hook converges the chart-managed field by value on every
# install/upgrade and nothing else.
#
# Reconcile fight (documented in the chart README): the operator's
# reconciles can rewrite the managed field at any time — between hook runs
# the operator's value wins.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried (bounded, in the preamble). Only the managed field is compared and
# patched; other fields the operator sets are never touched.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

CRD="certmanagers.operator.openshift.io"
RESOURCE="${CRD}/cluster"
OBJECT="CertManager singleton cluster"
export CERT_MANAGER_ARGS="${CERT_MANAGER_ARGS:-/config/cert-manager-args.json}"
export WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"

require_oc "cannot converge the CertManager operator configuration"
select_json_tool "cannot converge the CertManager operator configuration"

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT

if [ ! -s "$CERT_MANAGER_ARGS" ]; then
  echo "ERROR: the payload file is missing or empty at $CERT_MANAGER_ARGS; the ConfigMap delivery is broken" >&2
  exit 1
fi

# Waits until a resource type (CRD) is established, against the script-wide
# deadline computed below.
wait_for_crd() {
  local rc=0 established
  while :; do
    established=$(oc get "crd/$CRD" \
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
      echo "ERROR: timed out after ${WAIT_TIMEOUT}s waiting for the $CRD custom resource definition to be established; the cert-manager operator must install first (check the operator's CSV state, then re-run this hook)" >&2
      exit 1
    fi
    sleep "$POLL_INTERVAL"
  done
}

# Waits until the operator-created object exists, against the script-wide
# deadline computed below. Only absence is retried; every other read failure
# exits immediately.
wait_for_object() {
  local rc=0
  while :; do
    if oc get "$RESOURCE" -o json --request-timeout="$REQUEST_TIMEOUT" \
        >"$workdir/object.json" 2>"$workdir/oc.err"; then
      return 0
    fi
    if ! grep -q '(NotFound)' "$workdir/oc.err"; then
      cat "$workdir/oc.err" >&2
      exit 1
    fi
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "ERROR: timed out after ${WAIT_TIMEOUT}s waiting for $OBJECT to appear; the cert-manager operator creates it at CSV install (check the operator state, then re-run this hook)" >&2
      exit 1
    fi
    echo "$OBJECT not created yet; retrying" >&2
    sleep "$POLL_INTERVAL"
  done
}

# Computes the merge patch from the live object JSON in $1: the managed
# spec.controllerConfig.overrideArgs applied when it differs from the
# desired payload, nothing when it already matches. Only the managed field
# is compared — other spec fields the operator sets are neither matched nor
# patched (the merge patch itself preserves unmanaged live fields).
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile desired "$CERT_MANAGER_ARGS" '
        ($desired[0].spec.controllerConfig.overrideArgs) as $des
        | if ((.spec.controllerConfig.overrideArgs // []) == $des) then empty
          else {metadata: {resourceVersion: .metadata.resourceVersion},
                spec: {controllerConfig: {overrideArgs: $des}}}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
with open(os.environ["CERT_MANAGER_ARGS"]) as f:
    desired = json.load(f)["spec"]["controllerConfig"]["overrideArgs"]

live = (((doc.get("spec") or {}).get("controllerConfig") or {})
        .get("overrideArgs") or [])
if live == desired:
    raise SystemExit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"controllerConfig": {"overrideArgs": desired}},
}))
PYEOF
      ;;
  esac
}

# One deadline shared by both wait phases (CRD establishment, the object):
# the Job's activeDeadlineSeconds is WAIT_TIMEOUT + 30, so sequential waits
# draw from one budget rather than each starting a full WAIT_TIMEOUT of its
# own.
DEADLINE=$(( $(date +%s) + WAIT_TIMEOUT ))

wait_for_crd "$CRD"
wait_for_object

write_rc=0
# The CertManager singleton is cluster-scoped, so conditional_write takes no
# namespace.
conditional_write "$RESOURCE" compute_patch \
  "CertManager overrideArgs" "$OBJECT" || write_rc=$?
case $write_rc in
  0)
    echo "Converged $OBJECT to the configured controller arguments"
    ;;
  3)
    echo "$OBJECT already carries the configured controller arguments; no changes needed"
    ;;
  2)
    echo "ERROR: $OBJECT disappeared mid-run; re-run this hook" >&2
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
