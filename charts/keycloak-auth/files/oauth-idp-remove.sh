#!/bin/bash
# Removes this chart's OpenID identity provider entry from the platform-owned
# OAuth cluster object (config.openshift.io/v1, name "cluster") as a Helm
# pre-delete hook, so the entry never outlives the openid-client-secret
# Secret that it references. spec.identityProviders is an atomic list, so the
# only correct removal is a read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried. Other identity providers are preserved untouched. The OAuth object
# itself is never owned or deleted by this release; if it is already absent,
# there is nothing to remove and the hook succeeds.
set -euo pipefail

OAUTH="oauths.config.openshift.io/cluster"
REQUEST_TIMEOUT="30s"
MAX_ATTEMPTS=5
RETRY_SLEEP=2

: "${IDP_NAME:?IDP_NAME must be set}"

if ! command -v oc >/dev/null 2>&1; then
  echo "ERROR: the oc CLI is not available in this image; cannot remove the OAuth identity provider" >&2
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

# Reads the live OAuth object JSON from $1 and prints the merge patch
# carrying the current resourceVersion and the list without the named entry;
# prints nothing when the named entry is already absent.
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg name "$IDP_NAME" '
        . as $doc
        | ($doc.spec.identityProviders // []) as $have
        | ([$have[] | select(.name != $name)]) as $want
        | if ($want | length) == ($have | length) then empty
          else {metadata: {resourceVersion: $doc.metadata.resourceVersion},
                spec: {identityProviders: $want}}
          end' "$1"
      ;;
    python3)
      python3 - "$IDP_NAME" "$1" <<'PYEOF'
import json, sys

name, oauth_file = sys.argv[1], sys.argv[2]
with open(oauth_file) as f:
    doc = json.load(f)
have = doc.get("spec", {}).get("identityProviders") or []
want = [p for p in have if p.get("name") != name]
if len(want) == len(have):
    sys.exit(0)
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
      echo "OAuth object ($OAUTH) is absent; no identity provider to remove"
      exit 0
    fi
    cat "$oc_err" >&2
    exit 1
  fi

  if ! merged=$(compute_patch "$oauth_json"); then
    echo "ERROR: failed to compute the identity provider merge patch from the live OAuth object" >&2
    exit 1
  fi
  if [ -z "$merged" ]; then
    echo "OAuth identity provider '$IDP_NAME' is already absent; no changes needed"
    exit 0
  fi

  if oc patch "$OAUTH" --type=merge --patch="$merged" \
      --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$oc_err"; then
    echo "OAuth identity provider '$IDP_NAME' removed"
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
