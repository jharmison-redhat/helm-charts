keycloak-auth
=============

A chart to configure OpenShift authentication against a Keycloak instance deployed by the Red Hat Build of Keycloak
operator. It applies a cluster-scoped OAuth `OpenID` identity provider, manages the `openid-client-secret` Secret in
`openshift-config`, sets up a `cluster-admins` Group with a ClusterRoleBinding, and optionally removes the `kubeadmin`
Secret.

This chart has no chart dependencies and renders standalone. It is designed to be consumed alongside the `keycloak`
chart (which deploys the Keycloak instance and realm import): an umbrella consumer composes both and provides the OIDC
values once under `global.oidc` — both charts read `global.oidc.{realm,clientId,secret}` through global propagation.
Standalone consumers set `global.oidc.*` directly: `realm` must match a keycloak chart `realms` map key and `clientId`
must match a client name within it, and `keycloak.name` must match the Keycloak CR name.

Usage
-----

The chart performs the following:

- Applies an `OAuth` cluster resource with an OpenID identity provider pointing at the Keycloak realm's issuer URL,
  mapping the `email`, `name`, `preferred_username`, and `groups` claims
- Creates the `openid-client-secret` Secret in the `openshift-config` namespace from values (`global.oidc.secret` is
  required — no default is provided)
- Creates a `cluster-admins` Group bound to the `cluster-admin` ClusterRole
- Optionally removes the `kubeadmin` Secret via a Job that runs as a Helm `post-install,post-upgrade` hook (recreated on
  re-runs via `before-hook-creation`); enabled by default, disable it with `removeKubeAdmin: false`
- Optionally renders a `router-ca` trust ConfigMap in `openshift-config` as the identity provider's CA (set `ingressCA`
  to enable it)

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
global:
  wildcardDomain: apps.cluster.example.com
  toolsImage: image-registry.openshift-image-registry.svc:5000/openshift/tools:latest
  oidc:
    realm: sso
    clientId: ocp-idp
    secret: <client-secret>

keycloak:
  name: keycloak

clusterAdmins:
  users:
    - admin

removeKubeAdmin: true
```

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `global.wildcardDomain` — wildcard domain under which the Keycloak instance is exposed (default:
  `apps.cluster.example.com`)
- `global.toolsImage` — image for the kubeadmin-removal Job container (default: the in-cluster OpenShift tools image)
- `global.oidc.realm` / `global.oidc.clientId` / `global.oidc.secret` — the OIDC issuer realm, client identity, and
  client secret (defaults: `sso`, `ocp-idp`, empty). The realm must match a `realms` map key and the client ID a client
  name in the `keycloak` chart; setting the realm to a name with no matching realm makes the issuer point at a realm
  that was never imported (no fail-fast). The secret is required and must match the corresponding realm client secret in
  the `keycloak` chart. Leaving the secret empty skips rendering the OAuth resource and the Secret.
- `keycloak.name` — the Keycloak CR name for the issuer URL (default: `keycloak`, matching the keycloak chart's
  chart-name-based CR name)
- `oauth.idpName` — the identity provider name in the cluster OAuth resource (default: `rhbk`)
- `ingressCA` — optional CA bundle; when set, the `router-ca` ConfigMap is rendered and the identity provider uses it
- `clusterAdmins.users` — usernames added to the `cluster-admins` Group (default: `[admin]`)
- `removeKubeAdmin` — set to `false` to keep the kubeadmin Secret (default: `true`)

Secrets are never defaulted: the OIDC client secret must be provided through values.
