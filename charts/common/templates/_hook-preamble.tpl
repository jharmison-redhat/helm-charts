{{/* Shared preamble for this repo's hook scripts, delivered verbatim into
consuming charts' script ConfigMaps as a payload, not callable logic.
Consumers source it sibling-relative. The payload's own opening comment is
part of the delivered bytes, so its wording only changes as part of a
deliberate rendered-output change. The conditional_write contract is
namespace-optional: it takes the namespace as an optional argument and omits
-n when empty, so cluster-scoped callers pass no namespace and no variant is
needed. Any edit to this payload is a rendered-output change across ALL
consumers at once and re-owes live validation of each one's hook surfaces.
The payload must never contain {{ (Go-template action) */}}
{{- define "common.hook-preamble" -}}
# Shared preamble for this chart's hook scripts: fail-closed tool selection
# and the bounded-conflict-retry conditional-write mechanics. Each hook
# script sources this file from its own directory (the lib is delivered into
# the same ConfigMap as the script) and keeps its object- and field-specific
# logic — resources, JSON payloads, and diagnostics beyond what the helpers
# parameterize — local to itself.

REQUEST_TIMEOUT="30s"
MAX_ATTEMPTS=5
RETRY_SLEEP=2

# Exits when the oc CLI is unavailable. $1 completes the error message with
# the action the hook was attempting.
require_oc() {
  if ! command -v oc >/dev/null 2>&1; then
    echo "ERROR: the oc CLI is not available in this image; $1" >&2
    exit 1
  fi
}

# Sets JSON_TOOL to jq when available, else python3, and exits fail-closed
# when neither exists. $1 completes the error message with what the hook
# needed JSON tooling for.
select_json_tool() {
  if command -v jq >/dev/null 2>&1; then
    JSON_TOOL=jq
  elif command -v python3 >/dev/null 2>&1; then
    JSON_TOOL=python3
  else
    echo "ERROR: neither jq nor python3 is available in this image; $1" >&2
    exit 1
  fi
}

# Performs the hook scripts' conditional read-modify-write: fetch the live
# object, produce a merge patch carrying its metadata.resourceVersion, and
# apply it. A concurrent modification (HTTP 409) re-reads the object and
# retries, bounded by MAX_ATTEMPTS total attempts with RETRY_SLEEP between
# them.
#
# $1 resource, $2 name of a compute function, $3 patch subject for the
# compute-failure diagnostic, $4 object label for the concurrency
# diagnostics, $5 namespace (optional — needed for namespaced resources).
# The compute function receives the file holding the fetched object JSON and
# communicates by contract:
#   - prints the merge patch (exit 0) to have it applied
#   - prints nothing (exit 0) when nothing needs changing
#   - prints nothing and exits 4 after reporting its own fatal condition
#   - any other failure gets the generic compute-failure diagnostic
# Returns 0 once the patch is applied, 3 when nothing needed changing, and 2
# when the object is absent; every other failure exits 1. The body runs in a
# subshell so its scratch files are cleaned by its own EXIT trap without
# disturbing any trap the caller installed.
conditional_write() (
  local resource="$1" compute="$2" patch_subject="$3" object="$4" namespace="${5:-}"
  local json_file err_file attempt=1 patch get_rc=0 compute_rc=0
  local ns_args=()

  json_file=$(mktemp) || exit 1
  err_file=$(mktemp) || { rm -f "$json_file"; exit 1; }
  trap 'rm -f "$json_file" "$err_file"' EXIT
  if [ -n "$namespace" ]; then
    ns_args=(-n "$namespace")
  fi

  while :; do
    get_rc=0
    oc get "${ns_args[@]}" "$resource" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$json_file" 2>"$err_file" || get_rc=$?
    if [ "$get_rc" -ne 0 ]; then
      if grep -q '(NotFound)' "$err_file"; then
        return 2
      fi
      cat "$err_file" >&2
      exit 1
    fi

    compute_rc=0
    patch=$("$compute" "$json_file") || compute_rc=$?
    if [ "$compute_rc" -ne 0 ]; then
      if [ "$compute_rc" -ne 4 ]; then
        echo "ERROR: failed to compute the $patch_subject merge patch from the live $object" >&2
      fi
      exit 1
    fi
    if [ -z "$patch" ]; then
      return 3
    fi

    if oc patch "${ns_args[@]}" "$resource" --type=merge --patch="$patch" \
        --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$err_file"; then
      return 0
    fi
    if ! grep -q '(Conflict)' "$err_file"; then
      cat "$err_file" >&2
      exit 1
    fi
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
      echo "ERROR: the $object was modified concurrently on every attempt ($MAX_ATTEMPTS); giving up" >&2
      cat "$err_file" >&2
      exit 1
    fi
    echo "$object was modified concurrently (conflict); retrying (attempt $attempt of $MAX_ATTEMPTS)" >&2
    attempt=$((attempt + 1))
    sleep "$RETRY_SLEEP"
  done
)

# Creates the object described by the remaining arguments, passed to
# `oc create` verbatim (payload, namespace, and any flags are the caller's).
# Returns 0 on success and 3 when the object already exists — a concurrent
# creation the caller cannot merge into blind; any other failure exits 1
# after printing the server error.
create_object() {
  local object="$1"; shift
  local err_file create_rc=0
  err_file=$(mktemp) || exit 1
  oc create "$@" --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$err_file" || create_rc=$?
  if [ "$create_rc" -eq 0 ]; then
    rm -f "$err_file"
    return 0
  fi
  if grep -q '(AlreadyExists)' "$err_file"; then
    rm -f "$err_file"
    echo "the $object was created concurrently; re-running this hook merges into it" >&2
    return 3
  fi
  cat "$err_file" >&2
  rm -f "$err_file"
  exit 1
}

# Deletes the object named by the remaining arguments, passed to
# `oc delete` verbatim. Returns 0 on success and 3 when the object is
# already absent; any other failure exits 1 after printing the server error.
delete_object() {
  local object="$1"; shift
  local err_file delete_rc=0
  err_file=$(mktemp) || exit 1
  oc delete "$@" --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$err_file" || delete_rc=$?
  if [ "$delete_rc" -eq 0 ]; then
    rm -f "$err_file"
    return 0
  fi
  if grep -q '(NotFound)' "$err_file"; then
    rm -f "$err_file"
    return 3
  fi
  cat "$err_file" >&2
  rm -f "$err_file"
  exit 1
}
{{ end }}
