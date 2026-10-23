openshift-ai
============

A chart to deploy OpenShift AI and manage `DataScienceCluster`/`DSCInitialization` configuration: the 14-component DSC
matrix, the dashboard and telemetry configuration, Gateway API plumbing for MaaS and inference traffic, KEDA
metrics-reader plumbing, MLflow/EvalHub operands, hardware profiles, and a maas-db bootstrap Job.

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
- Renders `OdhDashboardConfig` + `Auth` (dashboard config incl. the 3.5 feature flags), the segment-key and
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
  `maasDb.namespace` (gated by `maasDb.create` and the `modelsAsAService == Managed` gate)

The maas-db bootstrap Job runs as a `post-install,post-upgrade` Helm hook (recreated on re-runs via
`before-hook-creation`), so it re-runs on upgrades the way the source GitOps PostSync hook did.

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `dataScienceCluster.components` — the full 14-component management-state matrix; pass an empty map to render a bare
  `spec: {}`
- `gateways.maasDefaultGateway` / `gateways.openshiftAiInference` — Gateway plumbing with `hostname` override support
  and certificate/Route selection (`useRoute` vs `certificate.create`)
- `maasDb.create` — render the maas-db bootstrap plumbing (default: `false`)
- `maasDb.namespace` — namespace the bootstrap Job renders into (default: `redhat-ai-gateway-infra`); must already exist
  (the postgres Cluster's namespace is created by whatever deploys it)
- `maasDb.secretName` — REQUIRED: name of the secret holding the postgres connection URI (the Cluster's `<fullname>-app`
  secret); the bootstrap Job reads it to create the `maas-db-config` secret
- `kedaMetricsReader.create` — opt-in KEDA metrics-reader plumbing (default: `false`); only enable when WVA/KEDA is
  actually deployed
- `mlflow.create` / `evalhub.create` — opt-in operands with free-form `spec` passthrough
- `hardwareProfiles` — one `HardwareProfile` per entry (CPU/Memory/GPU identifiers, Node or Queue scheduling)
- `dashboardReplicas` / `operatorReplicas` — set-on-upgrade knobs: override the replica counts of the
  `rhods-dashboard`/`rhods-operator` Deployments. Must NOT be set on first install — the Deployments do not exist until
  the rhods-operator creates the operands, and a partial manifest would render an invalid Deployment. Leave unset (null)
  until after the operator has deployed.
