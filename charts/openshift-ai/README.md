openshift-ai
============

A chart to deploy OpenShift AI and manage `DataScienceCluster`/`DSCInitialization` configuration: the 14-component DSC
matrix (every component key the chart manages exists in the live `DataScienceCluster` v2 API; `aigateway` is the
RHOAI-downstream component that the upstream docs example omits), the dashboard and telemetry configuration, Gateway API
plumbing for MaaS and inference traffic, KEDA metrics-reader plumbing, MLflow/EvalHub operands, hardware profiles, and a
maas-db bootstrap Job.

The `rhods-operator` is NOT installed by this chart. It must already be installed before this chart is deployed — the
chart renders custom resources directly (`DataScienceCluster`, `OdhDashboardConfig`, and so on), and a plain
`helm install` fails on the missing CRDs otherwise. The source GitOps composition relied on ArgoCD
`SkipDryRunOnMissingResource` annotations to bridge this ordering; under a helm-native two-install flow the operator
install comes first instead. Use the `install-operators` chart for the operator install (see the
`examples/maas-umbrella` README for the full two-install flow):

```yaml
# install-operators values
operators:
  rhods-operator:
    channel: stable-3.5 # REQUIRED — the channel pin controls the RHOAI release line
    namespace: redhat-ods-operator
    operatorGroup:
      enabled: true
```

Wait for the operator deployments (and therefore the CRDs) to be ready before the first install of this chart.

Install with `-n redhat-ods-operator` — the operator namespace, which the `install-operators` chart ensures before this
chart deploys — or any pre-existing namespace that does not enforce restricted Pod Security Admission (the hook Jobs run
with a root-running tools image). Root-running hook Jobs also land in the `maasDb.namespace` namespace (the
`create-maas-db-config` hook) and in each tenant namespace (the evalhub tenant hooks); those namespaces must admit the
tools image as well.

Deploy one release of this chart per cluster: the `DataScienceCluster` and KEDA `ClusterTriggerAuthentication` are
cluster-scoped fixed names, and the Gateways and their ConfigMaps use fixed names that collide when two releases share a
namespace.

Usage
-----

The chart performs the following:

- Renders a `DataScienceCluster` (gated by `dataScienceCluster.create`) with `spec.components` passthrough over the full
  14-component matrix, and a `DSCInitialization` (gated by a non-empty `dataScienceClusterInitialization` block) with
  whole-block `spec` passthrough
- Renders `OdhDashboardConfig` + `Auth` (dashboard config incl. the 3.5 feature flags — `genAiStudio`, `modelAsService`,
  `disableLLMd`, `gpuaas`, and the rest; the passthrough accepts every dashboard flag), the segment-key and
  notebook-culler ConfigMaps, and the `hardwareprofile-view` RoleBinding
- Renders `HardwareProfile` resources (one per `hardwareProfiles` entry)
- Renders the `maas-default-gateway` plumbing (ConfigMap, Certificate/Route, optional EnvoyFilter, Gateway) — gated on
  the DSC's `aigateway.modelsAsAService` being `Managed` (evaluates false when the components tree is unset)
- Renders the `openshift-ai-inference` Gateway plumbing (gated by `gateways.openshiftAiInference.create`)
- Renders a MaasTenantConfig configuration hook (ServiceAccount, ClusterRole, ClusterRoleBinding, script ConfigMap in
  the release namespace and a `post-install,post-upgrade` hook Job) that waits for the product-created
  `MaasTenantConfig/default-tenant` singleton, then merge-patches only the `maasTenantConfig.spec` block — gated on
  `maasTenantConfig.enabled` AND the `modelsAsAService == Managed` gate
- Renders the payload-processing `NetworkPolicy` (gated by `maasPayloadProcessingNetworkPolicy.create`)
- Renders the KEDA metrics-reader plumbing: ServiceAccount, ClusterRoleBinding, token Secret, and
  `ClusterTriggerAuthentication` in `openshift-keda` (gated by `kedaMetricsReader.create`)
- Renders MLflow (gated by `mlflow.create`) and EvalHub (gated by `evalhub.create`) with `spec` passthrough, plus the
  evalhub-tenant subtree (gated by `evalhub.create` AND `evalhub.tenant.create`)
- Renders the maas-db bootstrap plumbing: ServiceAccount, RoleBinding, script ConfigMap, and the bootstrap Job in
  `maasDb.namespace` (gated by the `modelsAsAService == Managed` gate AND `maasDb.create` or `maasDb.secretName`)
- Optionally manages the replica counts of the rhods-operator-created `rhods-dashboard`/`rhods-operator` Deployments
  (gated by non-null `replicas.<component>` counts) through a conditional-write hook pair plus a durability layer — see
  the "Replica management" section below; the Deployments themselves are never chart-owned

The maas-db bootstrap Job runs as a `post-install,post-upgrade` Helm hook (recreated on re-runs via
`before-hook-creation`), so it re-runs on upgrades the way the source GitOps PostSync hook did.

The Job waits up to `maasDb.waitTimeoutSeconds` of wall-clock time for the source secret to appear, then fails without
creating or applying anything. Only a missing named secret is retried — Forbidden, unauthorized, and transport errors
fail immediately — and timeout diagnostics name both the namespace and the source secret. The Job reads the secret's
`uri` key, rejects a missing key, invalid base64, or empty/whitespace-only content before writing anything, and feeds
`DB_CONNECTION_URL` into the target `maas-db-config` Secret through `--from-file` so the connection URI never appears in
command arguments or diagnostics. This validates the payload only — it does not verify URI grammar or credential
validity. The Job has `backoffLimit: 3` and `activeDeadlineSeconds: waitTimeoutSeconds + 30`; like the tenant hook,
longer configured waits need a matching Helm `--timeout`.

The chart declares an OCI dependency on the published `postgres-cluster` chart of this repository
(`oci://ghcr.io/jharmison-redhat/helm-charts`), pinned `>=0.1.0 <0.2.0`, aliased to `postgres`, and condition-gated by
`maasDb.create`. With `maasDb.create: true`, the dependency deploys the Cluster and the bootstrap job derives its secret
name from the `openshift-ai.pg.fullname` convention (see `templates/_helpers.tpl`); with `maasDb.secretName` set
instead, the job runs out-of-band against an external Cluster.

Every Job running on the configured cluster-local `openshift/tools:latest` image (`global.toolsImage` — the maas-db
bootstrap, maas-tenant-config, and EvalHub credential Jobs) requires Bash plus OpenShift `oc` and the standard utilities
the scripts use (`base64`, `date`, `sleep`, `mktemp`). The EvalHub credential Job additionally requires
`oc set data --from-file`; the maas-db bootstrap Job requires `oc create --dry-run=client`/`apply` support.

Replica management
------------------

The chart can enforce the replica counts of the two rhods-operator-created Deployments — `rhods-dashboard` in
`redhat-ods-applications` and `rhods-operator` in `redhat-ods-operator` — via the `replicas` values block:

```yaml
replicas:
  dashboard: null # null hands off; an explicit count (including 0) is enforced
  operator: null
  asCronJob: true # true: CronJob drift-corrector (default); false: partial manifests (Argo path)
  schedule: "*/15 * * * *"
  waitTimeoutSeconds: 240
```

Set-once enforcement (conditional-write hook pair). With at least one non-null count, a `post-install,post-upgrade` hook
Job waits a bounded time (`replicas.waitTimeoutSeconds`, `activeDeadlineSeconds` = wait + 30) for each managed
Deployment — the Deployments are operator-created operands that do not exist until the rhods-operator reconciles the
`DataScienceCluster` — then read-modify-writes `spec.replicas` to the configured count, carrying the live
`metadata.resourceVersion` with a bounded 409 retry. The first write records the Deployment's pre-existing count in a
proof annotation (`openshift-ai/replicas-prior`) on the Deployment itself; the recorded prior is never re-baselined
while the annotation survives, and writes are by-value idempotent. The operator's own reconciles and a CronJob
correction from this chart do not re-record (a restore always goes to the first recorded prior). A `pre-delete` hook
restores the recorded priors and clears the annotations: a prior recorded as `unset` (the Deployment carried no
`spec.replicas` field when the chart first managed it) removes the field again. Deployments without the proof annotation
are never touched, and the Deployments themselves are never release manifests — uninstall never deletes them. Removing a
count from management (`dashboard: 1` → `dashboard: null`) does not restore its prior: only uninstall walks the recorded
values back, so un-manage a component deliberately (restore the count manually first, or accept the persisted override).

First-install timing: setting counts on the first install of this chart is representable but racy — the hook waits only
`replicas.waitTimeoutSeconds` (default 240s, `activeDeadlineSeconds` wait + 30) for the operator to deploy the operands,
and a fresh `DataScienceCluster` reconcile can exceed that, failing the install. Prefer installing without managed
counts first and adding them on a follow-up upgrade, or raise both `waitTimeoutSeconds` and the Helm `--timeout`.

Durability modes (`replicas.asCronJob`). The operator's live-replicas-wins merge means in-place reconciles preserve an
externally patched replica count, but anything that RECREATES a Deployment — OLM CSV bumps recreating operands at
manifest defaults (dashboard 2, operator 3), Argo adoption churn — reverts the count until something re-asserts it. Two
enforcement mechanisms exist for the configured counts:

- `asCronJob: true` (default): the chart renders a release-owned CronJob drift-corrector (release namespace,
  `concurrencyPolicy: Forbid`, `replicas.schedule` controls the interval) that runs the same enforcement script on a
  schedule. A recreate reverts the count and loses the proof annotation; the next scheduled run re-baselines the
  recreated Deployment's count as the new recorded prior and re-asserts the configured count, so a revert is corrected
  within one interval. The CronJob is an ordinary release manifest any GitOps tool can own and watch. Schedule runs skip
  Deployments that do not exist (a removed or not-yet-reconciled component) instead of failing every interval.
- `asCronJob: false`: the chart ships literal patch-shaped Deployment manifests (`metadata.annotations` +
  `spec.replicas` only) carrying `argocd.argoproj.io/sync-options: ServerSideApply=true` pre-set for ArgoCD adoption,
  for consumers whose GitOps manages the Deployments directly. This mode is hazardous under plain Helm and Flux, and the
  chart only renders it when at least one count is managed (the nothing-to-ship composition fails at render). Hazards,
  documented deliberately because the mode is opt-in:
  - Under plain Helm, installing a partial Deployment manifest ADOPTS the pre-existing operator-created Deployment into
    the release — ownership of the whole object, not just the patched field. `helm uninstall` then deletes the entire
    Deployment (and its rollout cascades), not just the replica override. The manifests therefore carry
    `helm.sh/resource-policy: keep`, which makes Helm skip deletion of the object on uninstall and on value removal —
    the object survives for Argo (or the operator's recreate) to own. Residual Helm ownership labels on the kept object
    are harmless but visible.
  - Flux/SSA does not adopt whole objects the way Helm does, but it also cannot own a single field of an object it does
    not fully manage; manage these manifests through Argo or not at all.
  - The hook pair renders and runs at install/upgrade in both modes — the `asCronJob` choice selects the durability
    layer, not whether the hooks exist. The partial manifests ADD the Argo/SSA path on top of the hooks, and both write
    the same field: under plain Helm, the set hook and a partial manifest patch one Deployment in turn (the hooks win on
    their schedule). When no GitOps tool manages the Deployments, the CronJob mode is the alternative durability layer —
    it re-asserts the counts on a schedule without rendering release-owned Deployment manifests.
  - Uninstall in this mode does not restore the recorded priors unless the `pre-delete` hook runs (it does — the hook
    pair still renders in this mode) — but the kept Deployment persists with whatever count it had at uninstall.

Reconcile-fight caveats. The rhods-operator's deploy action merges live `spec.replicas` into its desired state, so
in-place reconciles preserve an externally patched count ("live always wins") — EXCEPT for Deployments annotated
`opendatahub.io/managed-by-odh-operator: "true"`, for which the preservation branch is skipped. Whether a given RHOAI
release annotates the dashboard that way is release-specific; verify against the live Deployments before trusting either
durability mode on a real cluster. Recreate events (OLM CSV bumps, operator upgrades) revert counts to manifest defaults
regardless of that annotation — this is the observed behavior the durability layer exists to correct.

Uninstall contract. The Deployments are never chart-owned in either mode: with the CronJob mode, uninstall runs the
`pre-delete` restore hook (recorded priors restored under proof, annotations cleared, CronJob and hook plumbing removed)
and nothing else touches the Deployments; with the partial-manifests mode, `helm.sh/resource-policy: keep` leaves the
patched Deployments in place after the same restore.

Gateway classes
---------------

The chart does not render a `GatewayClass`. Both Gateways reference `data-science-gateway-class` by default — enabling
the OpenShift AI `aigateway` component installs that class (via its own `GatewayConfig` controller), so a fresh Helm
install of this chart simply references it. The class name stays configurable per gateway via
`gateways.<gateway>.gatewayClassName` for independent consumers; each Gateway's OpenShift Route backend Service is
derived as `<gateway-name>-<gatewayClassName>` to match the generated gateway Service, including class overrides.

Namespace attachment
--------------------

Each Gateway authorizes Route attachment with a `routeNamespaceSelector` LabelSelector (defaults in `values.yaml`:
`matchLabels: {maas-gateway-access: "true"}` for `maasDefaultGateway`, `{openshift-ai-inference-access: "true"}` for
`openshiftAiInference`). Administrators label the explicitly authorized routing namespaces — Helm does not render or
patch Namespaces, and a selector that resolves empty fails at render time because it would select all namespaces. Helm
coalescing makes the chart-defined default sticky: a user-supplied empty map (`{}`) coalesces back to the `values.yaml`
default rather than clearing it, and nulling the default `matchLabels` leaf deletes it, leaving a selector that fails at
render time. The selector is namespace-wide authorization: it does not isolate traffic from other route authors inside
an allowed namespace.

Gateway deployment settings
---------------------------

Each gateway accepts a `replicas` override (default `null`, which omits the field and keeps the operator default). The
override is applied through the release-verified ConfigMap deployment overlay (`spec.replicas` strategic-merge patch on
the generated gateway Deployment) — it is not written into the Gateway spec, and the chart does not manage the generated
Deployments. Actual runtime replica persistence is not validated by this chart's tests. The inference Gateway's
ConfigMap and its `infrastructure.parametersRef` render independently of `useRoute` (which controls Service/Route
behavior only).

MaasTenantConfig hook
---------------------

With `maasTenantConfig.enabled: true` (and the `modelsAsAService == Managed` gate), the chart renders a bounded
configuration hook for the product-managed `MaasTenantConfig/default-tenant` singleton, which the maas-controller
creates automatically on first install. The hook's Job, ServiceAccount, ConfigMap, and RBAC identity are placed in the
Helm release namespace — a namespace that exists at installation time — not in the asynchronously created product
namespace; the Job (the only Helm hook resource) waits for the singleton to appear and merge-patches only
`{spec: maasTenantConfig.spec}`. It neither creates the singleton nor changes ownerRefs or Helm ownership metadata.

A dedicated ClusterRole grants ONLY `get`/`patch` on `maastenantconfigs.maas.opendatahub.io` with
`resourceNames: [default-tenant]`, bound to the release-namespace ServiceAccount. The cluster scope of the permission
covers same-named singleton instances in any namespace, while the script targets only the configured namespace — this
boundary is needed for first-install independence (a Role in the not-yet-existing product namespace cannot be used).
Installing this chart requires an identity permitted to create ClusterRole/ClusterRoleBinding objects.

Polling uses the product-version REST resource path
`/apis/maas.opendatahub.io/v1alpha1/namespaces/<namespace>/maastenantconfigs/default-tenant` with bounded wall-clock
wait and per-request timeouts; only 404 (resource/API/namespace/singleton absent) is treated as expected startup absence
— Forbidden, unauthorized, transport, and patch errors fail immediately with actionable messages. The Job has
`backoffLimit: 0` and `activeDeadlineSeconds: waitTimeoutSeconds + 30`. Defaults stay below Helm's five-minute default,
but a fresh operator reconciliation may be slower — pass `--timeout 10m` to `helm install/upgrade` when enabling the
hook on a fresh cluster. The patch is serialized to JSON in the ConfigMap; that surface contains runtime settings, not
credentials. A successful patch does not wait for all MaaS workloads to become Ready, and settings persist after hook
disablement, value removal, or uninstall: an explicit `null` inside `spec` removes a field, omission does not reset it.
Helm rollback does not revert this side effect.

EvalHub tenant credential job
-----------------------------

With `evalhub.create: true` AND `evalhub.tenant.create: true`, the chart renders the `update-secret-minio` Job in the
tenant namespace as a `post-install,post-upgrade` Helm hook (recreated on re-runs via `before-hook-creation`); its
ServiceAccount, RoleBinding, script ConfigMap, Namespace, and DSPA stay ordinary release resources. A failed hook
affects release status. The Job waits up to `evalhub.tenant.waitTimeoutSeconds` of wall-clock time for the
product-created `ds-pipeline-s3-dspa` Secret to appear, then fails without mutating anything. Only a missing named
secret is retried — Forbidden, unauthorized, and transport errors fail immediately — and timeout diagnostics name both
the namespace and the secret. The Job reads the secret's `accesskey`/`secretkey` keys, rejects a missing key, invalid
base64, or empty/whitespace-only content before writing anything, and decodes them into private pod files (preserving
literal quotes, backslashes, and newlines) feeding the five `AWS_*` keys into
`oc set data secret/ds-pipeline-s3-dspa --from-file=<key>=<file>`. The CLI encodes the changed data map and patches only
those keys — unrelated Secret keys, labels/annotations, and ownership are preserved — and credentials never appear in
command arguments or diagnostics. This validates the payload only; it does not verify credential validity. The Job has
`backoffLimit: 3` and `activeDeadlineSeconds: waitTimeoutSeconds + 30`; longer configured waits need a matching Helm
`--timeout`.

Helm 4 note: a plain `helm upgrade` defaults to `hookOnly` waiting, whose deletion waiter is a no-op, so
`before-hook-creation` alone does not guarantee race-free Job name reuse while an old hook Job is still terminating.
Explicit `--wait=watcher` (or bare `--wait`) or `--wait=legacy` provides deletion waiting, but also waits for normal
release resources to become Ready before the post-hook executes. The installing identity — not the Job's `patch-secret`
ServiceAccount — needs Job delete/get/create rights in the tenant namespace, plus the relevant list/watch permissions
for watcher waits. `--no-hooks` skips credential configuration and is not an equivalent deployment mode. Helm does not
directly uninstall hook Jobs; tenant Namespace deletion collects them. Helm rollback does not revert Secret side
effects, and rollback to a pre-hook chart revision can encounter the existing hook Job. No invocation is promised
race-free; real-cluster upgrade timing, migration, and rollback behavior are not validated by this chart's tests.

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `dataScienceCluster.components` — the full 14-component management-state matrix; pass an empty map to render a bare
  `spec: {}`. Kueue queue-name fields (`defaultClusterQueueName`/`defaultLocalQueueName`) are only effective when
  `kueue.managementState` is `Unmanaged`, so the default `Removed` entry carries no queue names
- `dashboardConfig` — free-form `OdhDashboardConfig` spec passthrough; representative commented examples in
  `values.yaml` cover `modelAsService`, `disableLLMd`, and `gpuaas` alongside the established flags
- `genAiStudioConfig.aiAssetCustomEndpoints` — custom-endpoint config for the Gen AI Studio asset surface
  (`externalProviders`/`clusterDomains`); commented example in `values.yaml`
- `globalMLflowNamespaces` — DSC spec-level list of namespaces where MLflow deployments are allowed (Tech Preview);
  pairs with the `mlflowoperator` component. Commented example in `values.yaml`
- `dataScienceClusterInitialization.monitoring` — the commented example in `values.yaml` shows the optional
  `monitoring.traces` block (`exporters`, `sampleRatio`, `storage` — `storage.size` is required for traces) and
  `monitoring.alerting` alongside the metrics storage defaults
- `gateways.maasDefaultGateway` / `gateways.openshiftAiInference` — Gateway plumbing with `hostname` override support,
  certificate/Route selection (`useRoute` vs `certificate.create`), the referenced `gatewayClassName`, the
  `routeNamespaceSelector` namespace authorization, and the `replicas` deployment overlay (null omits the patch field)
- `maasTenantConfig` — bounded configuration hook for the product-created `MaasTenantConfig/default-tenant` singleton:
  `enabled` (default `false`), `namespace` (default `models-as-a-service`, the product default), `waitTimeoutSeconds`
  (default 240), `pollIntervalSeconds` (default 5), and the explicit `spec` merge-patch map (default `{}`; enabling with
  an empty spec fails at render time). The singleton name is fixed as `default-tenant` (product CEL singleton).
  Representative telemetry settings with privacy-conscious `telemetry.metrics.captureUser: false` are in the commented
  example in `values.yaml`
- `maasDb.create` — render the maas-db bootstrap plumbing AND deploy the postgres Cluster through the dependency
  (default: `false`; the dependency condition shares this toggle)
- `maasDb.namespace` — namespace the bootstrap Job renders into (default: `redhat-ai-gateway-infra`); must already exist
  before this chart installs, and must match `postgres.namespace` — the Cluster renders there and the job looks the
  secret up there. Neither this chart nor the postgres-cluster dependency renders a Namespace; ensure the configured
  namespace exists in both managed and out-of-band modes before namespace-targeted chart resources install
- `maasDb.secretName` — out-of-band override: name of the secret holding the postgres connection URI. Empty by default —
  managed mode (with `maasDb.create: true`) derives the Cluster's `<fullname>-app` secret name via the
  `openshift-ai.pg.fullname` helper; setting it instead runs the job against an external Cluster. Either `maasDb.create`
  or `maasDb.secretName` (with `maasManaged`) is required for any maas-db plumbing to render
- `maasDb.waitTimeoutSeconds` — bounded wall-clock wait (default 240) for the source secret before the bootstrap Job
  fails; the Job's `activeDeadlineSeconds` is this value plus 30. Must be a positive integer (validated at render time)
- `postgres` — the postgres-cluster dependency's Cluster values (rendered only when `maasDb.create` is true; default: 2
  instances, 2Gi storage, modest resource requests/limits, namespace `redhat-ai-gateway-infra`)
- `kedaMetricsReader.create` — opt-in KEDA metrics-reader plumbing (default: `false`); only enable when WVA/KEDA is
  actually deployed
- `mlflow.create` / `evalhub.create` — opt-in operands with free-form `spec` passthrough
- `evalhub.tenant.waitTimeoutSeconds` — bounded wall-clock wait (default 240) for the product-created
  `ds-pipeline-s3-dspa` secret before the `update-secret-minio` credential Job fails; the Job's `activeDeadlineSeconds`
  is this value plus 30. Must be a positive integer (validated at render time)
- `hardwareProfiles` — one `HardwareProfile` per entry (CPU/Memory/GPU identifiers, Node or Queue scheduling). A
  Queue-type profile (`type: Queue`) must set `kueue.localQueueName` naming an existing LocalQueue in the profile's
  namespace (default: `redhat-ods-applications`); rendering fails otherwise, since a missing LocalQueue reference
  produces a non-functional profile that stalls workloads inadmissible
- `replicas` — replica management for the `rhods-dashboard`/`rhods-operator` Deployments (all components default `null`
  = hands-off): per-component counts (`dashboard`/`operator` — explicit 0 is managed, presence-aware), `asCronJob`
  (default `true`: CronJob drift-corrector; `false`: patch-shaped partial Deployment manifests with server-side-apply
  annotations for the Argo path — README-documented hazards, fails at render with no managed counts), `schedule`
  (default `*/15 * * * *`), and `waitTimeoutSeconds` (default 240; the set Job's `activeDeadlineSeconds` is this + 30;
  must be a positive integer, validated at render time). Requires the rhods-operator to be installed first — the managed
  Deployments do not exist before it deploys the operands. See the "Replica management" section above for the full
  enforcement, durability, and uninstall contract
