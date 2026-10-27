#!/bin/bash
set -o pipefail

: "${TENANT_NAMESPACE:?TENANT_NAMESPACE is required}"
: "${TENANT_WAIT_TIMEOUT:?TENANT_WAIT_TIMEOUT is required}"
SECRET_NAME="ds-pipeline-s3-dspa"

umask 077
workdir="$(mktemp -d)"
cleanup() { rm -rf "$workdir"; }
trap cleanup EXIT

deadline=$(( $(date +%s) + TENANT_WAIT_TIMEOUT ))

while true; do
  marker="$(oc get secret "$SECRET_NAME" -n "$TENANT_NAMESPACE" \
    -ojsonpath='{.metadata.name}' --ignore-not-found --request-timeout=10s)" || {
    echo "Error: unexpected failure checking secret ${SECRET_NAME} in namespace ${TENANT_NAMESPACE}" >&2
    exit 1
  }
  [ -n "$marker" ] && break
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "Error: timed out after ${TENANT_WAIT_TIMEOUT}s waiting for secret ${SECRET_NAME} in namespace ${TENANT_NAMESPACE}" >&2
    exit 1
  fi
  sleep 2
done

for key in accesskey secretkey; do
  value="$(oc get secret "$SECRET_NAME" -n "$TENANT_NAMESPACE" \
    -ojsonpath="{.data.${key}}" --request-timeout=10s)" || {
    echo "Error: failed to read the ${key} key from secret ${SECRET_NAME} in namespace ${TENANT_NAMESPACE} — the error body was suppressed deliberately to avoid forwarding credential-bearing output" >&2
    exit 1
  }
  case "$key" in
    accesskey) file="$workdir/AWS_ACCESS_KEY_ID" ;;
    secretkey) file="$workdir/AWS_SECRET_ACCESS_KEY" ;;
  esac
  printf '%s' "$value" | base64 -d > "$file" || {
    echo "Error: the ${key} key of secret ${SECRET_NAME} is not valid base64" >&2
    exit 1
  }
  if ! [ -s "$file" ] || ! grep -q '[^[:space:]]' "$file"; then
    echo "Error: the ${key} key of secret ${SECRET_NAME} is empty" >&2
    exit 1
  fi
done

printf '%s' "http://minio-dspa.${TENANT_NAMESPACE}.svc.cluster.local:9000" > "$workdir/AWS_S3_ENDPOINT"
printf '%s' "mlpipeline" > "$workdir/AWS_S3_BUCKET"
printf '%s' "us-east-1" > "$workdir/AWS_DEFAULT_REGION"

if ! oc set data "secret/${SECRET_NAME}" -n "$TENANT_NAMESPACE" \
    --from-file=AWS_ACCESS_KEY_ID="$workdir/AWS_ACCESS_KEY_ID" \
    --from-file=AWS_SECRET_ACCESS_KEY="$workdir/AWS_SECRET_ACCESS_KEY" \
    --from-file=AWS_S3_ENDPOINT="$workdir/AWS_S3_ENDPOINT" \
    --from-file=AWS_S3_BUCKET="$workdir/AWS_S3_BUCKET" \
    --from-file=AWS_DEFAULT_REGION="$workdir/AWS_DEFAULT_REGION" \
    2>"$workdir/set.err"; then
  echo "Error: failed to update the data of secret ${SECRET_NAME} in namespace ${TENANT_NAMESPACE} (oc set data --from-file) — the error body was suppressed deliberately to avoid forwarding credential-bearing output" >&2
  exit 1
fi
echo "secret ${SECRET_NAME} updated in namespace ${TENANT_NAMESPACE}"
