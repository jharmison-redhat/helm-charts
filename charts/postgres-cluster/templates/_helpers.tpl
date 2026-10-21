{{/*
Expand the name of the chart.
*/}}
{{- define "postgres-cluster.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "postgres-cluster.fullname" -}}
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
{{- define "postgres-cluster.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "postgres-cluster.labels" -}}
helm.sh/chart: {{ include "postgres-cluster.chart" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Component enabled gate: renders "true" unless `enabled` is explicitly false.
Callers must compare with `eq (include ...) "true"` — an `if` on the string
output directly would treat "false" as truthy.
*/}}
{{- define "postgres-cluster.enabled" -}}
{{- printf "%v" (or (not (hasKey . "enabled")) .enabled) -}}
{{- end -}}

{{/*
Name of the TLS Secret holding the server certificate.
*/}}
{{- define "postgres-cluster.certificate.secret.fullname" -}}
{{- include "postgres-cluster.fullname" . }}-certificate
{{- end -}}

{{/*
DNS names covered by the server certificate: the `<cluster>-<service>`
short name plus namespace, `.svc`, and `.<clusterDomain>`-suffixed variants
for each service in `services`.
*/}}
{{- define "postgres-cluster.certificate.dnsNames" -}}
{{- $ns := .Values.namespace | default .Release.Namespace -}}
{{- $fullname := include "postgres-cluster.fullname" . -}}
{{- $clusterDomain := .Values.certificate.clusterDomain -}}
{{- range $svc := .Values.services -}}
{{- printf "\n- %s-%s" $fullname $svc -}}
{{- printf "\n- %s-%s.%s" $fullname $svc $ns -}}
{{- printf "\n- %s-%s.%s.svc" $fullname $svc $ns -}}
{{- if $clusterDomain -}}
{{- printf "\n- %s-%s.%s.svc.%s" $fullname $svc $ns $clusterDomain -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Services among the CNPG defaults (rw/ro/r) that were removed from
`services`, as a JSON list for `managed.services.disabledDefaultServices`.
*/}}
{{- define "postgres-cluster.disabledServices" -}}
{{- $diff := list -}}
{{- $services := .Values.services -}}
{{- range $item := list "rw" "ro" "r" -}}
  {{- if not (has $item $services) -}}
    {{- $diff = append $diff $item -}}
  {{- end -}}
{{- end -}}
{{- $diff | toJson -}}
{{- end -}}
