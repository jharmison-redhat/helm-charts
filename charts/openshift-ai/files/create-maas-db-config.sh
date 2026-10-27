#!/bin/bash
set -euo pipefail

: "${DB_SOURCE_SECRET:?DB_SOURCE_SECRET is required}"
: "${DB_NAMESPACE:?DB_NAMESPACE is required}"
: "${DB_WAIT_TIMEOUT:?DB_WAIT_TIMEOUT is required}"

# Fixed poll interval (seconds) and per-request cap; the wait budget is the
# wall-clock deadline set from DB_WAIT_TIMEOUT
POLL_INTERVAL=2
REQUEST_TIMEOUT=10
WAIT_TIMEOUT="$DB_WAIT_TIMEOUT"

umask 077
workdir="$(mktemp -d)"
cleanup() { rm -rf "$workdir"; }
trap cleanup EXIT

deadline=$(( $(date +%s) + WAIT_TIMEOUT ))

remaining_seconds() { echo $(( deadline - $(date +%s) )); }

# Returns 0 when the source secret exists, 1 when it is absent (retry), 2
# when the wall-clock wait budget is exhausted, 3 on any unexpected API
# failure (Forbidden, Unauthorized, transport errors, ...). A missing secret
# or namespace yields no marker under --ignore-not-found and is retried.
check_source_secret() {
  local remaining budget marker
  remaining=$(remaining_seconds)
  if [ "$remaining" -le 0 ]; then
    return 2
  fi
  budget=$remaining
  if [ "$budget" -gt "$REQUEST_TIMEOUT" ]; then
    budget=$REQUEST_TIMEOUT
  fi
  marker="$(oc get secret "$DB_SOURCE_SECRET" -n "$DB_NAMESPACE" \
    -ojsonpath='{.metadata.name}' --ignore-not-found \
    --request-timeout="${budget}s")" || {
    echo "Error: unexpected failure checking secret ${DB_SOURCE_SECRET} in namespace ${DB_NAMESPACE}" >&2
    return 3
  }
  [ -n "$marker" ]
}

while true; do
  rc=0
  check_source_secret || rc=$?
  if [ "$rc" -eq 0 ]; then
    break
  fi
  if [ "$rc" -eq 2 ]; then
    echo "Error: timed out after ${WAIT_TIMEOUT}s waiting for secret ${DB_SOURCE_SECRET} in namespace ${DB_NAMESPACE}" >&2
    exit 1
  fi
  if [ "$rc" -eq 3 ]; then
    exit 1
  fi
  remaining=$(remaining_seconds)
  if [ "$remaining" -le 0 ]; then
    echo "Error: timed out after ${WAIT_TIMEOUT}s waiting for secret ${DB_SOURCE_SECRET} in namespace ${DB_NAMESPACE}" >&2
    exit 1
  fi
  if [ "$remaining" -lt "$POLL_INTERVAL" ]; then
    sleep "$remaining"
  else
    sleep "$POLL_INTERVAL"
  fi
done

# Read the uri key with a checked command status, then decode into a private
# file. Both pipeline sides are checked: oc via ||, base64 via pipefail.
uri_b64="$(oc get secret "$DB_SOURCE_SECRET" -n "$DB_NAMESPACE" \
  -ojsonpath='{.data.uri}' --request-timeout="${REQUEST_TIMEOUT}s")" || {
  echo "Error: failed to read the uri key from secret ${DB_SOURCE_SECRET} in namespace ${DB_NAMESPACE}" >&2
  exit 1
}
printf '%s' "$uri_b64" | base64 -d > "$workdir/DB_CONNECTION_URL" || {
  echo "Error: the uri key of secret ${DB_SOURCE_SECRET} is not valid base64" >&2
  exit 1
}
if ! [ -s "$workdir/DB_CONNECTION_URL" ] || \
   ! grep -q '[^[:space:]]' "$workdir/DB_CONNECTION_URL"; then
  echo "Error: the uri key of secret ${DB_SOURCE_SECRET} is empty" >&2
  exit 1
fi

# Construct the target Secret client-side from the private file and apply it.
# Pipeline failures on either side abort before any partial apply; error
# bodies are kept out of stdout/stderr so credentials never leak through
# client/server diagnostics.
if ! oc create secret generic maas-db-config -n "$DB_NAMESPACE" \
      --from-file=DB_CONNECTION_URL="$workdir/DB_CONNECTION_URL" \
      --dry-run=client -oyaml 2>"$workdir/create.err" \
   | oc apply -f- >/dev/null 2>"$workdir/apply.err"; then
  echo "Error: failed to apply the maas-db-config secret in namespace ${DB_NAMESPACE} — see the pod's private error logs" >&2
  exit 1
fi
echo "maas-db-config secret applied in namespace ${DB_NAMESPACE}"
