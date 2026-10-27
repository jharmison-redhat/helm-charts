#!/bin/bash
set -o pipefail

: "${DB_SOURCE_SECRET:?DB_SOURCE_SECRET is required}"
: "${DB_NAMESPACE:?DB_NAMESPACE is required}"
: "${DB_WAIT_TIMEOUT:?DB_WAIT_TIMEOUT is required}"

umask 077
workdir="$(mktemp -d)"
cleanup() { rm -rf "$workdir"; }
trap cleanup EXIT

deadline=$(( $(date +%s) + DB_WAIT_TIMEOUT ))

while true; do
  marker="$(oc get secret "$DB_SOURCE_SECRET" -n "$DB_NAMESPACE" \
    -ojsonpath='{.metadata.name}' --ignore-not-found --request-timeout=10s)" || {
    echo "Error: unexpected failure checking secret ${DB_SOURCE_SECRET} in namespace ${DB_NAMESPACE}" >&2
    exit 1
  }
  [ -n "$marker" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Error: timed out after ${DB_WAIT_TIMEOUT}s waiting for secret ${DB_SOURCE_SECRET} in namespace ${DB_NAMESPACE}" >&2
    exit 1
  fi
  sleep 2
done

uri="$(oc get secret "$DB_SOURCE_SECRET" -n "$DB_NAMESPACE" \
  -ojsonpath='{.data.uri}' --request-timeout=10s)" || {
  echo "Error: failed to read the uri key from secret ${DB_SOURCE_SECRET} in namespace ${DB_NAMESPACE} — the error body was suppressed deliberately to avoid forwarding credential-bearing output" >&2
  exit 1
}
printf '%s' "$uri" | base64 -d > "$workdir/DB_CONNECTION_URL" || {
  echo "Error: the uri key of secret ${DB_SOURCE_SECRET} is not valid base64" >&2
  exit 1
}
if ! [ -s "$workdir/DB_CONNECTION_URL" ] || \
   ! grep -q '[^[:space:]]' "$workdir/DB_CONNECTION_URL"; then
  echo "Error: the uri key of secret ${DB_SOURCE_SECRET} is empty" >&2
  exit 1
fi

if ! oc create secret generic maas-db-config -n "$DB_NAMESPACE" \
      --from-file=DB_CONNECTION_URL="$workdir/DB_CONNECTION_URL" \
      --dry-run=client -oyaml 2>"$workdir/create.err" \
   | oc apply -f- >/dev/null 2>"$workdir/apply.err"; then
  echo "Error: failed to apply the maas-db-config secret in namespace ${DB_NAMESPACE} (client dry-run create plus apply) — the error body was suppressed deliberately to avoid forwarding credential-bearing output" >&2
  exit 1
fi
echo "maas-db-config secret applied in namespace ${DB_NAMESPACE}"
