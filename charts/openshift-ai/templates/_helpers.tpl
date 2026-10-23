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
