maas-umbrella
===

This is an example operand umbrella chart (not published) that composes the published charts of this repository as
subchart dependencies with values overlays. It is deliberately placed outside `charts/` so the version-gated publishing
script never discovers it.

Two-install flow
---

OLM makes the custom resources required by the operand charts (`Keycloak`, `KeycloakRealmImport`, CloudNativePG
`Cluster`) available only after the operators are installed, and a helm-native install cannot order a Subscription and
its operand CR in one release. Deployment therefore uses two installs:

1. **Operator install** — the `install-operators` chart, one release covering all OLM operators via its `operators` map.
   `operator-values.yaml` in this example pairs with it for the operators required by the operand charts of this
   repository (RHBK, CloudNativePG, Red Hat Connectivity Link, LeaderWorkerSet, NFD, and NVIDIA GPU Operator):

   ```
   helm upgrade --install install-operators charts/install-operators \
       --values examples/maas-umbrella/operator-values.yaml
   ```

2. **Operand install** — this umbrella chart, which deploys `keycloak` (with its `postgres-cluster` dependency) and
   `keycloak-auth`:

   ```
   helm dependency build examples/maas-umbrella
   helm upgrade --install maas-umbrella examples/maas-umbrella
   ```

Wait for the operator deployments to be ready before the second install.

Composing charts from this repository
---

Each chart in this repository is subchart-friendly: values are namespaced under the chart's own key, charts support an
`enabled` toggle for conditional inclusion, and `global.*` passthrough compatibility is preserved. An umbrella chart
declares the published OCI charts as dependencies in its `Chart.yaml` and configures them entirely through values, as
`values.yaml` in this example demonstrates. Additional charts published from this repository can be composed as
dependencies the same way.

Shared OIDC values are set once in this chart under `global.oidc`: `keycloak-auth` reads
`global.oidc.{realm,clientId,secret}` directly, and the keycloak chart's realm client entry references
`global.oidc.secret` via a template string — changing the secret in one place updates both the realm client and the
openid-client-secret Secret. The realm name and client ID are structural in the keycloak chart (a map key and a client
name), so `global.oidc.realm`/`global.oidc.clientId` must match them — a mismatch silently points the issuer at a realm
that was never imported.

Running locally
---

Build the dependencies (Helm 3.8+ supports OCI dependencies; anonymous pulls work for public ghcr packages):

```
helm dependency build examples/maas-umbrella
```

Lint and render the example:

```
helm lint examples/maas-umbrella
helm template examples/maas-umbrella
```
