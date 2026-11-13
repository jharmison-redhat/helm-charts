#!/bin/bash
# Restores spec.authentication.logoutRedirect to unset on the platform-owned
# Console cluster object (config.openshift.io/v1, name "cluster") as a Helm
# pre-delete hook, so the console is never left redirecting logouts at a
# Keycloak that no longer exists. The write is a merge patch carrying the
# live metadata.resourceVersion: a stale version is rejected with HTTP 409
# and retried. Only the value this release set is removed — a foreign value
# is left untouched. The Console object itself is never owned or deleted by
# this release; if it is already absent, there is nothing to remove and the
# hook succeeds.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

CONSOLE="consoles.config.openshift.io/cluster"

REDIRECT_URL_FILE="${REDIRECT_URL_FILE:-/mnt/logout-redirect-url}"

require_oc "cannot remove the console logout redirect"
select_json_tool "cannot manipulate the Console object"

if [ ! -s "$REDIRECT_URL_FILE" ]; then
  echo "ERROR: the logout redirect URL file ($REDIRECT_URL_FILE) is missing or empty" >&2
  exit 1
fi
REDIRECT_URL=$(cat "$REDIRECT_URL_FILE")

# Prints spec.authentication.logoutRedirect from the live Console object JSON
# in $1; an absent, null, or empty field prints an empty string (unset).
read_current() {
  case "$JSON_TOOL" in
    jq)
      jq -r '.spec.authentication.logoutRedirect // ""' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
current = (((doc.get("spec") or {}).get("authentication") or {}).get("logoutRedirect")) or ""
print(current)
PYEOF
      ;;
  esac
}

# Prints the merge patch carrying the current resourceVersion and the field
# reset to null. Only reached when the field is verified equal to the URL
# this release set.
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c '{metadata: {resourceVersion: .metadata.resourceVersion},
              spec: {authentication: {logoutRedirect: null}}}' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"authentication": {"logoutRedirect": None}},
}))
PYEOF
      ;;
  esac
}

# Produces the reset patch for conditional_write per its compute-function
# contract: nothing to change when the field does not hold exactly the URL
# this release set (unset or foreign), and a fatal condition (reported here)
# when the field cannot be read.
prepare_patch() {
  local current
  if ! current=$(read_current "$1"); then
    echo "ERROR: failed to read spec.authentication.logoutRedirect from the live Console object" >&2
    return 4
  fi
  if [ "$current" != "$REDIRECT_URL" ]; then
    return 0
  fi
  compute_patch "$1"
}

write_rc=0
conditional_write "$CONSOLE" prepare_patch "logoutRedirect" "Console object" || write_rc=$?
case $write_rc in
  0)
    echo "Console logoutRedirect removed"
    ;;
  3)
    echo "Console logoutRedirect is unset or foreign; no changes needed"
    ;;
  2)
    echo "Console object ($CONSOLE) is absent; no logoutRedirect to remove"
    ;;
  *)
    exit 1
    ;;
esac
