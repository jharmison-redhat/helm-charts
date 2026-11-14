#!/bin/bash
# Propagates the CA certificate from the cert-manager-ca Secret into the
# openshift-config/user-ca-bundle ConfigMap and sets the cluster Proxy's
# spec.trustedCA reference to it, so in-cluster clients trust certificates
# issued by ca-issuer. Both objects are user-owned cluster inputs: this
# release never modifies content it did not install — PEM blocks this hook
# did not install are preserved in place, and a Proxy reference to any
# other ConfigMap stops the hook without writing. The remove hook deletes
# the bundle only once removing this chart's entries has emptied it.
#
# Identity is byte-equality, evaluated per PEM block: a block is
# ours-current when it equals the Secret's current tls.crt, and
# ours-recorded when its base64 payload is listed in the bundle's
# trust-ca-history annotation. The annotation is replaced — not appended
# to — on every run: it records exactly the payload installed by the run
# that wrote it, and a recorded block that is no longer current is pruned,
# so the hook maintains exactly one of its own entries and repeated CA
# replacements do not accumulate. Payloads are stored rather than hashes
# because jq has no hash function and the mechanics must run under either
# tool.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried. The trust-ca-owns-proxy-reference annotation is written only on
# a transition into owning the reference, and only after the reference
# write itself has succeeded — a failed reference write leaves no ownership
# record behind, so a reference a third party later sets is never claimed
# and teardown leaves it in place.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"
source "${BASH_SOURCE[0]%/*}/trust-bundle.sh"

CA_NAMESPACE="cert-manager"
SECRET="secret/cert-manager-ca"
PROXY="proxies.config.openshift.io/cluster"
BUNDLE="configmap/user-ca-bundle"
BUNDLE_NAME="user-ca-bundle"
BUNDLE_NAMESPACE="openshift-config"
BUNDLE_KEY="ca-bundle.crt"
SECRET_KEY="tls.crt"
OWNERSHIP_ANNOTATION="trust-ca-owns-proxy-reference"
HISTORY_ANNOTATION="trust-ca-history"
export BUNDLE_NAME BUNDLE_NAMESPACE BUNDLE_KEY SECRET_KEY \
  OWNERSHIP_ANNOTATION HISTORY_ANNOTATION

require_oc "cannot propagate the CA into cluster trust"
select_json_tool "cannot manipulate the cluster trust bundle"

TRUST_CA_TIMEOUT="${TRUST_CA_TIMEOUT:-120}"
# In-script wait poll interval; the Helm Job leaves this at the default
TRUST_CA_POLL_INTERVAL="${TRUST_CA_POLL_INTERVAL:-5}"

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT
SECRET_FILE="$workdir/secret.json"
PROXY_FILE="$workdir/proxy.json"
BUNDLE_FILE="$workdir/bundle.json"

# Waits bounded for the cert-manager-ca Secret, then fetches it. Both the
# self-provisioned Certificate and a BYO Secret are applied in the same
# release, but self-provisioned issuance is asynchronous, so the Secret may
# not exist yet when the hook starts.
wait_for_secret() {
  local deadline err
  deadline=$(( $(date +%s) + TRUST_CA_TIMEOUT ))
  while ! err=$(oc get "$SECRET" -n "$CA_NAMESPACE" -o json \
                   --request-timeout="$REQUEST_TIMEOUT" \
                   2>&1 >"$SECRET_FILE"); do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      echo "ERROR: timed out after ${TRUST_CA_TIMEOUT}s waiting for the cert-manager-ca Secret in namespace ${CA_NAMESPACE}; the CA family (self-provisioned issuance or the bring-your-own Secret) must exist before cluster trust can be configured. Last error:" >&2
      echo "$err" >&2
      exit 1
    fi
    sleep "$TRUST_CA_POLL_INTERVAL"
  done
}

# Prints "adopt" when the bundle JSON in $1 decomposes cleanly and holds
# exactly one PEM block byte-equal to the current certificate in $2 (prior
# output of this hook), otherwise prints nothing.
bundle_is_adoptable() {
  case "$JSON_TOOL" in
    jq)
      jq -r --arg key "$BUNDLE_KEY" --arg cur "$2" \
            "$TRUST_PEM_JQ_DEFS"'
        (.data[$key] // "") as $raw
        | if (($raw | pem_leftover) | test("^\\s*$"))
            and (($raw | [pem_blocks[] | pem_term]) == [$cur])
          then "adopt" else empty end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

exec(os.environ["TRUST_PEM_PY"])

with open(sys.argv[1]) as f:
    doc = json.load(f)
raw = (doc.get("data") or {}).get(os.environ["BUNDLE_KEY"]) or ""
try:
    blocks = pem_parse(raw)
except ValueError:
    sys.exit(0)
if blocks == [os.environ["CURRENT_BLOCK"]]:
    print("adopt")
PYEOF
      ;;
  esac
}

# Computes the bundle merge patch from the live bundle JSON in $1: the
# pruned/merged content plus the history annotation. Emits nothing when
# content and annotation already match; exits 4 after its own diagnostic on
# content that does not decompose into recognized PEM blocks plus
# whitespace, or is certificate-free. Foreign blocks are never removed or
# reordered, and the current block is appended only when absent — an
# already-present current block is never repositioned.
compute_bundle_set() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg key "$BUNDLE_KEY" --arg cur "$CURRENT_BLOCK" \
            --arg hist_ann "$HISTORY_ANNOTATION" \
            --arg obj "the user-ca-bundle ConfigMap content" \
            --arg tail "fix the bundle manually and re-run this hook" \
            --arg fatal_empty true \
            "$TRUST_PEM_JQ_DEFS"'
        . as $doc
        | pem_guard($obj; $tail; $fatal_empty) as $have
        | (try (($doc.metadata.annotations[$hist_ann] // "[]") | fromjson
                | (if type == "array" then . else [] end)) catch []) as $hist_list
        | [$have[]
           | select((@base64) as $pb
                    | ($pb == ($cur | @base64))
                      or ($hist_list | index($pb) == null))] as $kept
        | (if ($kept | index($cur)) == null then $kept + [$cur] else $kept end) as $want
        | ([($cur | @base64)] | tojson) as $newhist
        | if $have == $want
             and (($doc.metadata.annotations[$hist_ann] // "") == $newhist)
          then empty
          else {metadata:
                  {resourceVersion: $doc.metadata.resourceVersion,
                   annotations: {($hist_ann): $newhist}},
                data: {($key): ($want | join(""))}}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import base64, json, os, sys

exec(os.environ["TRUST_PEM_PY"])

with open(sys.argv[1]) as f:
    doc = json.load(f)
key = os.environ["BUNDLE_KEY"]
cur = os.environ["CURRENT_BLOCK"]
hist_ann = os.environ["HISTORY_ANNOTATION"]

raw = (doc.get("data") or {}).get(key) or ""
try:
    blocks = pem_parse(raw)
except ValueError as err:
    pem_fail("the user-ca-bundle ConfigMap content",
             "fix the bundle manually and re-run this hook", str(err))

try:
    hist_list = json.loads(
        (doc.get("metadata", {}).get("annotations") or {}).get(hist_ann) or "[]")
except (ValueError, TypeError):
    hist_list = []
if not isinstance(hist_list, list):
    hist_list = []
cur_b64 = base64.b64encode(cur.encode()).decode()

def recorded(block):
    return base64.b64encode(block.encode()).decode() in hist_list

kept = [b for b in blocks if b == cur or not recorded(b)]
want = kept if cur in kept else kept + [cur]
newhist = json.dumps([cur_b64])
anns = doc.get("metadata", {}).get("annotations") or {}
if blocks == want and anns.get(hist_ann) == newhist:
    sys.exit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"],
                 "annotations": {hist_ann: newhist}},
    "data": {key: "".join(want)},
}))
PYEOF
      ;;
  esac
}

# Computes the reference-ownership annotation patch from the live bundle
# JSON in $1 (the transition into owning the reference: the hook set the
# reference itself, or adopted prior output that already pointed it at
# exactly this chart's current CA); emits nothing when the annotation is
# already recorded.
compute_bundle_adopt() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg own_ann "$OWNERSHIP_ANNOTATION" '
        if ((.metadata.annotations[$own_ann] // "") == "true") then empty
        else {metadata: {resourceVersion: .metadata.resourceVersion,
                         annotations: {($own_ann): "true"}}}
        end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
own_ann = os.environ["OWNERSHIP_ANNOTATION"]
anns = doc.get("metadata", {}).get("annotations") or {}
if anns.get(own_ann) == "true":
    sys.exit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"],
                 "annotations": {own_ann: "true"}},
}))
PYEOF
      ;;
  esac
}

# Computes the trustedCA reference patch from the live Proxy JSON in $1;
# re-checks the reference on the fresh read, so a reference that turned
# foreign between the initial read and this write stops the hook instead of
# overwriting it.
compute_proxy_ref() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg name "$BUNDLE_NAME" '
        (.spec.trustedCA.name // "") as $ref
        | if $ref == $name then empty
          elif $ref != "" then
            ("the cluster Proxy references trust bundle " + $ref + " — "
             + "refusing to overwrite a foreign reference; point spec.trustedCA "
             + "at " + $name + " or unset it manually, then re-run this hook")
            | halt_error(4)
          else {metadata: {resourceVersion: .metadata.resourceVersion},
                spec: {trustedCA: {name: $name}}}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
name = os.environ["BUNDLE_NAME"]
ref = (((doc.get("spec") or {}).get("trustedCA") or {}).get("name")) or ""
if ref == name:
    sys.exit(0)
if ref:
    sys.stderr.write(
        "the cluster Proxy references trust bundle " + ref + " — refusing "
        "to overwrite a foreign reference; point spec.trustedCA at " + name
        + " or unset it manually, then re-run this hook\n")
    sys.exit(4)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"trustedCA": {"name": name}},
}))
PYEOF
      ;;
  esac
}

# Prints the ConfigMap manifest that creates the trust bundle with the
# content in $1. The ownership annotation is deliberately absent here: it
# is written by its own conditional write only after the reference write
# succeeds, so a failed reference write never leaves an ownership record on
# a reference this hook did not set.
bundle_manifest() {
  case "$JSON_TOOL" in
    jq)
      jq -n -c --arg name "$BUNDLE_NAME" --arg ns "$BUNDLE_NAMESPACE" \
            --arg key "$BUNDLE_KEY" --arg content "$1" \
            --arg hist_ann "$HISTORY_ANNOTATION" '
        {apiVersion: "v1", kind: "ConfigMap",
         metadata:
           {name: $name, namespace: $ns,
            annotations: {($hist_ann): ([$content | @base64] | tojson)}},
         data: {($key): $content}}'
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import base64, json, os, sys

content = sys.argv[1]
print(json.dumps({
    "apiVersion": "v1", "kind": "ConfigMap",
    "metadata": {"name": os.environ["BUNDLE_NAME"],
                 "namespace": os.environ["BUNDLE_NAMESPACE"],
                 "annotations": {os.environ["HISTORY_ANNOTATION"]: json.dumps(
                     [base64.b64encode(content.encode()).decode()])}},
    "data": {os.environ["BUNDLE_KEY"]: content},
}))
PYEOF
      ;;
  esac
}

wait_for_secret
extracted=$(secret_cert_block "$SECRET_FILE") || {
  echo "ERROR: the cert-manager-ca Secret's tls.crt is not decodable base64; cannot extract the CA certificate" >&2
  exit 1
}
CURRENT_BLOCK=""
if [ -n "$extracted" ]; then
  # Command substitution strips the canonical block's trailing newline;
  # restore it so byte-equality against bundle blocks holds
  printf -v CURRENT_BLOCK '%s\n' "$extracted"
fi
if [ -z "$CURRENT_BLOCK" ]; then
  echo "ERROR: the cert-manager-ca Secret carries no PEM certificate in tls.crt — refusing to install an empty or certificate-free trust bundle (an empty referenced bundle degrades the cluster Proxy); check the CA family issuance" >&2
  exit 1
fi
export CURRENT_BLOCK

if ! oc get "$PROXY" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$PROXY_FILE" 2>"$workdir/oc.err"; then
  if grep -q '(NotFound)' "$workdir/oc.err"; then
    echo "ERROR: prerequisite missing: the cluster Proxy object ($PROXY) does not exist on this cluster; this hook only applies to OpenShift clusters" >&2
  else
    cat "$workdir/oc.err" >&2
  fi
  exit 1
fi
REFERENCE=$(proxy_reference "$PROXY_FILE")
if [ -n "$REFERENCE" ] && [ "$REFERENCE" != "$BUNDLE_NAME" ]; then
  echo "ERROR: the cluster Proxy already references trust bundle '${REFERENCE}', which this chart did not configure — refusing to overwrite a foreign reference; point spec.trustedCA at '${BUNDLE_NAME}' or unset it manually, then re-run this hook" >&2
  exit 1
fi
WANT_OWNERSHIP=$([ -z "$REFERENCE" ] && echo true || echo false)
export WANT_OWNERSHIP

bundle_present=1
if ! oc get "$BUNDLE" -n "$BUNDLE_NAMESPACE" -o json \
      --request-timeout="$REQUEST_TIMEOUT" \
      >"$BUNDLE_FILE" 2>"$workdir/oc.err"; then
  if grep -q '(NotFound)' "$workdir/oc.err"; then
    bundle_present=0
  else
    cat "$workdir/oc.err" >&2
    exit 1
  fi
fi

# Adoption: the reference already points at this chart's bundle and the
# bundle holds exactly the current CA — recognized prior output; claim the
# reference ownership and change nothing else. Any other content is a
# foreign-managed bundle and takes the merge path without claiming.
if [ "$bundle_present" -eq 1 ] && [ "$WANT_OWNERSHIP" = false ]; then
  adoption=$(bundle_is_adoptable "$BUNDLE_FILE" "$CURRENT_BLOCK" || true)
  if [ "$adoption" = "adopt" ]; then
    write_rc=0
    conditional_write "$BUNDLE" compute_bundle_adopt \
      "reference ownership annotation" "${BUNDLE_NAME} ConfigMap" \
      "$BUNDLE_NAMESPACE" || write_rc=$?
    case $write_rc in
      0)
        echo "Adopted this chart's prior trust output: the cluster Proxy already references ${BUNDLE_NAME} with exactly the current CA certificate; recorded reference ownership"
        ;;
      3)
        echo "Reference ownership of ${BUNDLE_NAME} is already recorded; no changes needed"
        ;;
      2)
        echo "ERROR: the ${BUNDLE_NAME} ConfigMap disappeared mid-run; re-run this hook" >&2
        exit 1
        ;;
      *)
        exit 1
        ;;
    esac
    exit 0
  fi
fi

if [ "$bundle_present" -eq 0 ]; then
  create_rc=0
  bundle_manifest "$CURRENT_BLOCK" \
    | create_object "${BUNDLE_NAME} ConfigMap" -f - -n "$BUNDLE_NAMESPACE" \
    || create_rc=$?
  case $create_rc in
    0)
      echo "Created the ${BUNDLE_NAME} trust bundle with the current CA certificate"
      ;;
    3)
      echo "ERROR: the ${BUNDLE_NAME} ConfigMap was created concurrently; re-running this hook merges into it" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
else
  write_rc=0
  conditional_write "$BUNDLE" compute_bundle_set "trust bundle content" \
    "${BUNDLE_NAME} ConfigMap" "$BUNDLE_NAMESPACE" || write_rc=$?
  case $write_rc in
    0)
      echo "Updated the ${BUNDLE_NAME} trust bundle with the current CA certificate"
      ;;
    3)
      echo "The ${BUNDLE_NAME} trust bundle already carries this chart's entries; no changes needed"
      ;;
    2)
      echo "ERROR: the ${BUNDLE_NAME} ConfigMap disappeared mid-run; re-run this hook" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
fi

if [ "$WANT_OWNERSHIP" = true ]; then
  write_rc=0
  conditional_write "$PROXY" compute_proxy_ref "trustedCA reference" \
    "cluster Proxy" || write_rc=$?
  case $write_rc in
    0)
      echo "Set the cluster Proxy trustedCA reference to ${BUNDLE_NAME}"
      # Ownership is claimed only after the reference write succeeded: a
      # reference that turned out to be already set (rc 3) was set by a
      # third party mid-run and is never claimed
      owned_rc=0
      conditional_write "$BUNDLE" compute_bundle_adopt \
        "reference ownership annotation" "${BUNDLE_NAME} ConfigMap" \
        "$BUNDLE_NAMESPACE" || owned_rc=$?
      case $owned_rc in
        0|3)
          ;;
        2)
          echo "ERROR: the ${BUNDLE_NAME} ConfigMap disappeared mid-run; the cluster Proxy trustedCA reference is set but points at the missing bundle, which degrades cluster trust — re-running this hook recreates the bundle with the current CA certificate (a further run then records reference ownership); uninstalling before that leaves a dangling trustedCA reference" >&2
          exit 1
          ;;
        *)
          echo "ERROR: the reference ownership record could not be written on the ${BUNDLE_NAME} ConfigMap; the cluster Proxy trustedCA reference is set and the bundle contents are intact — a re-run records ownership only when the bundle holds exactly the current CA certificate at its start; otherwise uninstall leaves the reference in place, pointing at the surviving bundle contents" >&2
          exit 1
          ;;
      esac
      ;;
    3)
      echo "The cluster Proxy already references ${BUNDLE_NAME}; no changes needed"
      ;;
    2)
      echo "ERROR: the cluster Proxy object disappeared mid-run; re-run this hook" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
fi
