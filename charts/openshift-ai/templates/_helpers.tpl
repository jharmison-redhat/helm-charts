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
Chart labels for the MaasTenantConfig hook resources.
*/}}
{{- define "openshift-ai.tenantConfig.labels" -}}
app.kubernetes.io/name: openshift-ai
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
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
Resolves a gateway listener hostname from a dict context with root (top-level
chart context), gateway (the gateway values map) and subdomain ("maas" or
"inference"). A nonempty gateway hostname override is returned verbatim
(untrimmed, unvalidated); nil, empty, false or zero fall back to
<subdomain>.apps.<global.cluster.name>.<global.cluster.baseDomain>.
*/}}
{{- define "openshift-ai.gateway.hostname" -}}
{{- .gateway.hostname | default (printf "%s.apps.%s.%s" .subdomain .root.Values.global.cluster.name .root.Values.global.cluster.baseDomain) -}}
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
Release-qualified name of the MaasTenantConfig hook's ClusterRole /
ClusterRoleBinding / Job. Collision-resistant across release namespaces: the
name carries a stable hash of the release namespace and name, so the same
release name in two namespaces yields distinct cluster-scoped resources.
Kubernetes-valid at 63 characters: for long release names the tail is
truncated and the hash preserves the identity.
*/}}
{{- define "openshift-ai.tenantConfig.fullname" -}}
{{- $base := printf "%s-maas-tenant-config" .Release.Name -}}
{{- $hash := sha256sum (printf "%s/%s" .Release.Namespace .Release.Name) | trunc 8 -}}
{{- printf "%s-%s" (trimSuffix "-" ($base | trunc 54)) $hash -}}
{{- end -}}

{{/*
Validates the maasTenantConfig block for hook rendering: spec must be a
non-empty map (explicit user-supplied merge-patch content — nil, scalars and
lists are rejected rather than silently no-op'ed or wiping the spec), and the
timing values must be positive integers.
*/}}
{{- define "openshift-ai.tenantConfig.validated" -}}
{{- $cfg := .Values.maasTenantConfig -}}
{{- $spec := dig "spec" nil $cfg -}}
{{- if not (kindIs "map" $spec) -}}
{{- fail (printf "maasTenantConfig.spec must be a map (got %s) — an empty or non-map spec cannot configure the default-tenant object" (kindOf $spec)) -}}
{{- end -}}
{{- if eq (len $spec) 0 -}}
{{- fail "maasTenantConfig.spec must be a non-empty map when enabled — enabling without values cannot configure the default-tenant object" -}}
{{- end -}}
{{- range $k := list "waitTimeoutSeconds" "pollIntervalSeconds" -}}
{{- $v := dig $k nil $cfg -}}
{{- $n := int $v -}}
{{- if not (or (kindIs "int" $v) (and (kindIs "float64" $v) (eq $v (mulf (floor $v) 1.0)))) -}}
{{- fail (printf "maasTenantConfig.%s must be an integer (got %s)" $k (kindOf $v)) -}}
{{- end -}}
{{- if le $n 0 -}}
{{- fail (printf "maasTenantConfig.%s must be positive (got %v)" $k $v) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
Validates a positive-integral wait timeout in seconds, suitable for a
Kubernetes activeDeadlineSeconds (int32). Rejects nulls, bools, strings,
fractional values and values exceeding the int32 range; returns the validated
integer. Shared by the bootstrap Jobs' wait values.
*/}}
{{- define "openshift-ai.waitSeconds.validated" -}}
{{- $field := .field -}}
{{- $v := .value -}}
{{- if kindIs "invalid" $v -}}
{{- fail (printf "%s is required — a missing wait timeout cannot bound the job" $field) -}}
{{- end -}}
{{- if kindIs "bool" $v -}}
{{- fail (printf "%s must be an integer (got bool)" $field) -}}
{{- end -}}
{{- if not (or (kindIs "int" $v) (and (kindIs "float64" $v) (eq $v (mulf (floor $v) 1.0)))) -}}
{{- fail (printf "%s must be an integer (got %s)" $field (kindOf $v)) -}}
{{- end -}}
{{- $n := int $v -}}
{{- if le $n 0 -}}
{{- fail (printf "%s must be positive (got %v)" $field $v) -}}
{{- end -}}
{{- if gt $n 2147483647 -}}
{{- fail (printf "%s exceeds the int32 range (got %v)" $field $v) -}}
{{- end -}}
{{- $n -}}
{{- end -}}

{{/*
Release-qualified name of the replica-management surface's resources
(ClusterRole / ClusterRoleBinding / ServiceAccount / ConfigMap / hook Jobs /
CronJob). Collision-resistant across release namespaces, mirroring the
MaasTenantConfig hook naming.
*/}}
{{- define "openshift-ai.replicaSurface.fullname" -}}
{{- $base := printf "%s-replicas" .Release.Name -}}
{{- $hash := sha256sum (printf "%s/%s" .Release.Namespace .Release.Name) | trunc 8 -}}
{{- printf "%s-%s" (trimSuffix "-" ($base | trunc 54)) $hash -}}
{{- end -}}

{{/*
Chart labels for the replica-management resources.
*/}}
{{- define "openshift-ai.replicaSurface.labels" -}}
app.kubernetes.io/name: openshift-ai
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Resolves the replica-management surface's gate: it renders when at least one
component is managed (a non-null count), OR when asCronJob is not a boolean
or is explicitly false — either enters the templates so the validation can
fail loudly on the invalid or nothing-to-ship composition instead of
silently rendering nothing.
*/}}
{{- define "openshift-ai.replicaSurface.enabled" -}}
{{- $r := .Values.replicas | default dict -}}
{{- $managed := false -}}
{{- range $c := list "dashboard" "operator" -}}
{{- if not (kindIs "invalid" (dig $c nil $r)) -}}{{- $managed = true -}}{{- end -}}
{{- end -}}
{{- $asCronJob := dig "asCronJob" true $r -}}
{{- if or $managed (not (kindIs "bool" $asCronJob)) (eq $asCronJob false) -}}true{{- end -}}
{{- end -}}

{{/*
Validates the replicas block: managed counts must be nonnegative integers
(explicit 0 is managed — presence-aware, not falsey), asCronJob must be a
boolean, the schedule must be a non-empty string when the CronJob mode is
selected, the wait timeout must bound a Job deadline, and the partial-manifests
mode must have something to ship.
*/}}
{{- define "openshift-ai.replicaSurface.config.validated" -}}
{{- $r := .Values.replicas | default dict -}}
{{- if not (kindIs "map" $r) -}}
{{- fail (printf "replicas must be a map (got %s)" (kindOf $r)) -}}
{{- end -}}
{{- $asCronJob := dig "asCronJob" true $r -}}
{{- if not (kindIs "bool" $asCronJob) -}}
{{- fail (printf "replicas.asCronJob must be a boolean (got %s)" (kindOf $asCronJob)) -}}
{{- end -}}
{{- $managed := false -}}
{{- range $c := list "dashboard" "operator" -}}
{{- $v := dig $c nil $r -}}
{{- if not (kindIs "invalid" $v) -}}
{{- $managed = true -}}
{{- if not (or (kindIs "int" $v) (kindIs "int64" $v) (and (kindIs "float64" $v) (eq $v (mulf (floor $v) 1.0)))) -}}
{{- fail (printf "replicas.%s must be an integer or null (got %s)" $c (kindOf $v)) -}}
{{- end -}}
{{- if lt (int $v) 0 -}}
{{- fail (printf "replicas.%s must be nonnegative (got %v)" $c $v) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $asCronJob -}}
{{- $sched := dig "schedule" "*/15 * * * *" $r -}}
{{- if not (kindIs "string" $sched) -}}
{{- fail (printf "replicas.schedule must be a cron schedule string (got %s)" (kindOf $sched)) -}}
{{- end -}}
{{- if eq (trim $sched) "" -}}
{{- fail "replicas.schedule must be a non-empty cron schedule string" -}}
{{- end -}}
{{- end -}}
{{- $wait := include "openshift-ai.waitSeconds.validated" (dict "field" "replicas.waitTimeoutSeconds" "value" (dig "waitTimeoutSeconds" 240 $r)) -}}
{{- if and (not $asCronJob) (not $managed) -}}
{{- fail "replicas.asCronJob: false with no managed components has nothing to ship — set replicas.dashboard or replicas.operator to a count, or remove the replicas.asCronJob override to use the CronJob drift-corrector" -}}
{{- end -}}
{{- end -}}

{{/*
The managed-component lines of components.txt ("<namespace> <name> <count>"),
newline-separated without a trailing newline. The component→Deployment
mapping is fixed: the rhods-dashboard lives in the product applications
namespace and the rhods-operator in the operator namespace.
*/}}
{{- define "openshift-ai.replicaSurface.components" -}}
{{- $r := .Values.replicas | default dict -}}
{{- $targets := dict
      "dashboard" "redhat-ods-applications rhods-dashboard"
      "operator" "redhat-ods-operator rhods-operator" -}}
{{- $lines := list -}}
{{- range $c := list "dashboard" "operator" -}}
{{- $v := dig $c nil $r -}}
{{- if not (kindIs "invalid" $v) -}}
{{- $lines = append $lines (printf "%s %v" (get $targets $c) $v) -}}
{{- end -}}
{{- end -}}
{{- join "\n" $lines -}}
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
