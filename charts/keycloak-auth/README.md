keycloak-auth
=============

A chart to configure OpenShift authentication against a Keycloak instance deployed by the Red Hat Build of Keycloak
operator. It configures the platform's cluster-scoped OAuth object with an `OpenID` identity provider, manages the
`openid-client-secret` Secret in `openshift-config`, sets up a `cluster-admins` Group with a ClusterRoleBinding, and can
remove the `kubeadmin` Secret when explicitly opted in.

This chart has no chart dependencies and renders standalone. It is designed to be consumed alongside the `keycloak`
chart (which deploys the Keycloak instance and realm import): an umbrella consumer composes both and provides the OIDC
values once under `global.oidc` — both charts read `global.oidc.{realm,clientId,secret}` through global propagation.
Standalone consumers set `global.oidc.*` directly: `realm` must match a keycloak chart `realms` map key and `clientId`
must match a client name within it, and `keycloak.name` must match the Keycloak CR name.

Usage
-----

The chart performs the following:

- Configures the platform's existing cluster OAuth object (`oauths.config.openshift.io/cluster`) with an OpenID identity
  provider pointing at the Keycloak realm's issuer URL, mapping the `email`, `name`, `preferred_username`, and `groups`
  claims — through `post-install,post-upgrade` hook Jobs that upsert only this chart's named entry and preserve all
  other identity providers (see OAuth configuration below)
- Creates the `openid-client-secret` Secret in the `openshift-config` namespace from values (`global.oidc.secret` is
  required — no default is provided)
- Creates a `cluster-admins` Group bound to the `cluster-admin` ClusterRole
- Removes the `kubeadmin` Secret via a Job that runs as a Helm `post-install,post-upgrade` hook (recreated on re-runs
  via `before-hook-creation`); disabled by default — opt in with `removeKubeAdmin: true` only after exercising a
  replacement administrator login (see `removeKubeAdmin` under Values)
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

removeKubeAdmin: false
```

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `global.wildcardDomain` — wildcard domain under which the Keycloak instance is exposed (default:
  `apps.cluster.example.com`)
- `global.toolsImage` — image for the hook Job containers (kubeadmin removal and OAuth identity provider upsert/remove;
  requires the `oc` CLI plus jq or python3) (default: the in-cluster OpenShift tools image)
- `global.oidc.realm` / `global.oidc.clientId` / `global.oidc.secret` — the OIDC issuer realm, client identity, and
  client secret (defaults: `sso`, `ocp-idp`, empty). The realm must match a `realms` map key and the client ID a client
  name in the `keycloak` chart; setting the realm to a name with no matching realm makes the issuer point at a realm
  that was never imported (no fail-fast). The secret is required and must match the corresponding realm client secret in
  the `keycloak` chart. Leaving the secret empty skips rendering the identity-provider hook Jobs, their
  `oauth-idp` ServiceAccount/ClusterRole/ClusterRoleBinding, the `oauth-idp-config` ConfigMap, and the
  `openid-client-secret` Secret.
- `keycloak.name` — the Keycloak CR name for the issuer URL (default: `keycloak`, matching the keycloak chart's
  chart-name-based CR name)
- `oauth.idpName` — the identity provider entry name the hook Jobs manage in the cluster OAuth resource (default:
  `rhbk`)
- `ingressCA` — optional CA bundle; when set, the `router-ca` ConfigMap is rendered and the identity provider uses it —
  only needed when the identity provider's TLS is issued by private enterprise PKI (a CA that is neither publicly
  trusted nor the cluster's own, whose CA the OAuth server already trusts)
- `clusterAdmins.users` — usernames added to the `cluster-admins` Group (default: `[admin]`)
- `removeKubeAdmin` — set to `true` to remove the kubeadmin Secret (default: `false`)

Secrets are never defaulted: the OIDC client secret must be provided through values.

OAuth configuration
-------------------

The OpenShift platform always creates the cluster-scoped OAuth object (`oauths.config.openshift.io/cluster`), so this
chart never owns or deletes it — it configures it. On `helm install` and `helm upgrade`, `post-install,post-upgrade`
hook Jobs upsert this chart's named identity provider entry (from `oauth.idpName`) into the object's
`spec.identityProviders` list, preserving every other entry. The write is resourceVersion-conditional:
`spec.identityProviders` is an atomic list that cannot be patched entry-by-entry, so each Job reads the live object,
computes the merged list, and patches it with the current `metadata.resourceVersion` — a concurrent change is rejected
with a conflict and retried a bounded number of times. The chart's entry is looked up by name: if it is already present
and exactly equal, the Job exits successfully without writing; if an entry with the same name differs, it is replaced in
place, preserving its position in the list; if it is absent, it is appended. If the OAuth object is absent (not a real
OpenShift cluster), the upsert Job fails with a clear prerequisite message.

On `helm uninstall`, a `pre-delete` hook Job removes the named entry from the list with the same
resourceVersion-conditional mechanism, before the release deletes the `openid-client-secret` Secret — so the identity
provider never references a Secret that no longer exists. If the named entry is already absent, or the OAuth object
itself is gone, the removal is a successful no-op. The OAuth object itself is never owned or deleted by this release.

Clearing `global.oidc.secret` on an upgrade does not remove the `openid-client-secret` Secret or the identity provider
entry: Helm does not prune manifests that a release stops rendering, and the uninstall removal hook itself only exists
while the secret is set.

The identity provider entry contains no secret material — the client secret is referenced by name through the
`openid-client-secret` Secret this chart creates. Verifying that a replacement administrator can log in through the
configured identity provider remains the operator's responsibility; the presence of the identity provider entry is not
proof that anyone can authenticate.

Kubeadmin removal
-----------------

The `kubeadmin` Secret is the break-glass administrator credential created by the OpenShift installer. Deleting it is
irreversible: Helm rollback and `helm uninstall` do not recreate it, and this chart does not verify that a replacement
administrator login works — configured resources (the OAuth identity provider, the `cluster-admins` Group, or anything
else this chart manages) are not proof that anyone can authenticate. Before setting `removeKubeAdmin: true`, log in as a
replacement administrator and confirm that access works. The removal Job deletes only the `kubeadmin` Secret in
`kube-system` with a delete-only Role; a successful run means the API accepted the deletion request, not that a login
was verified.
