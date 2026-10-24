#!/bin/bash
# Waits for the product-managed MaasTenantConfig/default-tenant singleton to
# appear, then merge-patches only {spec: ...}. The singleton itself is created
# by the maas-controller; this script neither creates nor adopts it.
#
# Expected startup absence is a 404 (missing API, namespace or singleton); any
# other failure (Forbidden, Unauthorized, transport) exits immediately.
set -euo pipefail

: "${MAAS_TENANT_NAMESPACE:?MAAS_TENANT_NAMESPACE is required}"
: "${MAAS_TENANT_WAIT_TIMEOUT:?MAAS_TENANT_WAIT_TIMEOUT is required}"
MAAS_TENANT_POLL_INTERVAL="${MAAS_TENANT_POLL_INTERVAL:-5}"
MAAS_TENANT_PATCH_FILE="${MAAS_TENANT_PATCH_FILE:-/config/patch.json}"
MAAS_TENANT_REQUEST_TIMEOUT="${MAAS_TENANT_REQUEST_TIMEOUT:-10s}"

resource_path="/apis/maas.opendatahub.io/v1alpha1/namespaces/${MAAS_TENANT_NAMESPACE}/maastenantconfigs/default-tenant"
deadline=$(( $(date +%s) + MAAS_TENANT_WAIT_TIMEOUT ))

while true; do
  if out=$(oc get --raw "${resource_path}" --request-timeout="${MAAS_TENANT_REQUEST_TIMEOUT}" 2>&1); then
    break
  fi
  case "${out}" in
    *"404"*|*"could not find the requested resource"*)
      if [ "$(date +%s)" -ge "${deadline}" ]; then
        echo "ERROR: MaasTenantConfig/default-tenant did not appear in namespace ${MAAS_TENANT_NAMESPACE} within ${MAAS_TENANT_WAIT_TIMEOUT}s — install the RHOAI MaaS components and re-run, or raise waitTimeoutSeconds" >&2
        exit 1
      fi
      sleep "${MAAS_TENANT_POLL_INTERVAL}"
      ;;
    *)
      echo "ERROR: unexpected failure waiting for MaasTenantConfig/default-tenant in namespace ${MAAS_TENANT_NAMESPACE}: ${out}" >&2
      exit 1
      ;;
  esac
done

oc patch maastenantconfig default-tenant \
  --namespace "${MAAS_TENANT_NAMESPACE}" \
  --type=merge \
  --patch-file "${MAAS_TENANT_PATCH_FILE}" \
  --request-timeout="${MAAS_TENANT_REQUEST_TIMEOUT}"
