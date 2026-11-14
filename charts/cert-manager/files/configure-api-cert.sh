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

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

APISERVER="apiservers.config.openshift.io/cluster"
REFERENCE_FILE="${REFERENCE_FILE:-/mnt/api-reference.json}"

require_oc "cannot configure the APIServer certificate"
select_json_tool "cannot patch the namedCertificates list"

# Computes the namedCertificates merge patch from the live APIServer JSON
# read from $1. Prints the patch on stdout, prints nothing when the live
# list is exactly the expected one (no-op), and exits 4 with a diagnostic
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
            | halt_error(4)
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
    sys.exit(4)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"servingCerts": {"namedCertificates": want}},
}))
PYEOF
      ;;
  esac
}

write_rc=0
conditional_write "$APISERVER" compute_patch "namedCertificates" "APIServer" \
  || write_rc=$?
case $write_rc in
  0)
    echo "API server certificate reference configured"
    ;;
  3)
    echo "The APIServer already serves the expected certificate reference; no changes needed"
    ;;
  2)
    echo "ERROR: prerequisite missing: the cluster APIServer object ($APISERVER) does not exist on this cluster; this hook only applies to OpenShift clusters" >&2
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
