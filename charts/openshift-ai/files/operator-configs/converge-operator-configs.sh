#!/bin/bash
# Converges the four operator-created platform configuration surfaces by
# value as a Helm post-install/post-upgrade hook: the Auth cluster singleton
# (services.platform.opendatahub.io/v1alpha1, name "auth", no namespace), the
# operator-created DSCInitialization/default-dsci cluster singleton, the
# odh-segment-key-config ConfigMap in redhat-ods-applications, and the
# workload-variant-autoscaler-saturation-scaling-config ConfigMap in
# redhat-ods-applications (value-gated: only when wvaOverrides is
# configured). No object is chart-created — the rhods-operator's
# DSCInitialization controller creates the Auth singleton, the operator
# creates the DSCInitialization singleton at startup (before any
# DataScienceCluster exists), and the dashboard deploys with the segment-key
# ConfigMap — so the chart never renders them as release manifests and never
# deletes them, recording no proof annotation: this hook converges the
# chart-managed fields by value on every install/upgrade and nothing else.
# The WVA ConfigMap is normally deployed by the WVA operator's bundle too;
# when it is absent (the WVA component is not deployed yet) the hook creates
# it under the chart-created proof annotation so the matching pre-delete hook
# walks it back on uninstall, while an operator-deployed instance (no proof)
# is only ever converged by value.
#
# Reconcile fight (documented in the chart README): the operator's
# reconciles and the dashboard's own cluster-settings writes can rewrite
# these fields at any time — between hook runs the operator's value wins.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried (bounded, in the preamble). Only the managed fields are compared
# and patched; other fields the operator or a user set are never touched.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"
source "${BASH_SOURCE[0]%/*}/wait-for.sh"
# The operand set-hook lib supplies the WVA path's create-under-proof tail
# (sourced, not executed — only the function definitions and defaults run).
source "${BASH_SOURCE[0]%/*}/set-chart-created-cr.sh"

AUTH_CRD="auths.services.platform.opendatahub.io"
AUTH_RESOURCE="${AUTH_CRD}/auth"
AUTH_OBJECT="Auth singleton auth"
export AUTH_PATCH="${AUTH_PATCH:-/config/auth-patch.json}"
# oc targets are "<plural>.<group>/<name>": the API GROUP is the singular
# dscinitialization and the plural RESOURCE is dscinitializations — the API
# rejects the bare "dscinitializations" group form ("the server doesn't have
# a resource type \"dscinitializations\"").
DSCI_RESOURCE="dscinitializations.dscinitialization.opendatahub.io/default-dsci"
DSCI_OBJECT="DSCInitialization singleton default-dsci"
export DSCI_PATCH="${DSCI_PATCH:-/config/dsci-patch.json}"
SEGMENT_RESOURCE="configmap/odh-segment-key-config"
SEGMENT_OBJECT="ConfigMap redhat-ods-applications/odh-segment-key-config"
SEGMENT_NAMESPACE="redhat-ods-applications"
export SEGMENT_PATCH="${SEGMENT_PATCH:-/config/segment-key-patch.json}"
WVA_RESOURCE="configmap/workload-variant-autoscaler-saturation-scaling-config"
WVA_OBJECT="ConfigMap redhat-ods-applications/workload-variant-autoscaler-saturation-scaling-config"
WVA_NAMESPACE="redhat-ods-applications"
export WVA_CREATE="${WVA_CREATE:-/config/wva-create.json}"
export WVA_PATCH="${WVA_PATCH:-/config/wva-patch.json}"
export PROOF_ANNOTATION="${PROOF_ANNOTATION:-openshift-ai/chart-created}"
export WAIT_TIMEOUT="${WAIT_TIMEOUT:-240}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"

require_oc "cannot converge the operator configuration surfaces"
select_json_tool "cannot converge the operator configuration surfaces"

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT

for payload in "$AUTH_PATCH" "$DSCI_PATCH" "$SEGMENT_PATCH"; do
  if [ ! -s "$payload" ]; then
    echo "ERROR: the payload file is missing or empty at $payload; the ConfigMap delivery is broken" >&2
    exit 1
  fi
done

# Waits until a resource type (CRD) is established, against the script-wide
# deadline computed below. The distinct names keep these clear of the sourced
# operand set-hook lib's same-named helpers (which read the CRD from
# CRD_NAME instead of an argument).
poll_crd_established() {
  local crd="$1" rc=0 established
  established=$(oc get "crd/$crd" \
    -o jsonpath='{.status.conditions[?(@.type=="Established")].status}' \
    --request-timeout="$REQUEST_TIMEOUT" 2>"$workdir/oc.err") && rc=0 || rc=$?
  if [ "$rc" -eq 0 ] && [ "$established" = "True" ]; then
    return 0
  fi
  if [ "$rc" -ne 0 ] && ! grep -q '(NotFound)' "$workdir/oc.err"; then
    cat "$workdir/oc.err" >&2
    exit 1
  fi
  return 3
}

wait_for_crd_established() {
  local crd="$1"
  if ! wait_for "$DEADLINE" "$POLL_INTERVAL" "" \
        poll_crd_established "$crd"; then
    echo "ERROR: timed out after ${WAIT_TIMEOUT}s waiting for the $crd custom resource definition to be established; the rhods-operator must install and reconcile the owning component first (check the operator state and the DataScienceCluster reconciliation, then re-run this hook)" >&2
    exit 1
  fi
}

# Waits until the operator-created object exists, against the script-wide
# deadline computed below.
# $1 is the oc get target, $2 the display label, and any remaining arguments
# are passed to oc get verbatim (the -n flag for namespaced targets). Only
# absence is retried; every other read failure exits immediately.
check_object_present() {
  local target="$1" object="$2"; shift 2
  if oc get "$target" "$@" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$workdir/object.json" 2>"$workdir/oc.err"; then
    return 0
  fi
  if ! grep -q '(NotFound)' "$workdir/oc.err"; then
    cat "$workdir/oc.err" >&2
    exit 1
  fi
  WAIT_DETAIL="$object"
  return 3
}

wait_for_object() {
  local target="$1" object="$2"; shift 2
  if ! wait_for "$DEADLINE" "$POLL_INTERVAL" '%s not created yet; retrying\n' \
        check_object_present "$target" "$object" "$@"; then
    echo "ERROR: timed out after ${WAIT_TIMEOUT}s waiting for $object to appear; the rhods-operator creates these objects during operator startup and DataScienceCluster reconciliation (check the operator state, then re-run this hook)" >&2
    exit 1
  fi
}

# Computes the Auth merge patch from the live object JSON in $1: the managed
# spec fields (adminGroups, allowedGroups) applied when either differs from
# the desired payload, nothing when both already match.
compute_auth_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile desired "$AUTH_PATCH" '
        ($desired[0].spec) as $des
        | if ((.spec.adminGroups // []) == ($des.adminGroups // []))
              and ((.spec.allowedGroups // []) == ($des.allowedGroups // [])) then empty
          else {metadata: {resourceVersion: .metadata.resourceVersion},
                spec: {adminGroups: $des.adminGroups,
                       allowedGroups: $des.allowedGroups}}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
with open(os.environ["AUTH_PATCH"]) as f:
    desired = json.load(f)["spec"]

live = doc.get("spec") or {}
if ((live.get("adminGroups") or []) == (desired.get("adminGroups") or [])
        and (live.get("allowedGroups") or []) == (desired.get("allowedGroups") or [])):
    raise SystemExit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"adminGroups": desired.get("adminGroups"),
             "allowedGroups": desired.get("allowedGroups")},
}))
PYEOF
      ;;
  esac
}

# Computes the segment-key merge patch from the live ConfigMap JSON in $1:
# the managed data key (segmentKeyEnabled) applied when it differs from the
# desired payload, nothing when it already matches.
compute_segment_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile desired "$SEGMENT_PATCH" '
        ($desired[0].data.segmentKeyEnabled) as $des
        | if ((.data.segmentKeyEnabled // "") == $des) then empty
          else {metadata: {resourceVersion: .metadata.resourceVersion},
                data: {segmentKeyEnabled: $des}}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
with open(os.environ["SEGMENT_PATCH"]) as f:
    desired = json.load(f)["data"]["segmentKeyEnabled"]

live = (doc.get("data") or {}).get("segmentKeyEnabled", "")
if live == desired:
    raise SystemExit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "data": {"segmentKeyEnabled": desired},
}))
PYEOF
      ;;
  esac
}

# Computes the DSCInitialization merge patch from the live object JSON in $1:
# the spec fields the chart's payload carries applied when any differs from
# the live spec, nothing when the live spec already matches on every desired
# field. Only the DESIRED subset of the live spec is compared — extra spec
# fields the operator's components or a user set are neither matched nor
# patched (the merge patch itself preserves unmanaged live fields; an
# explicit null in the desired payload removes a field).
compute_dsci_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile desired "$DSCI_PATCH" '
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
with open(os.environ["DSCI_PATCH"]) as f:
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

# Computes the WVA saturation-scaling merge patch from the live ConfigMap
# JSON in $1: the desired data keys applied when any differs from the live
# data, nothing when the live data already carries the desired values. Only
# the DESIRED subset of the live data is compared — data keys the WVA
# operator or a user set are neither matched nor patched (the merge patch
# itself preserves unmanaged live keys).
compute_wva_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile desired "$WVA_PATCH" '
        ($desired[0].data // {}) as $des
        | if ((.data // {}) | with_entries(.key as $k | select($des | has($k)))) == $des
          then empty
          else {metadata: {resourceVersion: .metadata.resourceVersion},
                data: $des}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
with open(os.environ["WVA_PATCH"]) as f:
    desired = json.load(f).get("data") or {}

live = doc.get("data") or {}
if {k: live[k] for k in desired if k in live} == desired:
    raise SystemExit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "data": desired,
}))
PYEOF
      ;;
  esac
}

converge_object() {
  local resource="$1" compute="$2" patch_subject="$3" object="$4" namespace="$5"
  local write_rc=0
  # conditional_write's fifth parameter is the plain namespace string (empty
  # for cluster-scoped resources) — it builds the -n flag itself.
  conditional_write "$resource" "$compute" "$patch_subject" "$object" \
    "$namespace" || write_rc=$?
  case $write_rc in
    0)
      echo "Converged $object to the configured values"
      ;;
    3)
      echo "$object already matches the configured values; no changes needed"
      ;;
    2)
      echo "ERROR: $object disappeared mid-run; re-run this hook" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
}

# Converges the WVA ConfigMap by value, treating absence as the caller's
# create signal rather than a mid-run failure: returns 0 once converged (at
# the desired values or after a successful patch) and 2 when the object is
# absent.
converge_wva() {
  local write_rc=0
  conditional_write "$WVA_RESOURCE" compute_wva_patch \
    "saturation-scaling data" "$WVA_OBJECT" "$WVA_NAMESPACE" || write_rc=$?
  case $write_rc in
    0)
      echo "Converged $WVA_OBJECT to the configured values"
      ;;
    3)
      echo "$WVA_OBJECT already matches the configured values; no changes needed"
      ;;
    2)
      return 2
      ;;
    *)
      exit 1
      ;;
  esac
}

# Auth and DSCInitialization are cluster-scoped and carry no namespace.
# One deadline shared by every wait phase below (CRD establishment, the Auth
# object, the DSCInitialization object, the segment-key ConfigMap): the Job's
# activeDeadlineSeconds is WAIT_TIMEOUT + 30, so sequential waits must draw
# from one budget rather than each starting a full WAIT_TIMEOUT of its own.
DEADLINE=$(( $(date +%s) + WAIT_TIMEOUT ))

wait_for_crd_established "$AUTH_CRD"
wait_for_object "$AUTH_RESOURCE" "$AUTH_OBJECT"
converge_object "$AUTH_RESOURCE" compute_auth_patch "Auth spec" \
  "$AUTH_OBJECT" ""

# The operator creates the DSCInitialization singleton at startup — before
# any DataScienceCluster exists — so the wait is for the operator itself.
wait_for_object "$DSCI_RESOURCE" "$DSCI_OBJECT"
converge_object "$DSCI_RESOURCE" compute_dsci_patch "DSCInitialization spec" \
  "$DSCI_OBJECT" ""

wait_for_object "$SEGMENT_RESOURCE" "$SEGMENT_OBJECT" -n "$SEGMENT_NAMESPACE"
converge_object "$SEGMENT_RESOURCE" compute_segment_patch "segmentKeyEnabled" \
  "$SEGMENT_OBJECT" "$SEGMENT_NAMESPACE"

# The WVA saturation-scaling section is value-gated: the payload keys render
# into this ConfigMap only when wvaOverrides is configured. Unlike the three
# surfaces above, the ConfigMap is not waited for — when it is absent (the
# WVA component has not deployed yet) it is created under the chart-created
# proof annotation so the matching pre-delete hook walks it back on
# uninstall; an operator-deployed instance carries no proof and is never
# deleted.
if [ -s "$WVA_PATCH" ]; then
  if [ ! -s "$WVA_CREATE" ]; then
    echo "ERROR: the payload file is missing or empty at $WVA_CREATE; the ConfigMap delivery is broken" >&2
    exit 1
  fi
  wva_rc=0
  converge_wva || wva_rc=$?
  if [ "$wva_rc" -eq 2 ]; then
    # Reuses the operand set-hook lib's create-under-proof tail. CRD_NAME is
    # unset, so the lib's CRD wait is skipped — a core ConfigMap needs no
    # CRD, and this surface is deliberately not waited for.
    RESOURCE="$WVA_RESOURCE"
    OBJECT_NAME="$WVA_OBJECT"
    OBJECT_NAMESPACE="$WVA_NAMESPACE"
    CREATE_PAYLOAD="$WVA_CREATE"
    create_under_proof converge_wva
  elif [ "$wva_rc" -ne 0 ]; then
    exit 1
  fi
fi
