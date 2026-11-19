#!/bin/bash

set -ex

rhcl_operators=(
  authorino
  dns
  limitador
  rhcl
)

operator_namespace={{ .Values.operatorNamespace }}

# Bounded waits; the Job sources these from the chart's restart values and
# the environment overrides exist for the smoke tests to keep them fast.
csv_ready_timeout=${CSV_READY_TIMEOUT_SECONDS:-120}
poll_interval=${CSV_READY_POLL_INTERVAL_SECONDS:-5}
# Bare seconds, like its siblings; the rollout command appends the s unit
# its --timeout flag expects.
rollout_timeout=${ROLLOUT_TIMEOUT_SECONDS:-300}

function operator_ready {
  local installed_csv
  installed_csv="$(oc get -n $operator_namespace subscription \
    -l "operators.coreos.com/${1}-operator.${operator_namespace}" \
    --request-timeout=10s \
    -ojsonpath='{.items[0].status.installedCSV}' 2>/dev/null)" || return 1
  if [ -z "$installed_csv" ]; then
    return 1
  fi
  # installedCSV only proves OLM resolved the Subscription; phase=Succeeded
  # on the CSV itself proves the operator is actually deployed and healthy.
  [ "$(oc get -n $operator_namespace csv "$installed_csv" \
    --request-timeout=10s \
    -ojsonpath='{.status.phase}' 2>/dev/null)" = "Succeeded" ]
}

for operator in "${rhcl_operators[@]}"; do
  echo "Waiting up to ${csv_ready_timeout}s for the ${operator} operator CSV to report Succeeded..."
  waited=0
  until operator_ready "$operator"; do
    if [ "$waited" -ge "$csv_ready_timeout" ]; then
      echo "Timed out after ${csv_ready_timeout}s waiting for the ${operator} operator CSV to report Succeeded — check the Subscription and ClusterServiceVersion in ${operator_namespace}" >&2
      exit 1
    fi
    sleep "$poll_interval"
    waited=$((waited + poll_interval))
  done
done
echo

oc delete pod -n $operator_namespace -l app=kuadrant,control-plane=controller-manager
sleep 1
oc rollout status -n $operator_namespace deployment/kuadrant-operator-controller-manager --timeout=${rollout_timeout}s
