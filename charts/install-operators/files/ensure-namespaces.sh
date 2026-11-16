#!/bin/bash
# Ensures the namespaces this chart's operators install into exist, as a Helm
# pre-install/pre-upgrade hook, so the namespaces exist before Subscriptions,
# OperatorGroups, and their RBAC apply. The hook runs over the unique
# namespace set declared by the enabled operator entries (each entry's
# `namespace` value); a namespace absent from that set — including the shared
# openshift-operators default — is never touched by this hook.
#
# Contract per namespace:
# - absent → created with the chart's provenance labels, the entry's
#   configured labels/annotations, and a proof annotation recording that this
#   chart created it
# - present → additive merge of ONLY the entry's configured label/annotation
#   keys; any other key on the namespace is foreign and is never touched. A
#   foreign value on a configured key (or a proof annotation this chart did
#   not write) STOPs the hook naming the namespace and key. A present
#   namespace carrying this release's provenance labels
#   (app.kubernetes.io/name + app.kubernetes.io/instance) is additionally
#   stamped `helm.sh/resource-policy: keep` in the same write: on an upgrade
#   from a chart version that shipped Namespaces as release manifests, this
#   runs before Helm's upgrade deletion pass and stops the release from
#   deleting the namespace (and cascading its contents). A namespace this
#   hook creates is never release-tracked and does not need the stamp
# - uninstall never deletes namespaces — they persist after the release is
#   removed, whatever this hook created into them
#
# Creation and merge are conditional writes: creation fails closed on an
# AlreadyExists race (the hook then merges into the winner), and the merge is
# a read-modify-write carrying the live metadata.resourceVersion — a stale
# version is rejected with HTTP 409 and retried.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"

NAMESPACE_LIST="${NAMESPACE_LIST:-/mnt/namespaces.json}"
PROOF_ANNOTATION="install-operators/proof"
KEEP_ANNOTATION="helm.sh/resource-policy"
ENTRY_FILE="$(mktemp)"

require_oc "cannot ensure the operator namespaces"
select_json_tool "cannot inspect the operator namespace list"

export PROOF_ANNOTATION KEEP_ANNOTATION NAMESPACE_LIST ENTRY_FILE
trap 'rm -f "$ENTRY_FILE"' EXIT
if [ ! -f "$NAMESPACE_LIST" ]; then
  echo "ERROR: the operator namespace list is missing at ${NAMESPACE_LIST}; the hook ConfigMap delivery is broken" >&2
  exit 1
fi

# Prints the JSON entry at index $1 of the namespace list.
print_entry() {
  case "$JSON_TOOL" in
    jq)
      jq -c --argjson index "$1" '.[$index]' "$NAMESPACE_LIST"
      ;;
    python3)
      python3 -c 'import json, os, sys

entries = json.load(open(os.environ["NAMESPACE_LIST"]))
print(json.dumps(entries[int(sys.argv[1])]))' "$1"
      ;;
  esac
}

# Prints the number of entries in the namespace list.
entry_count() {
  case "$JSON_TOOL" in
    jq)
      jq 'length' "$NAMESPACE_LIST"
      ;;
    python3)
      python3 -c 'import json, os

print(len(json.load(open(os.environ["NAMESPACE_LIST"]))))'
      ;;
  esac
}

# Prints the name of the namespace entry JSON in $1.
entry_name() {
  case "$JSON_TOOL" in
    jq)
      jq -r '.name' "$1"
      ;;
    python3)
      python3 -c 'import json, sys

print(json.load(open(sys.argv[1]))["name"])' "$1"
      ;;
  esac
}

# Prints the create manifest for the namespace entry in $ENTRY_FILE:
# apiVersion/kind, the entry's name, the chart's provenance labels plus the
# entry's configured labels, and the entry's configured annotations plus the
# creation-proof annotation.
print_create_manifest() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg ann "$PROOF_ANNOTATION" '
        {apiVersion: "v1",
         kind: "Namespace",
         metadata:
           {name: .name,
            labels: (.chartLabels + .labels),
            annotations: ((.annotations // {}) + {($ann): "true"})}}' "$ENTRY_FILE"
      ;;
    python3)
      python3 -c 'import json, os

entry = json.load(open(os.environ["ENTRY_FILE"]))
annotations = entry.get("annotations") or {}
metadata = {"name": entry["name"],
            "labels": {**entry["chartLabels"], **entry["labels"]},
            "annotations": {**annotations,
                            os.environ["PROOF_ANNOTATION"]: "true"}}
print(json.dumps({"apiVersion": "v1", "kind": "Namespace",
                  "metadata": metadata}))'
      ;;
  esac
}

# Computes the merge patch for the live Namespace JSON in $1 against the
# entry in $ENTRY_FILE: only the entry's configured label/annotation keys are
# managed — a foreign value on one of them STOPs the hook naming the
# namespace and key, as does a proof annotation this chart did not write.
# When the namespace carries this release's provenance labels, the
# helm.sh/resource-policy: keep annotation is stamped in the same patch
# (without the foreign-STOP check: it is a Helm-reserved retention marker
# this hook owns on merge-path namespaces, not a user-configured key).
# Emits nothing when every managed key already matches; otherwise emits the
# merge patch carrying the live resourceVersion and the absent-or-differing
# managed keys.
compute_merge_patch() {
  case "$JSON_TOOL" in
    jq)
      jq -c --arg ann "$PROOF_ANNOTATION" --arg keep "$KEEP_ANNOTATION" \
        --slurpfile entry "$ENTRY_FILE" '
        . as $doc
        | ($entry[0]) as $want
        | ($doc.metadata.labels // {}) as $haveLabels
        | ($doc.metadata.annotations // {}) as $haveAnns
        | ([$want.labels | to_entries[]
            | select(($haveLabels[.key] // null) != null)
            | select(.value != $haveLabels[.key])
            | {key: .key, have: $haveLabels[.key], want: .value}] | .[0]) as $labelClash
        | ([$want.annotations | to_entries[]
            | select(($haveAnns[.key] // null) != null)
            | select(.value != $haveAnns[.key])
            | {key: .key, have: $haveAnns[.key], want: .value}] | .[0]) as $annClash
        | (if ($haveAnns[$ann] // null) != null and $haveAnns[$ann] != "true"
           then {key: $ann, have: $haveAnns[$ann], want: "true"}
           else null end) as $proofClash
        | ([$labelClash, $annClash, $proofClash]
           | map(select(. != null)) | .[0]) as $clash
        | if $clash != null then
            ("foreign value on the namespace " + $doc.metadata.name + ": key "
             + $clash.key + " carries " + ($clash.have | tostring)
             + " but this chart manages it with " + ($clash.want | tostring)
             + " — refusing to modify a value this chart did not set")
            | halt_error(4)
          else . end
        | [$want.labels | to_entries[]
           | select(.value != ($haveLabels[.key] // null))
           | {(.key): .value}] as $labelEntries
        | [$want.annotations | to_entries[]
           | select(.value != ($haveAnns[.key] // null))
           | {(.key): .value}] as $configuredAnnEntries
        | (if ($haveLabels["app.kubernetes.io/name"] // null)
              == ($want.chartLabels["app.kubernetes.io/name"] // null)
             and ($haveLabels["app.kubernetes.io/instance"] // null)
              == ($want.chartLabels["app.kubernetes.io/instance"] // null)
             and ($haveAnns[$keep] // null) != "keep"
           then [{($keep): "keep"}] else [] end) as $keepEntries
        | ($configuredAnnEntries + $keepEntries) as $annEntries
        | if ($labelEntries | length) == 0 and ($annEntries | length) == 0
          then empty
          else
            {metadata:
               ({resourceVersion: $doc.metadata.resourceVersion}
                + (if ($labelEntries | length) > 0
                   then {labels: ($labelEntries | add)} else {} end)
                + (if ($annEntries | length) > 0
                   then {annotations: ($annEntries | add)} else {} end))}
          end' "$1"
      ;;
    python3)
      python3 - "$1" <<'PYEOF'
import json, os, sys

with open(os.environ["ENTRY_FILE"]) as f:
    want = json.load(f)
with open(sys.argv[1]) as f:
    doc = json.load(f)
ann = os.environ["PROOF_ANNOTATION"]
keep = os.environ["KEEP_ANNOTATION"]
have_labels = doc.get("metadata", {}).get("labels") or {}
have_anns = doc.get("metadata", {}).get("annotations") or {}


def stop_foreign(key, have, want_value, kind):
    sys.stderr.write(
        "foreign value on the namespace " + doc["metadata"]["name"]
        + ": " + kind + " key " + key + " carries " + json.dumps(have)
        + " but this chart manages it with " + json.dumps(want_value)
        + " — refusing to modify a value this chart did not set\n")
    sys.exit(4)


for kind, want_map, have_map in (("label", want["labels"], have_labels),
                                 ("annotation",
                                  want.get("annotations") or {},
                                  have_anns)):
    for key, value in want_map.items():
        if key in have_map and have_map[key] != value:
            stop_foreign(key, have_map[key], value, kind)

proof = have_anns.get(ann)
if proof is not None and proof != "true":
    stop_foreign(ann, proof, "true", "annotation")

label_part = {k: v for k, v in want["labels"].items()
              if have_labels.get(k) != v}
ann_part = {k: v for k, v in (want.get("annotations") or {}).items()
            if have_anns.get(k) != v}
# A present namespace carrying this release's provenance labels gets the
# helm.sh/resource-policy: keep retention stamp in the same write — on an
# upgrade from a chart version that shipped Namespaces as release manifests,
# the hook runs before Helm's upgrade deletion pass and the stamp stops the
# release from deleting the namespace. Created namespaces are not
# release-tracked, so the create path never stamps.
chart_labels = want.get("chartLabels") or {}
provenance = (have_labels.get("app.kubernetes.io/name")
              == chart_labels.get("app.kubernetes.io/name")
              and have_labels.get("app.kubernetes.io/instance")
              == chart_labels.get("app.kubernetes.io/instance"))
if provenance and have_anns.get(keep) != "keep":
    ann_part[keep] = "keep"
if not label_part and not ann_part:
    sys.exit(0)
metadata = {"resourceVersion": doc["metadata"]["resourceVersion"]}
if label_part:
    metadata["labels"] = label_part
if ann_part:
    metadata["annotations"] = ann_part
print(json.dumps({"metadata": metadata}))
PYEOF
      ;;
  esac
}

# Merges this chart's managed keys into the namespace named by $1. Re-runs
# after a concurrent creation lost the race. Exits 1 when the namespace is
# still absent, which can only mean the earlier NotFound was spurious.
merge_namespace() {
  local name="$1" merge_rc=0
  conditional_write "namespace/$name" compute_merge_patch \
    "namespace metadata" "namespace $name" || merge_rc=$?
  case $merge_rc in
    0)
      echo "Merged this chart's managed metadata into namespace $name"
      ;;
    3)
      echo "Namespace $name already carries this chart's managed metadata; no changes needed"
      ;;
    2)
      echo "ERROR: namespace $name was reported absent twice; refusing to guess at cluster state" >&2
      exit 1
      ;;
    *)
      exit 1
      ;;
  esac
}

count=$(entry_count)
for ((index = 0; index < count; index++)); do
  print_entry "$index" >"$ENTRY_FILE"
  name=$(entry_name "$ENTRY_FILE")
  merge_rc=0
  conditional_write "namespace/$name" compute_merge_patch \
    "namespace metadata" "namespace $name" || merge_rc=$?
  case $merge_rc in
    0)
      echo "Merged this chart's managed metadata into namespace $name"
      ;;
    3)
      echo "Namespace $name already carries this chart's managed metadata; no changes needed"
      ;;
    2)
      create_rc=0
      print_create_manifest | create_object "namespace $name" -f - || create_rc=$?
      case $create_rc in
        0)
          echo "Created namespace $name with this chart's labels and the ${PROOF_ANNOTATION} creation-proof annotation"
          ;;
        3)
          merge_namespace "$name"
          ;;
        *)
          exit 1
          ;;
      esac
      ;;
    *)
      exit 1
      ;;
  esac
done
