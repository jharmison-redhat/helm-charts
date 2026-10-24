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
{{/*
Resolves a gateway's referenced GatewayClass name. The default lives solely
in values.yaml; an unset or empty class fails with a required message instead
of being silently defaulted or rendering an empty reference.
*/}}
{{- define "openshift-ai.gateway.className" -}}
{{- $class := .gateway.gatewayClassName -}}
{{- if kindIs "invalid" $class -}}
{{- fail "gatewayClassName is required — unset the gateway or provide a non-empty class name" -}}
{{- end -}}
{{- if not (kindIs "string" $class) -}}
{{- fail (printf "gatewayClassName must be a string (got %s)" (kindOf $class)) -}}
{{- end -}}
{{- if eq (trim $class) "" -}}
{{- fail "gatewayClassName must be a non-empty string — an empty value cannot reference a GatewayClass" -}}
{{- end -}}
{{- $class -}}
{{- end -}}

{{/*
Resolves a gateway's routeNamespaceSelector and fails rendering when it is
absent or empty — an empty LabelSelector would allow routes from every
namespace. Supported matchExpressions are preserved as-is.
*/}}
{{- define "openshift-ai.routeNamespaceSelector" -}}
{{- $sel := .gateway.routeNamespaceSelector -}}
{{- if kindIs "invalid" $sel -}}
{{- fail "routeNamespaceSelector is required (an unset selector would allow routes from all namespaces)" -}}
{{- end -}}
{{- if not (kindIs "map" $sel) -}}
{{- fail (printf "routeNamespaceSelector must be a LabelSelector map (got %s) — an empty or invalid selector allows routes from all namespaces" (kindOf $sel)) -}}
{{- end -}}
{{- $hasLabels := hasKey $sel "matchLabels" -}}
{{- $hasExprs := hasKey $sel "matchExpressions" -}}
{{- if $hasLabels -}}
{{- if not (kindIs "map" $sel.matchLabels) -}}
{{- fail (printf "routeNamespaceSelector.matchLabels must be a map (got %s)" (kindOf $sel.matchLabels)) -}}
{{- end -}}
{{- end -}}
{{- if $hasExprs -}}
{{- if not (kindIs "slice" $sel.matchExpressions) -}}
{{- fail (printf "routeNamespaceSelector.matchExpressions must be a list (got %s)" (kindOf $sel.matchExpressions)) -}}
{{- end -}}
{{- end -}}
{{- if and (or (not $hasLabels) (eq (len $sel.matchLabels) 0)) (or (not $hasExprs) (eq (len $sel.matchExpressions) 0)) -}}
{{- fail "routeNamespaceSelector must set matchLabels or matchExpressions (an empty selector allows routes from all namespaces)" -}}
{{- end -}}
{{- toYaml $sel -}}
{{- end -}}

{{/*
Validates a gateway's replicas override: null omits the patch field; explicit
values must be nonnegative integers.
*/}}
{{- define "openshift-ai.replicas.validated" -}}
{{- if not (eq (kindOf .value) "invalid") -}}
{{- $n := int .value -}}
{{- if not (or (kindIs "int" .value) (and (kindIs "float64" .value) (eq .value (mulf (floor .value) 1.0)))) -}}
{{- fail (printf "gateway replicas must be an integer or null (got %s)" (kindOf .value)) -}}
{{- end -}}
{{- if lt $n 0 -}}
{{- fail "gateway replicas must be nonnegative" -}}
{{- end -}}
{{- $n -}}
{{- end -}}
{{- end -}}
