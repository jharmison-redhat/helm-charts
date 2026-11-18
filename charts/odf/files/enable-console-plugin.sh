#!/bin/bash
# Ensures the ODF console plugin is enabled on the platform-owned Console
# operator object (operator.openshift.io/v1, name "cluster") as a Helm
# post-install/post-upgrade hook. The Console object itself is never owned
# or deleted by this release. The write is a JSON patch guarded by a test
# operation pinning the metadata.resourceVersion read from the live object,
# so a concurrent writer's change fails the test operation (HTTP 422)
# instead of being silently lost — the hook then re-reads the object and
# retries, bounded by MAX_ATTEMPTS total attempts with RETRY_SLEEP between
# them. The plugin is appended to an existing plugins array (never
# rewritten wholesale), and only added when this chart's plugin is absent.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

CONSOLE="consoles.operator.openshift.io/cluster"
PLUGIN_NAME="${PLUGIN_NAME:-odf-console}"

echo "Ensuring ${PLUGIN_NAME} plugin is enabled"
echo ""

require_oc "cannot enable the console plugin"
select_json_tool "cannot manipulate the Console plugins"

state_file=$(mktemp) || exit 1
err_file=$(mktemp) || { rm -f "$state_file"; exit 1; }
trap 'rm -f "$state_file" "$err_file"' EXIT

# Prints the plugins array of the Console object JSON in $1 as a
# newline-separated list, or nothing when the array is absent or empty.
plugins() {
  case "$JSON_TOOL" in
    jq)
      jq -r '.spec.plugins // [] | .[]' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
for name in ((doc.get("spec") or {}).get("plugins") or []):
    print(name)
PYEOF
      ;;
  esac
}

# Prints the metadata.resourceVersion of the Console object JSON in $1.
resource_version() {
  case "$JSON_TOOL" in
    jq)
      jq -r '.metadata.resourceVersion' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
print(doc["metadata"]["resourceVersion"])
PYEOF
      ;;
  esac
}

# Prints the JSON patch that enables the plugin on the Console object JSON
# in $1, per $2's mode: "create" adds the whole plugins array (absent on
# the object — a single operation, so no empty-array intermediate is
# published for a concurrent writer to clobber), "append" adds one entry
# to the existing array. Both carry a test operation pinning the read
# resourceVersion, rejecting the whole patch when the object changed.
plugin_patch() {
  local console_file="$1" mode="$2" rv
  rv=$(resource_version "$console_file")
  case "$JSON_TOOL" in
    jq)
      if [ "$mode" = "create" ]; then
        jq -nc --arg rv "$rv" --arg name "$PLUGIN_NAME" \
          '[{op: "test", path: "/metadata/resourceVersion", value: $rv},
            {op: "add", path: "/spec/plugins", value: [$name]}]'
      else
        jq -nc --arg rv "$rv" --arg name "$PLUGIN_NAME" \
          '[{op: "test", path: "/metadata/resourceVersion", value: $rv},
            {op: "add", path: "/spec/plugins/-", value: $name}]'
      fi
      ;;
    python3)
      python3 - "$rv" "$PLUGIN_NAME" "$mode" <<'PYEOF'
import json, sys

rv, name, mode = sys.argv[1], sys.argv[2], sys.argv[3]
add = {"op": "add",
       "path": "/spec/plugins" if mode == "create" else "/spec/plugins/-",
       "value": [name] if mode == "create" else name}
print(json.dumps([
    {"op": "test", "path": "/metadata/resourceVersion", "value": rv},
    add,
]))
PYEOF
      ;;
  esac
}

attempt=1
while :; do
  get_rc=0
  oc get "$CONSOLE" -o json --request-timeout="$REQUEST_TIMEOUT" \
    >"$state_file" 2>"$err_file" || get_rc=$?
  if [ "$get_rc" -ne 0 ]; then
    if grep -q '(NotFound)' "$err_file"; then
      echo "ERROR: prerequisite missing: the Console operator object ($CONSOLE) does not exist on this cluster; install OpenShift's console operator before applying this chart" >&2
      exit 1
    fi
    cat "$err_file" >&2
    exit 1
  fi

  installed_plugins=$(plugins "$state_file")
  echo "Current plugins:"
  echo "${installed_plugins}"
  if printf '%s\n' "$installed_plugins" | grep -qxF "$PLUGIN_NAME"; then
    echo "${PLUGIN_NAME} is already enabled"
    break
  fi

  if [ -n "$installed_plugins" ]; then
    mode="append"
  else
    mode="create"
  fi
  patch=$(plugin_patch "$state_file" "$mode")

  patch_rc=0
  oc patch "$CONSOLE" --type=json --patch="$patch" \
    --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$err_file" || patch_rc=$?
  if [ "$patch_rc" -eq 0 ]; then
    echo "Enabling plugin: ${PLUGIN_NAME}"
    break
  fi
  # A JSON-patch test operation failure is an HTTP 422 (Invalid), not the
  # 409 a merge patch's stale resourceVersion produces, so the preamble's
  # Conflict retry contract does not apply here — this loop handles the
  # rejection itself.
  if ! grep -q '(Invalid)' "$err_file"; then
    cat "$err_file" >&2
    exit 1
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "ERROR: the Console object was modified concurrently on every attempt ($MAX_ATTEMPTS); giving up" >&2
    cat "$err_file" >&2
    exit 1
  fi
  echo "$CONSOLE was modified concurrently (test operation rejected); retrying (attempt $attempt of $MAX_ATTEMPTS)" >&2
  attempt=$((attempt + 1))
  sleep "$RETRY_SLEEP"
done

oc get "$CONSOLE" -o jsonpath='{.spec.plugins}'
echo
