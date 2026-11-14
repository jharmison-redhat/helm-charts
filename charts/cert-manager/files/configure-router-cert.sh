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

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

IC="ingresscontrollers.operator.openshift.io/default"
IC_NAMESPACE="openshift-ingress-operator"
REFERENCE_FILE="${REFERENCE_FILE:-/mnt/router-reference.json}"

require_oc "cannot configure the default IngressController certificate"
select_json_tool "cannot patch the defaultCertificate reference"

# Computes the defaultCertificate merge patch from the live IngressController
# JSON read from $1. Prints the patch on stdout, prints nothing when the live
# reference already matches the expected one (no-op), and exits 4 with a
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
            | halt_error(4)
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
    sys.exit(4)
PYEOF
      ;;
  esac
}

write_rc=0
conditional_write "$IC" compute_patch "defaultCertificate" "IngressController" \
  "$IC_NAMESPACE" || write_rc=$?
case $write_rc in
  0)
    echo "Router default certificate reference configured"
    ;;
  3)
    echo "The default IngressController already serves the expected certificate reference; no changes needed"
    ;;
  2)
    echo "ERROR: prerequisite missing: the default IngressController ($IC) does not exist on this cluster; this hook only applies to OpenShift clusters" >&2
    exit 1
    ;;
  *)
    exit 1
    ;;
esac
