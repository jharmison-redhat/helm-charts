#!/bin/bash
# Enforces the configured replica counts on the rhods Dashboard and operator
# Deployments as a Helm post-install/post-upgrade hook (and, in the CronJob
# durability mode, on a schedule from the same ConfigMap). The Deployments are
# created by the rhods-operator — this chart never renders them — so the hook
# waits a bounded time for each managed Deployment, then read-modify-writes
# spec.replicas to the configured count.
#
# The first write to a Deployment records its pre-existing replica count in
# the proof annotation (openshift-ai/replicas-prior): the recorded prior is
# never re-baselined while the annotation survives, so the remove hook always
# restores the FIRST recorded prior. A Deployment that carries the proof
# annotation is only corrected back to the configured count (drift from OLM
# recreates, CSV bumps, or manual scaling). The annotation travels on the
# Deployment, so an externally recreated Deployment (annotation gone) is
# re-baselined: its current count becomes the new recorded prior.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried (bounded, in the preamble).
#
# REPLICAS_WAIT_FOR_MISSING selects the startup-absence behavior: "true" (the
# set hook) waits bounded for a missing Deployment and fails on timeout — a
# fresh install must not silently skip enforcement; anything else (the
# CronJob) skips a missing Deployment with a diagnostic so a schedule run
# against a removed or not-yet-created component does not pile up failures.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

PROOF_ANNOTATION="openshift-ai/replicas-prior"
COMPONENTS_FILE="${REPLICAS_COMPONENTS_FILE:-/config/components.txt}"
WAIT_TIMEOUT="${REPLICAS_WAIT_TIMEOUT:-240}"
POLL_INTERVAL="${REPLICAS_POLL_INTERVAL:-5}"
WAIT_FOR_MISSING="${REPLICAS_WAIT_FOR_MISSING:-true}"

require_oc "cannot enforce the replica counts"
select_json_tool "cannot manipulate Deployment replicas"

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

# Computes the enforcement merge patch from the live Deployment JSON in $1.
# Emits the proof annotation (recording the current replica count, or the
# token "unset" when the field is absent) together with the configured count
# when no proof annotation exists; emits only the count when a proof
# annotation exists and the live count drifted; emits nothing when the count
# already matches under proof.
compute_replicas_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg ann "$PROOF_ANNOTATION" --argjson count "$REPLICAS_COUNT" '
        (try (.metadata.annotations[$ann]) catch null) as $proof
        | (try (.spec.replicas) catch null) as $current
        | if $proof == null or $proof == "" then
            (if $current == null then "unset" else ($current | tostring) end) as $prior
            | {metadata: {resourceVersion: .metadata.resourceVersion,
                          annotations: {($ann): $prior}},
               spec: {replicas: $count}}
          elif $current != $count then
            {metadata: {resourceVersion: .metadata.resourceVersion},
             spec: {replicas: $count}}
          else empty end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
ann = os.environ["PROOF_ANNOTATION"]
count = int(os.environ["REPLICAS_COUNT"])

proof = (doc.get("metadata", {}).get("annotations") or {}).get(ann)
current = doc.get("spec", {}).get("replicas")
if proof in (None, ""):
    prior = "unset" if current is None else str(current)
    print(json.dumps({
        "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"],
                     "annotations": {ann: prior}},
        "spec": {"replicas": count},
    }))
elif current != count:
    print(json.dumps({
        "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
        "spec": {"replicas": count},
    }))
PYEOF
      ;;
  esac
}

# Waits with one shared deadline until every managed Deployment exists. The
# Deployments are operator-created operands that appear only after the
# rhods-operator reconciles the DataScienceCluster.
wait_for_deployments() {
  local deadline=$(( $(date +%s) + WAIT_TIMEOUT ))
  while :; do
    local missing=() component namespace name
    for component in "${components[@]}"; do
      read -r namespace name _ <<< "$component"
      if ! oc get "deployment/$name" -n "$namespace" -o json \
            --request-timeout="$REQUEST_TIMEOUT" \
            >/dev/null 2>"$workdir/oc.err"; then
        if grep -q '(NotFound)' "$workdir/oc.err"; then
          missing+=("$name")
        else
          cat "$workdir/oc.err" >&2
          exit 1
        fi
      fi
    done
    if [ "${#missing[@]}" -eq 0 ]; then
      return 0
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "ERROR: timed out after ${WAIT_TIMEOUT}s waiting for Deployment(s) ${missing[*]}; the rhods-operator must deploy the operands before the replica counts can be enforced (check the operator state and DataScienceCluster reconciliation, then re-run this hook)" >&2
      exit 1
    fi
    echo "Deployment(s) ${missing[*]} not created yet; retrying" >&2
    sleep "$POLL_INTERVAL"
  done
}

# The set hook (bounded-wait mode) waits for every managed Deployment up
# front; the CronJob's schedule runs skip missing Deployments per component
# below instead.
if [ "$WAIT_FOR_MISSING" = "true" ]; then
  wait_for_deployments
fi

for component in "${components[@]}"; do
  read -r namespace name count <<< "$component"
  if ! oc get "deployment/$name" -n "$namespace" -o json \
        --request-timeout="$REQUEST_TIMEOUT" \
        >"$workdir/deployment.json" 2>"$workdir/oc.err"; then
    if grep -q '(NotFound)' "$workdir/oc.err" \
        && [ "$WAIT_FOR_MISSING" != "true" ]; then
      echo "Deployment $namespace/$name does not exist (yet); skipped this run"
      continue
    fi
    cat "$workdir/oc.err" >&2
    exit 1
  fi
  export REPLICAS_COUNT="$count" PROOF_ANNOTATION
  write_rc=0
  conditional_write "deployment/$name" compute_replicas_patch \
    "replicas patch" "deployment $namespace/$name" "$namespace" || write_rc=$?
  case $write_rc in
    0)
      echo "Enforced replicas=$count on deployment $namespace/$name (prior recorded in the ${PROOF_ANNOTATION} annotation)"
      ;;
    3)
      echo "Deployment $namespace/$name already runs replicas=$count under proof; no changes needed"
      ;;
    2)
      echo "ERROR: deployment $namespace/$name disappeared mid-run; re-run this hook" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
done
