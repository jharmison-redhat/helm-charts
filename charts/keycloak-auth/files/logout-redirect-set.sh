#!/bin/bash
# Sets spec.authentication.logoutRedirect on the platform-owned Console
# cluster object (config.openshift.io/v1, name "cluster") to the derived
# Keycloak RP-initiated logout URL, so logging out of the web console ends
# the Keycloak SSO session. The object itself is never owned or deleted by
# this release. The write is a merge patch carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried. The field is only written when unset — if it already holds a
# foreign value (a redirect this release did not set), the script stops
# loudly instead of overwriting it.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

CONSOLE="consoles.config.openshift.io/cluster"

REDIRECT_URL_FILE="${REDIRECT_URL_FILE:-/mnt/logout-redirect-url}"

require_oc "cannot set the console logout redirect"
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
# set to the derived URL. Only reached when the field is verified unset.
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg url "$REDIRECT_URL" '
        {metadata: {resourceVersion: .metadata.resourceVersion},
         spec: {authentication: {logoutRedirect: $url}}}' "$1"
      ;;
    python3)
      python3 - "$REDIRECT_URL" "$1" <<'PYEOF'
import json, sys

url, console_file = sys.argv[1], sys.argv[2]
with open(console_file) as f:
    doc = json.load(f)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"authentication": {"logoutRedirect": url}},
}))
PYEOF
      ;;
  esac
}

# Produces the patch for conditional_write per its compute-function contract:
# nothing to change when the field already holds the derived URL, a fatal
# condition (reported here) when it holds a foreign value, and the patch once
# the field is verified unset.
prepare_patch() {
  local current
  if ! current=$(read_current "$1"); then
    echo "ERROR: failed to read spec.authentication.logoutRedirect from the live Console object" >&2
    return 4
  fi
  if [ "$current" = "$REDIRECT_URL" ]; then
    return 0
  fi
  if [ -n "$current" ]; then
    echo "ERROR: the Console logoutRedirect is set to a foreign value '$current' that this release did not set; refusing to overwrite it — remove the value manually (or point logoutRedirect at this chart's derived URL) and re-run" >&2
    return 4
  fi
  compute_patch "$1"
}

write_rc=0
conditional_write "$CONSOLE" prepare_patch "logoutRedirect" "Console object" || write_rc=$?
case $write_rc in
  0)
    echo "Console logoutRedirect set"
    ;;
  3)
    echo "Console logoutRedirect is already up to date; no changes needed"
    ;;
  2)
    echo "ERROR: prerequisite missing: the platform Console object ($CONSOLE) does not exist on this cluster; install OpenShift's web console before applying this chart" >&2
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
