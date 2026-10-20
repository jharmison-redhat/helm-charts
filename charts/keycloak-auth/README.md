keycloak-auth
==============

A chart to configure OpenShift authentication against a Keycloak instance deployed by the Red Hat Build of Keycloak
operator. It applies a cluster-scoped OAuth `OpenID` identity provider, manages the `openid-client-secret` Secret in
`openshift-config`, sets up a `cluster-admins` Group with a ClusterRoleBinding, and optionally removes the `kubeadmin`
Secret.

This chart has no chart dependencies and renders standalone. It is designed to be consumed alongside the `keycloak`
chart (which deploys the Keycloak instance and realm import): an umbrella consumer composes both and provides matching
values — the `keycloak.name`/`keycloak.realm` and `openidClient.id`/`openidClient.secret` here must match the Keycloak
CR name, realm name, and realm client configured in the `keycloak` chart.

Usage
-----

The chart performs the following:

- Applies an `OAuth` cluster resource with an OpenID identity provider pointing at the Keycloak realm's issuer URL,
  mapping the `email`, `name`, `preferred_username`, and `groups` claims
- Creates the `openid-client-secret` Secret in the `openshift-config` namespace from values (`openidClient.secret` is
  required — no default is provided)
- Creates a `cluster-admins` Group bound to the `cluster-admin` ClusterRole
- Optionally removes the `kubeadmin` Secret via a Job that runs as a Helm `post-install,post-upgrade` hook (recreated on
  re-runs via `before-hook-creation`); enable it with `removeKubeAdmin.enabled: true`
- Optionally references a `router-ca` ConfigMap in `openshift-config` as the identity provider's CA (set `ingressCA` to
  match the `keycloak` chart's `ingressCA` value)

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
global:
  wildcardDomain: apps.cluster.example.com
  toolsImage: image-registry.openshift-image-registry.svc:5000/openshift/tools:latest

keycloak:
  name: keycloak
  realm: sso

openidClient:
  id: ocp-idp
  secret: <client-secret>

clusterAdmins:
  users:
    - admin

removeKubeAdmin:
  enabled: true
```

Values
------

See `values.yaml` for the full set of commented examples. Notable keys:

- `global.wildcardDomain` — wildcard domain under which the Keycloak instance is exposed
- `global.toolsImage` — image for the kubeadmin-removal Job container
- `keycloak.name` / `keycloak.realm` — the Keycloak CR name and realm for the issuer URL (defaults: `keycloak`, `sso`)
- `openidClient.id` / `openidClient.secret` — the OIDC client identity; the secret is required and must match the
  corresponding realm client secret in the `keycloak` chart
- `ingressCA` — optional CA bundle; when set, the identity provider uses the `router-ca` ConfigMap
- `clusterAdmins.users` — usernames added to the `cluster-admins` Group (default: `[admin]`)
- `removeKubeAdmin.enabled` — gate for the kubeadmin removal Job (default: `true` when unset)
