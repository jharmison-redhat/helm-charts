#!/bin/bash
# Walks back this chart's cluster-trust footprint as a Helm pre-delete hook:
# unsets the cluster Proxy's spec.trustedCA reference when this hook set it
# (recorded by the trust-ca-owns-proxy-reference annotation on the
# openshift-config/user-ca-bundle ConfigMap), removes the CA entries this
# chart installed from the bundle, and deletes the bundle ConfigMap only
# when nothing remains in it. Objects are user-owned cluster inputs that
# this release never owns blindly:
#
# - A Proxy reference this chart did not set (no ownership annotation) is
#   left in place.
# - Bundle identity is byte-equality, evaluated per PEM block: a block is
#   ours when it equals the Secret's current tls.crt or its base64 payload
#   is listed in the bundle's trust-ca-history annotation — replaced on
#   every set-hook run with that run's payload, so it identifies the block
#   the set hook most recently installed (payloads rather than hashes,
#   since jq has no hash function). Foreign blocks are never removed or
#   reordered.
# - When neither the Secret nor the history annotation identifies any block,
#   the bundle is left untouched rather than guessed at.
#
# Every write is a conditional read-modify-write carrying the live
# metadata.resourceVersion: a stale version is rejected with HTTP 409 and
# retried.
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

require_oc "cannot walk back the cluster trust integration"
select_json_tool "cannot manipulate the cluster trust bundle"

workdir=$(mktemp -d) || exit 1
trap 'rm -rf "$workdir"' EXIT
SECRET_FILE="$workdir/secret.json"
PROXY_FILE="$workdir/proxy.json"
BUNDLE_FILE="$workdir/bundle.json"

# Prints the value of annotation $2 from the bundle JSON in $1, or an empty
# string when it is absent.
bundle_annotation() {
  case "$JSON_TOOL" in
    jq)
      jq -r --arg key "$2" '.metadata.annotations[$key] // ""' "$1"
      ;;
    python3)
      python3 - "$1" "$2" <<'PYEOF'
import json, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
print((doc.get("metadata", {}).get("annotations") or {}).get(sys.argv[2]) or "")
PYEOF
      ;;
  esac
}

# Classifies the bundle JSON in $1 for teardown and prints a JSON verdict:
# {"action":"none","reason":...,"hist":N} when no block belongs to this
# chart, {"action":"delete"} when removing the owned blocks would empty the
# bundle, and {"action":"write","data":...} with the content that remains
# after removing them. Exits 4 after its own diagnostic on content that
# does not decompose into recognized PEM blocks plus whitespace; a
# certificate-free (whitespace-only) content merely classifies as holding
# nothing of this chart's.
classify_bundle() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg key "$BUNDLE_KEY" --arg cur "$CURRENT_BLOCK" \
            --arg hist_ann "$HISTORY_ANNOTATION" \
            --arg obj "the user-ca-bundle ConfigMap content" \
            --arg tail "inspect and fix the bundle manually" \
            --arg fatal_empty false \
            "$TRUST_PEM_JQ_DEFS"'
        pem_guard($obj; $tail; $fatal_empty) as $have
        | (try ((.metadata.annotations[$hist_ann] // "[]") | fromjson
                | (if type == "array" then . else [] end)) catch []) as $hist_list
        | ([(if $cur != "" then ($cur | @base64) else empty end)] + $hist_list) as $ours_b64
        | [$have[]
           | select((@base64) as $pb | $ours_b64 | index($pb) != null)] as $ours_list
        | if ($ours_list | length) == 0 then
            {action: "none",
             reason: (if ($have | length) == 0 then "empty" else "no-ours" end),
             hist: ($hist_list | length)}
          else
            [$have[]
             | select((@base64) as $pb | $ours_b64 | index($pb) == null)] as $remaining
            | if ($remaining | length) == 0 then {action: "delete"}
              else {action: "write", data: ($remaining | join(""))} end
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import base64, json, os, sys

exec(os.environ["TRUST_PEM_PY"])

with open(sys.argv[1]) as f:
    doc = json.load(f)
cur = os.environ.get("CURRENT_BLOCK", "")
hist_ann = os.environ["HISTORY_ANNOTATION"]
raw = (doc.get("data") or {}).get(os.environ["BUNDLE_KEY"]) or ""
try:
    blocks = pem_parse(raw)
except ValueError as err:
    if str(err) == "empty":
        # Whitespace-only content holds nothing of this chart's; there is
        # nothing to fail closed over
        blocks = []
    else:
        pem_fail("the user-ca-bundle ConfigMap content",
                 "inspect and fix the bundle manually", str(err))
try:
    hist_list = json.loads(
        (doc.get("metadata", {}).get("annotations") or {}).get(hist_ann) or "[]")
except (ValueError, TypeError):
    hist_list = []
if not isinstance(hist_list, list):
    hist_list = []

def ours(block):
    if cur and block == cur:
        return True
    return base64.b64encode(block.encode()).decode() in hist_list

ours_list = [b for b in blocks if ours(b)]
if not ours_list:
    print(json.dumps({"action": "none",
                      "reason": "empty" if not blocks else "no-ours",
                      "hist": len(hist_list)}))
else:
    remaining = [b for b in blocks if not ours(b)]
    if remaining:
        print(json.dumps({"action": "write", "data": "".join(remaining)}))
    else:
        print(json.dumps({"action": "delete"}))
PYEOF
      ;;
  esac
}

# Prints the value of field $1 from the verdict JSON read from stdin, or an
# empty string when it is absent.
verdict_field() {
  case "$JSON_TOOL" in
    jq)
      jq -r --arg key "$1" '.[$key] // ""'
      ;;
    python3)
      # The verdict arrives on stdin, so the code must come from -c rather
      # than a heredoc, which would consume the pipe
      python3 -c 'import json, sys

verdict = json.load(sys.stdin)
print(verdict.get(sys.argv[1], ""))' "$1"
      ;;
  esac
}

# Computes the bundle merge patch from the live bundle JSON in $1: the
# remaining content after this chart's blocks are removed, with both trust
# annotations cleared. Emits nothing when the fresh classification finds
# nothing of this chart's left; when a concurrent modification emptied the
# remainder after the initial read, nulls the content key instead of
# deleting (conditional_write patches only) and leaves the ConfigMap in
# place.
compute_bundle_remove() {
  local verdict_rc=0 verdict action data
  verdict=$(classify_bundle "$1") || verdict_rc=$?
  if [ "$verdict_rc" -ne 0 ]; then
    return 4
  fi
  action=$(printf '%s' "$verdict" | verdict_field "action")
  case "$action" in
    none)
      return 0
      ;;
    delete)
      data=""
      ;;
    write)
      data=$(printf '%s' "$verdict" | verdict_field "data")
      # Command substitution strips the trailing newline of the joined
      # blocks; restore it so the written content is byte-identical to what
      # the blocks carried
      [ -n "$data" ] && printf -v data '%s\n' "$data"
      ;;
    *)
      echo "ERROR: unexpected trust bundle classification verdict" >&2
      return 4
      ;;
  esac
  case "$JSON_TOOL" in
    jq)
      jq -c --arg key "$BUNDLE_KEY" --arg hist_ann "$HISTORY_ANNOTATION" \
            --arg own_ann "$OWNERSHIP_ANNOTATION" --arg data "$data" '
        {metadata: {resourceVersion: .metadata.resourceVersion,
                    annotations: {($hist_ann): null, ($own_ann): null}},
         data: {($key): (if $data == "" then null else $data end)}}' "$1"
      ;;
    python3)
      python3 - "$1" "$data" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
data = sys.argv[2]
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"],
                 "annotations": {os.environ["HISTORY_ANNOTATION"]: None,
                                 os.environ["OWNERSHIP_ANNOTATION"]: None}},
    "data": {os.environ["BUNDLE_KEY"]: data or None},
}))
PYEOF
      ;;
  esac
}

# Computes the trustedCA unset patch from the live Proxy JSON in $1; emits
# nothing when the fresh reference no longer points at this chart's bundle.
compute_proxy_unset() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg name "$BUNDLE_NAME" '
        if ((.spec.trustedCA.name // "") != $name) then empty
        else {metadata: {resourceVersion: .metadata.resourceVersion},
              spec: {trustedCA: null}}
        end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(sys.argv[1]) as f:
    doc = json.load(f)
name = os.environ["BUNDLE_NAME"]
ref = (((doc.get("spec") or {}).get("trustedCA") or {}).get("name")) or ""
if ref != name:
    sys.exit(0)
print(json.dumps({
    "metadata": {"resourceVersion": doc["metadata"]["resourceVersion"]},
    "spec": {"trustedCA": None},
}))
PYEOF
      ;;
  esac
}

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

proxy_present=1
if ! oc get "$PROXY" -o json --request-timeout="$REQUEST_TIMEOUT" \
      >"$PROXY_FILE" 2>"$workdir/oc.err"; then
  if grep -q '(NotFound)' "$workdir/oc.err"; then
    proxy_present=0
  else
    cat "$workdir/oc.err" >&2
    exit 1
  fi
fi

# Unset the Proxy reference first: unsetting reverts the cluster to
# system-trust-only cleanly, while removing bundle content underneath a live
# reference would leave a degraded Proxy condition behind.
if [ "$proxy_present" -eq 1 ]; then
  reference=$(proxy_reference "$PROXY_FILE")
  if [ "$reference" = "$BUNDLE_NAME" ]; then
    owned=""
    if [ "$bundle_present" -eq 1 ]; then
      owned=$(bundle_annotation "$BUNDLE_FILE" "$OWNERSHIP_ANNOTATION")
    fi
    if [ "$owned" = "true" ]; then
      write_rc=0
      conditional_write "$PROXY" compute_proxy_unset "trustedCA reference" \
        "cluster Proxy" || write_rc=$?
      case $write_rc in
        0)
          echo "Unset the cluster Proxy trustedCA reference to ${BUNDLE_NAME}"
          ;;
        3)
          echo "The cluster Proxy no longer references ${BUNDLE_NAME}; nothing to unset"
          ;;
        2)
          echo "The cluster Proxy object is absent; nothing to unset"
          ;;
        *)
          exit 1
          ;;
      esac
    else
      echo "The cluster Proxy references ${BUNDLE_NAME} but the reference-ownership annotation is absent — the reference was set outside this chart; leaving it in place"
    fi
  elif [ -n "$reference" ]; then
    echo "The cluster Proxy references trust bundle ${reference}; nothing to unset"
  fi
fi

# The Secret's current certificate identifies our blocks by byte-equality;
# when it cannot be read (or carries none), the history annotation alone
# identifies them.
SECRET_IDENTIFIES=0
CURRENT_BLOCK=""
if oc get "$SECRET" -n "$CA_NAMESPACE" -o json \
      --request-timeout="$REQUEST_TIMEOUT" \
      >"$SECRET_FILE" 2>/dev/null; then
  extracted=$(secret_cert_block "$SECRET_FILE") || extracted=""
  # Command substitution strips the canonical block's trailing newline;
  # restore it so byte-equality against bundle blocks holds
  if [ -n "$extracted" ]; then
    printf -v CURRENT_BLOCK '%s\n' "$extracted"
    SECRET_IDENTIFIES=1
  fi
fi
export CURRENT_BLOCK

if [ "$bundle_present" -eq 0 ]; then
  echo "The ${BUNDLE_NAME} trust bundle is absent; nothing to remove"
  exit 0
fi

classify_rc=0
verdict=$(classify_bundle "$BUNDLE_FILE") || classify_rc=$?
case $classify_rc in
  0) ;;
  4) exit 1 ;;
  *)
    echo "ERROR: failed to classify the ${BUNDLE_NAME} trust bundle content" >&2
    exit 1
    ;;
esac
action=$(printf '%s' "$verdict" | verdict_field "action")
case "$action" in
  none)
    reason=$(printf '%s' "$verdict" | verdict_field "reason")
    hist_count=$(printf '%s' "$verdict" | verdict_field "hist")
    if [ "$reason" = "no-ours" ] && [ "$SECRET_IDENTIFIES" -eq 0 ] \
        && [ "$hist_count" = "0" ]; then
      echo "ERROR: cannot identify which entries in the ${BUNDLE_NAME} trust bundle belong to this chart: the cert-manager-ca Secret is unreadable or carries no certificate, and the trust-ca-history annotation records no entries — leaving the bundle untouched rather than guessing" >&2
      exit 1
    fi
    echo "No entries belonging to this chart were found in the ${BUNDLE_NAME} trust bundle; leaving it untouched"
    ;;
  delete)
    delete_rc=0
    delete_object "${BUNDLE_NAME} ConfigMap" "$BUNDLE" -n "$BUNDLE_NAMESPACE" \
      || delete_rc=$?
    case $delete_rc in
      0)
        echo "Removed this chart's entries from the ${BUNDLE_NAME} trust bundle; deleted the emptied ConfigMap"
        ;;
      3)
        echo "The ${BUNDLE_NAME} ConfigMap is already absent; nothing to remove"
        ;;
      *)
        exit 1
        ;;
    esac
    ;;
  write)
    write_rc=0
    conditional_write "$BUNDLE" compute_bundle_remove "trust bundle content" \
      "${BUNDLE_NAME} ConfigMap" "$BUNDLE_NAMESPACE" || write_rc=$?
    case $write_rc in
      0)
        echo "Removed this chart's entries from the ${BUNDLE_NAME} trust bundle"
        ;;
      3)
        echo "The ${BUNDLE_NAME} trust bundle already contains none of this chart's entries; no changes needed"
        ;;
      2)
        echo "The ${BUNDLE_NAME} ConfigMap is absent; nothing to remove"
        ;;
      *)
        exit 1
        ;;
    esac
    ;;
  *)
    echo "ERROR: unexpected trust bundle classification verdict" >&2
    exit 1
    ;;
esac
