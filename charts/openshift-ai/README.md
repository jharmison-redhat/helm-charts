openshift-ai
============

A chart to deploy OpenShift AI and manage `DataScienceCluster`/`DSCInitialization` configuration: the 14-component DSC
matrix (every component key the chart manages exists in the live `DataScienceCluster` v2 API; `aigateway` is the
RHOAI-downstream component that the upstream docs example omits), the dashboard and telemetry configuration, Gateway API
plumbing for MaaS and inference traffic, KEDA metrics-reader plumbing, MLflow/EvalHub operands, hardware profiles, and a
maas-db bootstrap Job.

The `rhods-operator` is NOT installed by this chart. It must already be installed before this chart is deployed — the
chart renders custom resources directly (`DataScienceCluster`, `OdhDashboardConfig`, `Telemetry`, and so on), and a
plain `helm install` fails on the missing CRDs otherwise. The source GitOps composition relied on ArgoCD
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
- Renders a `GatewayClass` named `data-science-gateway-class` whenever either Gateway renders
- Renders the `maas-default-gateway` plumbing (ConfigMap, Certificate/Route, optional EnvoyFilter, Gateway) plus
  `Telemetry`/`TelemetryPolicy` — gated on the DSC's `aigateway.modelsAsAService` being `Managed` (evaluates false when
  the components tree is unset)
- Renders the `openshift-ai-inference` Gateway plumbing (gated by `gateways.openshiftAiInference.create`)
- Renders the payload-processing `NetworkPolicy` (gated by `maasPayloadProcessingNetworkPolicy.create`)
- Renders the KEDA metrics-reader plumbing: ServiceAccount, ClusterRoleBinding, token Secret, and
  `ClusterTriggerAuthentication` in `openshift-keda` (gated by `kedaMetricsReader.create`)
- Renders MLflow (gated by `mlflow.create`) and EvalHub (gated by `evalhub.create`) with `spec` passthrough, plus the
  evalhub-tenant subtree (gated by `evalhub.create` AND `evalhub.tenant.create`)
- Renders the maas-db bootstrap plumbing: ServiceAccount, RoleBinding, script ConfigMap, and the bootstrap Job in
  `maasDb.namespace` (gated by the `modelsAsAService == Managed` gate AND `maasDb.create` or `maasDb.secretName`)

The maas-db bootstrap Job runs as a `post-install,post-upgrade` Helm hook (recreated on re-runs via
`before-hook-creation`), so it re-runs on upgrades the way the source GitOps PostSync hook did.

The chart declares an OCI dependency on the published `postgres-cluster` chart of this repository
(`oci://ghcr.io/jharmison-redhat/helm-charts`), pinned `>=0.1.0 <0.2.0`, aliased to `postgres`, and condition-gated by
`maasDb.create`. With `maasDb.create: true`, the dependency deploys the Cluster and the bootstrap job derives its secret
name from the `openshift-ai.pg.fullname` convention (see `templates/_helpers.tpl`); with `maasDb.secretName` set
instead, the job runs out-of-band against an external Cluster.

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
- `gateways.maasDefaultGateway` / `gateways.openshiftAiInference` — Gateway plumbing with `hostname` override support
  and certificate/Route selection (`useRoute` vs `certificate.create`)
- `maasDb.create` — render the maas-db bootstrap plumbing AND deploy the postgres Cluster through the dependency
  (default: `false`; the dependency condition shares this toggle)
- `maasDb.namespace` — namespace the bootstrap Job renders into (default: `redhat-ai-gateway-infra`); must already
  exist, and must match `postgres.namespace` — the Cluster renders there and the job looks the secret up there
- `maasDb.secretName` — out-of-band override: name of the secret holding the postgres connection URI. Empty by default —
  managed mode (with `maasDb.create: true`) derives the Cluster's `<fullname>-app` secret name via the
  `openshift-ai.pg.fullname` helper; setting it instead runs the job against an external Cluster. Either `maasDb.create`
  or `maasDb.secretName` (with `maasManaged`) is required for any maas-db plumbing to render
- `postgres` — the postgres-cluster dependency's Cluster values (rendered only when `maasDb.create` is true; default: 2
  instances, 2Gi storage, modest resource requests/limits, namespace `redhat-ai-gateway-infra`)
- `kedaMetricsReader.create` — opt-in KEDA metrics-reader plumbing (default: `false`); only enable when WVA/KEDA is
  actually deployed
- `mlflow.create` / `evalhub.create` — opt-in operands with free-form `spec` passthrough
- `hardwareProfiles` — one `HardwareProfile` per entry (CPU/Memory/GPU identifiers, Node or Queue scheduling). A
  Queue-type profile (`type: Queue`) must set `kueue.localQueueName` naming an existing LocalQueue in the profile's
  namespace (default: `redhat-ods-applications`); rendering fails otherwise, since a missing LocalQueue reference
  produces a non-functional profile that stalls workloads inadmissible
- `dashboardReplicas` / `operatorReplicas` — set-on-upgrade knobs: override the replica counts of the
  `rhods-dashboard`/`rhods-operator` Deployments. Must NOT be set on first install — the Deployments do not exist until
  the rhods-operator creates the operands, and a partial manifest would render an invalid Deployment. Leave unset (null)
  until after the operator has deployed.
