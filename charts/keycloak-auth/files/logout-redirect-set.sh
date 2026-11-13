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

CONSOLE="consoles.config.openshift.io/cluster"
REQUEST_TIMEOUT="30s"
MAX_ATTEMPTS=5
RETRY_SLEEP=2

REDIRECT_URL_FILE="${REDIRECT_URL_FILE:-/mnt/logout-redirect-url}"

if ! command -v oc >/dev/null 2>&1; then
  echo "ERROR: the oc CLI is not available in this image; cannot set the console logout redirect" >&2
  exit 1
fi

if command -v jq >/dev/null 2>&1; then
  JSON_TOOL=jq
elif command -v python3 >/dev/null 2>&1; then
  JSON_TOOL=python3
else
  echo "ERROR: neither jq nor python3 is available in this image; cannot manipulate the Console object" >&2
  exit 1
fi

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

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
console_json="$workdir/console.json"
oc_err="$workdir/oc.err"

attempt=1
while :; do
  if ! oc get "$CONSOLE" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$console_json" 2>"$oc_err"; then
    if grep -q '(NotFound)' "$oc_err"; then
      echo "ERROR: prerequisite missing: the platform Console object ($CONSOLE) does not exist on this cluster; install OpenShift's web console before applying this chart" >&2
      exit 1
    fi
    cat "$oc_err" >&2
    exit 1
  fi

  if ! current=$(read_current "$console_json"); then
    echo "ERROR: failed to read spec.authentication.logoutRedirect from the live Console object" >&2
    exit 1
  fi
  if [ "$current" = "$REDIRECT_URL" ]; then
    echo "Console logoutRedirect is already up to date; no changes needed"
    exit 0
  fi
  if [ -n "$current" ]; then
    echo "ERROR: the Console logoutRedirect is set to a foreign value '$current' that this release did not set; refusing to overwrite it — remove the value manually (or point logoutRedirect at this chart's derived URL) and re-run" >&2
    exit 1
  fi

  if ! merged=$(compute_patch "$console_json"); then
    echo "ERROR: failed to compute the logoutRedirect merge patch from the live Console object" >&2
    exit 1
  fi

  if oc patch "$CONSOLE" --type=merge --patch="$merged" \
      --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$oc_err"; then
    echo "Console logoutRedirect set"
    exit 0
  fi

  if ! grep -q '(Conflict)' "$oc_err"; then
    cat "$oc_err" >&2
    exit 1
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "ERROR: the Console object was modified concurrently on every attempt ($MAX_ATTEMPTS); giving up" >&2
    cat "$oc_err" >&2
    exit 1
  fi
  echo "Console object was modified concurrently (conflict); retrying (attempt $attempt of $MAX_ATTEMPTS)" >&2
  attempt=$((attempt + 1))
  sleep "$RETRY_SLEEP"
done
