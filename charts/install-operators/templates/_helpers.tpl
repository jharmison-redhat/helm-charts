{{/*
Expand the name of the chart.
*/}}
{{- define "install-operators.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "install-operators.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "install-operators.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "install-operators.labels" -}}
helm.sh/chart: {{ include "install-operators.chart" . }}
{{ include "install-operators.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
The chart's provenance labels as a JSON object, for the hook payload
manifests (which are built as JSON, not YAML). Derived from the common
labels so the label set is defined once.
*/}}
{{- define "install-operators.chartLabels" -}}
{{- include "install-operators.labels" . | fromYaml | toJson }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "install-operators.selectorLabels" -}}
app.kubernetes.io/name: {{ include "install-operators.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Per-operator enabled gate: renders "true" unless `enabled` is explicitly false.
Callers must compare with `eq (include ...) "true"` — an `if` on the string
output directly would treat "false" as truthy.
*/}}
{{- define "install-operators.operatorEnabled" -}}
{{- printf "%v" (or (not (hasKey . "enabled")) .enabled) -}}
{{- end -}}

{{/*
The namespace an operator entry installs into.
*/}}
{{- define "install-operators.operatorNamespace" -}}
{{- .namespace | default "openshift-operators" -}}
{{- end -}}

{{/*
The managed operator namespaces: the unique set of explicitly-configured
`namespace` values across enabled operator entries. Two entries sharing a
namespace must declare identical values for every shared
namespaceLabels/namespaceAnnotations key — a conflict fails the render naming
both entries and the key. Returns a JSON array; each entry carries the
namespace name, the chart's provenance labels, and the configured labels and
annotations for the ensure-operator-namespaces hook. The shared
openshift-operators default is deliberately not in this set: it pre-exists on
every OpenShift cluster and is never created, labeled, or deleted here.
*/}}
{{- define "install-operators.managedNamespaces" -}}
{{- $chartLabels := fromJson (include "install-operators.chartLabels" .) }}
{{- $namespaces := dict }}
{{- range $operator, $config := .Values.operators }}
{{- if eq (include "install-operators.operatorEnabled" $config) "true" }}
{{- if $config.namespace }}
{{- if not (hasKey $namespaces $config.namespace) }}
{{- $_ := set $namespaces $config.namespace (dict
    "name" $config.namespace
    "operators" (list $operator)
    "namespaceLabels" (dict)
    "namespaceAnnotations" (dict)) }}
{{- end }}
{{- $entry := get $namespaces $config.namespace }}
{{- if not (has $operator (get $entry "operators")) }}
{{- $_ := set $entry "operators" (append (get $entry "operators") $operator) }}
{{- end }}
{{- range $surface, $want := dict
    "namespaceLabels" ($config.namespaceLabels | default dict)
    "namespaceAnnotations" ($config.namespaceAnnotations | default dict) }}
{{- range $key, $value := $want }}
{{- $haveMap := get $entry $surface }}
{{- if hasKey $haveMap $key }}
{{- if ne (get $haveMap $key) $value }}
{{- fail (printf "operators %q and %q share namespace %q but declare conflicting %s for key %q (%q vs %q) — give the shared namespace identical metadata or use different namespaces" (first (get $entry "operators")) $operator $config.namespace $surface $key (get $haveMap $key) $value) }}
{{- end }}
{{- else }}
{{- $_ := set $haveMap $key $value }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- $list := list }}
{{- range $name := keys $namespaces | sortAlpha }}
{{- $entry := get $namespaces $name }}
{{- $list = append $list (dict
    "name" $entry.name
    "chartLabels" $chartLabels
    "labels" $entry.namespaceLabels
    "annotations" $entry.namespaceAnnotations) }}
{{- end }}
{{- $list | toJson }}
{{- end -}}

{{/*
The hook payload for one operator's hook-managed Subscription: the target
name and namespace, the full managed spec (unconfigured optional fields
converge to null, removing only that key), and the create manifest used when
the Subscription is absent. Rendered as JSON for the hook ConfigMap.
*/}}
{{- define "install-operators.subscriptionPayload" -}}
{{- $operator := .operator }}
{{- $config := .config }}
{{- $root := .root }}
{{- $namespace := include "install-operators.operatorNamespace" $config }}
{{- $spec := dict
    "channel" $config.channel
    "installPlanApproval" ($config.installPlanApproval | default "Automatic")
    "name" $operator
    "source" ($config.catalog | default "redhat-operators")
    "sourceNamespace" "openshift-marketplace"
    "startingCSV" ($config.startingCSV | default nil)
    "config" (dict "env" nil) }}
{{- $manifestSpec := omit $spec "startingCSV" "config" }}
{{- with $config.startingCSV }}
{{- $_ := set $manifestSpec "startingCSV" . }}
{{- end }}
{{- if $config.env }}
{{- $envList := list }}
{{- range $k, $v := $config.env }}
{{- $envList = append $envList (dict "name" $k "value" $v) }}
{{- end }}
{{- $_ := set $spec "config" (dict "env" $envList) }}
{{- $_ := set $manifestSpec "config" (dict "env" $envList) }}
{{- end }}
{{- dict
    "name" $operator
    "namespace" $namespace
    "spec" $spec
    "manifest" (dict
      "apiVersion" "operators.coreos.com/v1alpha1"
      "kind" "Subscription"
      "metadata" (dict
        "name" $operator
        "namespace" $namespace
        "labels" (fromJson (include "install-operators.chartLabels" $root))
        "annotations" (dict "install-operators/proof" "true"))
      "spec" $manifestSpec) | toJson }}
{{- end -}}

{{/*
Subscription manifest for ownSubscription: true — derived from the hook
payload so the Subscription spec is defined once (the payload's create
manifest already omits the unconfigured optional fields, matching the
release-owned shape).
*/}}
{{- define "install-operators.subscription" -}}
{{- $payload := fromJson (include "install-operators.subscriptionPayload" .) }}
apiVersion: operators.coreos.com/v1alpha1
kind: Subscription
metadata:
  name: {{ $payload.name }}
  namespace: {{ $payload.namespace }}
  labels:
    {{- include "install-operators.labels" .root | nindent 4 }}
spec:
{{- $payload.manifest.spec | toYaml | nindent 2 }}
{{- end }}
