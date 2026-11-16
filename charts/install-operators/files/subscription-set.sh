#!/bin/bash
# Upserts this chart's managed Subscription fields as a Helm
# post-install/post-upgrade hook. The Subscription itself is never a release
# manifest (unless ownSubscription: true opts back into release ownership):
# on every install and upgrade this hook converges the chart-managed fields —
# spec.channel, spec.installPlanApproval, spec.name, spec.source,
# spec.sourceNamespace, spec.startingCSV, and spec.config.env — to the
# configured values, BY VALUE. Fields outside that set (any other spec.config
# entry, labels, annotations) are never touched.
#
# When the Subscription is absent the hook creates it with the chart's
# labels and a proof annotation recording that this chart created it; the
# pre-delete remove hook deletes the Subscription ONLY under that proof. A
# Subscription that pre-existed (created outside this chart) is converged but
# never deleted.
#
# The upsert is a read-modify-write carrying the live metadata.resourceVersion
# — a stale version is rejected with HTTP 409 and retried.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

DESIRED_FILE="${DESIRED_FILE:-/mnt/subscription.json}"
PROOF_ANNOTATION="install-operators/proof"

require_oc "cannot upsert the operator Subscription"
select_json_tool "cannot manipulate the Subscription spec"

export PROOF_ANNOTATION DESIRED_FILE
if [ ! -f "$DESIRED_FILE" ]; then
  echo "ERROR: the desired Subscription configuration is missing at ${DESIRED_FILE}; the hook ConfigMap delivery is broken" >&2
  exit 1
fi

# Prints the target Subscription's name and namespace from the desired
# configuration.
desired_field() {
  case "$JSON_TOOL" in
    jq)
      jq -r --arg field "$1" '.[$field]' "$DESIRED_FILE"
      ;;
    python3)
      python3 -c 'import json, os, sys

print(json.load(open(os.environ["DESIRED_FILE"]))[sys.argv[1]])' "$1"
      ;;
  esac
}

# Prints the set merge patch from the live Subscription JSON in $1: every
# chart-managed field that differs from the desired value (absent optional
# fields converge to null, removing only that key). Emits nothing when the
# live Subscription already matches.
compute_set_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile cfg "$DESIRED_FILE" '
        . as $doc
        | ($cfg[0].spec) as $want
        | ($doc.spec // {}) as $have
        | ($want.config.env // null) as $wantEnv
        | ($have.config.env // null) as $haveEnv
        | [($want | del(.config) | to_entries[]
            | select(.value != ($have[.key] // null))
            | {(.key): .value})] as $scalarEntries
        | (if $wantEnv != $haveEnv then [{"env": $wantEnv}] else [] end) as $envEntry
        | if ($scalarEntries | length) == 0 and ($envEntry | length) == 0
          then empty
          else
            {metadata: {resourceVersion: $doc.metadata.resourceVersion},
             spec: (((($scalarEntries | add) // {})
                     + (if ($envEntry | length) > 0
                        then {config: ($envEntry[0])} else {} end)))}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(os.environ["DESIRED_FILE"]) as f:
    desired = json.load(f)
with open(sys.argv[1]) as f:
    doc = json.load(f)
have = doc.get("spec") or {}
want = desired["spec"]

spec_patch = {}
for key, value in want.items():
    if key == "config":
        continue
    if value != have.get(key):
        spec_patch[key] = value
want_env = (want.get("config") or {}).get("env")
have_env = (have.get("config") or {}).get("env")
if want_env != have_env:
    spec_patch["config"] = {"env": want_env}
if not spec_patch:
    sys.exit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": spec_patch,
}))
PYEOF
      ;;
  esac
}

# Prints the create manifest (with the creation-proof annotation and the
# chart's labels baked in) from the desired configuration.
print_manifest() {
  case "$JSON_TOOL" in
    jq)
      jq -c '.manifest' "$DESIRED_FILE"
      ;;
    python3)
      python3 -c 'import json, os

print(json.dumps(json.load(open(os.environ["DESIRED_FILE"]))["manifest"]))'
      ;;
  esac
}

# Merges into a Subscription that appeared (or was won in a create race) after
# the first read reported it absent. Exits 1 when it is absent again.
merge_existing() {
  local name="$1" namespace="$2" merge_rc=0
  conditional_write "subscriptions.operators.coreos.com/$name" compute_set_patch \
    "Subscription spec" "Subscription $name" "$namespace" || merge_rc=$?
  case $merge_rc in
    0)
      echo "Subscription $name upserted with this chart's managed fields"
      ;;
    3)
      echo "Subscription $name already carries this chart's managed fields; no changes needed"
      ;;
    2)
      echo "ERROR: Subscription $name was reported absent twice; refusing to guess at cluster state" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
}

name=$(desired_field "name")
namespace=$(desired_field "namespace")

write_rc=0
conditional_write "subscriptions.operators.coreos.com/$name" compute_set_patch \
  "Subscription spec" "Subscription $name" "$namespace" || write_rc=$?
case $write_rc in
  0)
    echo "Subscription $name upserted with this chart's managed fields"
    ;;
  3)
    echo "Subscription $name already carries this chart's managed fields; no changes needed"
    ;;
  2)
    create_rc=0
    print_manifest | create_object "the Subscription $name" -n "$namespace" -f - || create_rc=$?
    case $create_rc in
      0)
        echo "Created Subscription $name with this chart's labels and the ${PROOF_ANNOTATION} creation-proof annotation"
        ;;
      3)
        merge_existing "$name" "$namespace"
        ;;
      *)
        exit 1
        ;;
    esac
    ;;
  *)
    exit 1
    ;;
esac
