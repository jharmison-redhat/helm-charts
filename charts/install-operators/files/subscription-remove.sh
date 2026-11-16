#!/bin/bash
# Deletes this chart's hook-created Subscription as a Helm pre-delete hook.
# Release ownership and the remove hook enforce the same boundary: the
# Subscription is removed
# ONLY when the install-operators/proof annotation records that the set hook
# created it. A Subscription that pre-existed (created outside this chart) is
# left in place, converged fields and all — this chart never deletes what it
# did not create. An absent Subscription needs no removal and succeeds.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

DESIRED_FILE="${DESIRED_FILE:-/mnt/subscription.json}"
PROOF_ANNOTATION="install-operators/proof"

require_oc "cannot remove the operator Subscription"
select_json_tool "cannot read the Subscription's proof annotation"

export PROOF_ANNOTATION DESIRED_FILE
if [ ! -f "$DESIRED_FILE" ]; then
  echo "ERROR: the Subscription configuration is missing at ${DESIRED_FILE}; the hook ConfigMap delivery is broken" >&2
  exit 1
fi

case "$JSON_TOOL" in
  jq)
    desired_field() { jq -r --arg field "$1" '.[$field]' "$DESIRED_FILE"; }
    proof_annotation() { jq -r --arg ann "$PROOF_ANNOTATION" \
      '.metadata.annotations[$ann] // ""' "$1"; }
    ;;
  python3)
    desired_field() {
      python3 -c 'import json, os, sys

print(json.load(open(os.environ["DESIRED_FILE"]))[sys.argv[1]])' "$1"
    }
    proof_annotation() {
      python3 -c 'import json, sys

doc = json.load(open(sys.argv[1]))
print(doc.get("metadata", {}).get("annotations", {}).get(sys.argv[2], ""))' \
        "$1" "$PROOF_ANNOTATION"
    }
    ;;
esac

name=$(desired_field "name")
namespace=$(desired_field "namespace")
resource="subscriptions.operators.coreos.com/$name"

doc_file=$(mktemp)
err_file=$(mktemp)
trap 'rm -f "$doc_file" "$err_file"' EXIT

get_rc=0
oc get -n "$namespace" "$resource" -o json --request-timeout="$REQUEST_TIMEOUT" \
  >"$doc_file" 2>"$err_file" || get_rc=$?
if [ "$get_rc" -ne 0 ]; then
  if grep -q '(NotFound)' "$err_file"; then
    echo "Subscription $name is already absent; nothing to remove"
    exit 0
  fi
  cat "$err_file" >&2
  exit 1
fi

if [ "$(proof_annotation "$doc_file")" = "true" ]; then
  delete_rc=0
  delete_object "the Subscription $name" -n "$namespace" "$resource" || delete_rc=$?
  case $delete_rc in
    0)
      echo "Deleted the hook-created Subscription $name"
      ;;
    3)
      echo "Subscription $name is already absent; nothing to remove"
      ;;
    *)
      exit 1
      ;;
  esac
else
  echo "Subscription $name carries no ${PROOF_ANNOTATION} creation-proof annotation — it was not created by this chart; leaving it in place"
fi
