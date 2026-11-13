#!/bin/bash
# Patches the cluster APIServer (config.openshift.io/v1, name "cluster") to
# serve this chart's API certificate Secret through servingCerts.
# namedCertificates. The APIServer is a platform-owned object that this
# release never owns or deletes, so the reference is written through a hook
# Job. The write is a read-modify-write merge patch carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried. namedCertificates is an atomic list, so entries cannot be merged
# per-item: an empty list is a valid target and receives our entry, a list
# exactly equal to our entry is a no-op, and any other list is foreign and
# stops the hook without writing, so another owner's entries are never
# clobbered.
set -euo pipefail

APISERVER="apiservers.config.openshift.io/cluster"
REFERENCE_FILE="${REFERENCE_FILE:-/mnt/api-reference.json}"
REQUEST_TIMEOUT="30s"
MAX_ATTEMPTS=5
RETRY_SLEEP=2

if ! command -v oc >/dev/null 2>&1; then
  echo "ERROR: the oc CLI is not available in this image; cannot configure the APIServer certificate" >&2
  exit 1
fi

if command -v jq >/dev/null 2>&1; then
  JSON_TOOL=jq
elif command -v python3 >/dev/null 2>&1; then
  JSON_TOOL=python3
else
  echo "ERROR: neither jq nor python3 is available in this image; cannot patch the namedCertificates list" >&2
  exit 1
fi

# Computes the namedCertificates merge patch from the live APIServer JSON
# read from $1. Prints the patch on stdout, prints nothing when the live
# list is exactly the expected one (no-op), and exits 3 with a diagnostic
# when the live list is foreign.
compute_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --slurpfile ref "$REFERENCE_FILE" '
        . as $doc
        | ($ref[0]) as $ref
        | ($doc.spec.servingCerts.namedCertificates // []) as $have
        | if ($have | length) == 0 then
            {metadata: {resourceVersion: $doc.metadata.resourceVersion},
             spec: {servingCerts: {namedCertificates: [$ref]}}}
          elif $have == [$ref] then empty
          else
            ("the APIServer namedCertificates list is already set to a foreign configuration "
             + "(names: "
             + (($have
                 | map(.names // [])
                 | flatten
                 | if length > 0 then join(", ") else "<none>" end))
             + ") — refusing to overwrite a foreign reference; remove the "
             + "existing namedCertificates entries manually, then re-run this hook")
            | halt_error(3)
          end' "$1"
      ;;
    python3)
      python3 - "$REFERENCE_FILE" "$1" <<'PYEOF'
import json, sys

ref_file, api_file = sys.argv[1], sys.argv[2]
with open(ref_file) as f:
    ref = json.load(f)
with open(api_file) as f:
    doc = json.load(f)
have = doc.get("spec", {}).get("servingCerts", {}).get("namedCertificates") or []
if not have:
    want = [ref]
elif have == [ref]:
    sys.exit(0)
else:
    names = [name for entry in have for name in entry.get("names", [])]
    shown = ", ".join(names) if names else "<none>"
    sys.stderr.write(
        "the APIServer namedCertificates list is already set to a foreign "
        + "configuration (names: " + shown + ") — refusing to overwrite a "
        + "foreign reference; remove the existing namedCertificates entries "
        + "manually, then re-run this hook\n")
    sys.exit(3)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"servingCerts": {"namedCertificates": want}},
}))
PYEOF
      ;;
  esac
}

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT
api_json="$workdir/apiserver.json"
oc_err="$workdir/oc.err"
patch_err="$workdir/patch.err"

attempt=1
while :; do
  if ! oc get "$APISERVER" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$api_json" 2>"$oc_err"; then
    if grep -q '(NotFound)' "$oc_err"; then
      echo "ERROR: prerequisite missing: the cluster APIServer object ($APISERVER) does not exist on this cluster; this hook only applies to OpenShift clusters" >&2
      exit 1
    fi
    cat "$oc_err" >&2
    exit 1
  fi

  rc=0
  merged=$(compute_patch "$api_json" 2>"$patch_err") || rc=$?
  case "$rc" in
    0) ;;
    3) cat "$patch_err" >&2; exit 1 ;;
    *)
      echo "ERROR: failed to compute the namedCertificates merge patch from the live APIServer" >&2
      cat "$patch_err" >&2
      exit 1
      ;;
  esac
  if [ -z "$merged" ]; then
    echo "The APIServer already serves the expected certificate reference; no changes needed"
    exit 0
  fi

  if oc patch "$APISERVER" --type=merge --patch="$merged" \
      --request-timeout="$REQUEST_TIMEOUT" >/dev/null 2>"$oc_err"; then
    echo "API server certificate reference configured"
    exit 0
  fi

  if ! grep -q '(Conflict)' "$oc_err"; then
    cat "$oc_err" >&2
    exit 1
  fi
  if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
    echo "ERROR: the APIServer was modified concurrently on every attempt ($MAX_ATTEMPTS); giving up" >&2
    cat "$oc_err" >&2
    exit 1
  fi
  echo "APIServer was modified concurrently (conflict); retrying (attempt $attempt of $MAX_ATTEMPTS)" >&2
  attempt=$((attempt + 1))
  sleep "$RETRY_SLEEP"
done
