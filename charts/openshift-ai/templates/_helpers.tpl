{{/*
Evaluates whether the aigateway modelsAsAService component is Managed. Safe
against an unset dataScienceCluster/components tree — evaluates false instead
of nil-pointer.
*/}}
{{- define "openshift-ai.maasManaged" -}}
{{- $dsc := .Values.dataScienceCluster | default dict -}}
{{- $comps := dig "components" dict $dsc -}}
{{- $aigateway := dig "aigateway" dict $comps -}}
{{- $maas := dig "modelsAsAService" dict $aigateway -}}
{{- eq (dig "managementState" "" $maas) "Managed" -}}
{{- end -}}

{{/*
Full name of the postgres-cluster dependency's Cluster, replicating the
postgres-cluster chart's fullname derivation against the dependency's values
(subchart templates cannot be invoked with a subchart context). The default
bootstrap creates the `app` database with credentials in a `<fullname>-app`
Secret, which the maas-db bootstrap job reads in managed mode.
*/}}
{{- define "openshift-ai.pg.fullname" -}}
{{- $pg := index .Values "postgres" | default dict -}}
{{- if $pg.fullnameOverride -}}
{{- $pg.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default "postgres-cluster" $pg.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}
