# Shared scaffold for this chart's logout-redirect hook scripts: the
# Console resource reference, the redirect-URL input reader, and the
# dual-tool reader and patch computer both callers drive through the hook
# preamble's conditional_write. Each script sources this file alongside
# hook-preamble.sh (both are delivered into the same ConfigMap) and keeps
# its value-specific decision logic — when the field may be written and
# with what — local to itself.

CONSOLE="consoles.config.openshift.io/cluster"

REDIRECT_URL_FILE="${REDIRECT_URL_FILE:-/mnt/logout-redirect-url}"

# Prints the redirect URL from the input file, exiting when it is missing
# or empty.
read_redirect_url() {
  if [ ! -s "$REDIRECT_URL_FILE" ]; then
    echo "ERROR: the logout redirect URL file ($REDIRECT_URL_FILE) is missing or empty" >&2
    exit 1
  fi
  cat "$REDIRECT_URL_FILE"
}

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

# Prints the merge patch carrying the current resourceVersion and
# spec.authentication.logoutRedirect set to the URL in $2, or to null when
# $2 is empty — the same unset reading read_current reports.
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg redirect "$2" '
        {metadata: {resourceVersion: .metadata.resourceVersion},
         spec: {authentication: {logoutRedirect: (if $redirect == "" then null else $redirect end)}}}' "$1"
      ;;
    python3)
      python3 - "$2" "$1" <<'PYEOF'
import json, sys

redirect, console_file = sys.argv[1], sys.argv[2]
with open(console_file) as f:
    doc = json.load(f)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"authentication": {"logoutRedirect": redirect or None}},
}))
PYEOF
      ;;
  esac
}
