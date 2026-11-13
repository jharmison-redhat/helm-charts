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

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

OAUTH="oauths.config.openshift.io/cluster"

: "${IDP_NAME:?IDP_NAME must be set}"

require_oc "cannot remove the OAuth identity provider"
select_json_tool "cannot manipulate the identityProviders list"

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

write_rc=0
conditional_write "$OAUTH" compute_patch "identity provider" "OAuth object" || write_rc=$?
case $write_rc in
  0)
    echo "OAuth identity provider '$IDP_NAME' removed"
    ;;
  3)
    echo "OAuth identity provider '$IDP_NAME' is already absent; no changes needed"
    ;;
  2)
    echo "OAuth object ($OAUTH) is absent; no identity provider to remove"
    ;;
  *)
    exit 1
    ;;
esac
