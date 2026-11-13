#!/bin/bash
# Upserts this chart's OpenID identity provider entry into the platform-owned
# OAuth cluster object (config.openshift.io/v1, name "cluster"). The object
# itself is never owned or deleted by this release. spec.identityProviders is
# an atomic list, so the only correct upsert is a read-modify-write carrying
# the live metadata.resourceVersion: a stale version is rejected with HTTP 409
# and retried. Other identity providers are preserved untouched.
set -euo pipefail

OAUTH="oauths.config.openshift.io/cluster"
REQUEST_TIMEOUT="30s"
MAX_ATTEMPTS=5
RETRY_SLEEP=2

: "${IDP_NAME:?IDP_NAME must be set}"
ENTRY_FILE="${ENTRY_FILE:-/mnt/idp-entry.json}"

if ! command -v oc >/dev/null 2>&1; then
  echo "ERROR: the oc CLI is not available in this image; cannot configure the OAuth identity provider" >&2
  exit 1
fi

if command -v jq >/dev/null 2>&1; then
  JSON_TOOL=jq
elif command -v python3 >/dev/null 2>&1; then
  JSON_TOOL=python3
else
  echo "ERROR: neither jq nor python3 is available in this image; cannot manipulate the identityProviders list" >&2
  exit 1
fi

# Reads the live OAuth object JSON from $1 and prints the merge patch carrying
# the current resourceVersion and the upserted list. The named entry is looked
# up by name: an exactly equal entry prints nothing (no-op), a different entry
# is replaced in place preserving its position, and an absent entry is
# appended.
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg name "$IDP_NAME" --slurpfile entry "$ENTRY_FILE" '
        . as $doc
        | ($entry[0]) as $entry
        | ($doc.spec.identityProviders // []) as $have
        | ($have | to_entries | map(select(.value.name == $name)) | .[0].key) as $idx
        | if $idx != null and $have[$idx] == $entry then empty
          else
            (if $idx == null
             then $have + [$entry]
             else $have[:$idx] + [$entry]
                  + ($have[($idx + 1):] | map(select(.name != $name)))
             end) as $want
            | {metadata: {resourceVersion: $doc.metadata.resourceVersion},
               spec: {identityProviders: $want}}
          end' "$1"
      ;;
    python3)
      python3 - "$IDP_NAME" "$ENTRY_FILE" "$1" <<'PYEOF'
import json, sys

name, entry_file, oauth_file = sys.argv[1], sys.argv[2], sys.argv[3]
with open(entry_file) as f:
    entry = json.load(f)
with open(oauth_file) as f:
    doc = json.load(f)
have = doc.get("spec", {}).get("identityProviders") or []
idx = next((i for i, p in enumerate(have) if p.get("name") == name), None)
if idx is not None and have[idx] == entry:
    sys.exit(0)
if idx is None:
    want = have + [entry]
else:
    want = (have[:idx] + [entry]
            + [p for p in have[idx + 1:] if p.get("name") != name])
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"identityProviders": want},
}))
PYEOF
      ;;
  esac
}

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
oauth_json="$workdir/oauth.json"
oc_err="$workdir/oc.err"

attempt=1
while :; do
  if ! oc get "$OAUTH" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$oauth_json" 2>"$oc_err"; then
    if grep -q '(NotFound)' "$oc_err"; then
      echo "ERROR: prerequisite missing: the platform OAuth object ($OAUTH) does not exist on this cluster; install OpenShift's default authentication before applying this chart" >&2
      exit 1
    fi
    cat "$oc_err" >&2
    exit 1
  fi

  if ! merged=$(compute_patch "$oauth_json"); then
    echo "ERROR: failed to compute the identity provider merge patch from the live OAuth object" >&2
    exit 1
  fi
  if [ -z "$merged" ]; then
    echo "OAuth identity provider '$IDP_NAME' is already up to date; no changes needed"
    exit 0
  fi

  if oc patch "$OAUTH" --type=merge --patch="$merged" \
      --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$oc_err"; then
    echo "OAuth identity provider '$IDP_NAME' upserted"
    exit 0
  fi

  if ! grep -q '(Conflict)' "$oc_err"; then
    cat "$oc_err" >&2
    exit 1
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "ERROR: the OAuth object was modified concurrently on every attempt ($MAX_ATTEMPTS); giving up" >&2
    cat "$oc_err" >&2
    exit 1
  fi
  echo "OAuth object was modified concurrently (conflict); retrying (attempt $attempt of $MAX_ATTEMPTS)" >&2
  attempt=$((attempt + 1))
  sleep "$RETRY_SLEEP"
done
