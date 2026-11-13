#!/bin/bash
# Patches the default IngressController (operator.openshift.io/v1, name
# "default") to serve this chart's router certificate Secret. The
# IngressController is a platform-owned object that this release never owns
# or deletes, so the reference is written through a hook Job. The write is a
# read-modify-write merge patch carrying the live metadata.resourceVersion:
# a stale version is rejected with HTTP 409 and retried. An unset
# defaultCertificate is the stock-cluster state and a valid target; a set
# reference to any other Secret is foreign and stops the hook without
# writing, so another owner's certificate is never clobbered.
set -euo pipefail

IC="ingresscontrollers.operator.openshift.io/default"
REFERENCE_FILE="${REFERENCE_FILE:-/mnt/router-reference.json}"
REQUEST_TIMEOUT="30s"
MAX_ATTEMPTS=5
RETRY_SLEEP=2

if ! command -v oc >/dev/null 2>&1; then
  echo "ERROR: the oc CLI is not available in this image; cannot configure the default IngressController certificate" >&2
  exit 1
fi

if command -v jq >/dev/null 2>&1; then
  JSON_TOOL=jq
elif command -v python3 >/dev/null 2>&1; then
  JSON_TOOL=python3
else
  echo "ERROR: neither jq nor python3 is available in this image; cannot patch the defaultCertificate reference" >&2
  exit 1
fi

# Computes the defaultCertificate merge patch from the live IngressController
# JSON read from $1. Prints the patch on stdout, prints nothing when the live
# reference already matches the expected one (no-op), and exits 3 with a
# diagnostic when the live reference is foreign.
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile ref "$REFERENCE_FILE" '
        . as $doc
        | ($ref[0]) as $ref
        | ($doc.spec.defaultCertificate // null) as $have
        | if $have == null then
            {metadata: {resourceVersion: $doc.metadata.resourceVersion},
             spec: {defaultCertificate: $ref}}
          elif $have == $ref then empty
          else
            ("the default IngressController already references default certificate "
             + (($have.name // "<unset>"))
             + " — refusing to overwrite a foreign reference; remove or restore "
             + "the existing defaultCertificate manually, then re-run this hook")
            | halt_error(3)
          end' "$1"
      ;;
    python3)
      python3 - "$REFERENCE_FILE" "$1" <<'PYEOF'
import json, sys

ref_file, ic_file = sys.argv[1], sys.argv[2]
with open(ref_file) as f:
    ref = json.load(f)
with open(ic_file) as f:
    doc = json.load(f)
have = doc.get("spec", {}).get("defaultCertificate")
if have is None:
    print(json.dumps({
        "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
        "spec": {"defaultCertificate": ref},
    }))
elif have == ref:
    pass
else:
    sys.stderr.write(
        "the default IngressController already references default certificate "
        + str(have.get("name", "<unset>"))
        + " — refusing to overwrite a foreign reference; remove or restore "
        + "the existing defaultCertificate manually, then re-run this hook\n")
    sys.exit(3)
PYEOF
      ;;
  esac
}

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
ic_json="$workdir/ic.json"
oc_err="$workdir/oc.err"
patch_err="$workdir/patch.err"

attempt=1
while :; do
  if ! oc get "$IC" -n openshift-ingress-operator -o json \
      --request-timeout="$REQUEST_TIMEOUT" \
      >"$ic_json" 2>"$oc_err"; then
    if grep -q '(NotFound)' "$oc_err"; then
      echo "ERROR: prerequisite missing: the default IngressController ($IC) does not exist on this cluster; this hook only applies to OpenShift clusters" >&2
      exit 1
    fi
    cat "$oc_err" >&2
    exit 1
  fi

  rc=0
  merged=$(compute_patch "$ic_json" 2>"$patch_err") || rc=$?
  case "$rc" in
    0) ;;
    3) cat "$patch_err" >&2; exit 1 ;;
    *)
      echo "ERROR: failed to compute the defaultCertificate merge patch from the live IngressController" >&2
      cat "$patch_err" >&2
      exit 1
      ;;
  esac
  if [ -z "$merged" ]; then
    echo "The default IngressController already serves the expected certificate reference; no changes needed"
    exit 0
  fi

  if oc patch "$IC" -n openshift-ingress-operator --type=merge --patch="$merged" \
      --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$oc_err"; then
    echo "Router default certificate reference configured"
    exit 0
  fi

  if ! grep -q '(Conflict)' "$oc_err"; then
    cat "$oc_err" >&2
    exit 1
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "ERROR: the IngressController was modified concurrently on every attempt ($MAX_ATTEMPTS); giving up" >&2
    cat "$oc_err" >&2
    exit 1
  fi
  echo "IngressController was modified concurrently (conflict); retrying (attempt $attempt of $MAX_ATTEMPTS)" >&2
  attempt=$((attempt + 1))
  sleep "$RETRY_SLEEP"
done
