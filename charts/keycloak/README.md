keycloak
========

A chart to deploy a Keycloak instance using the Red Hat Build of Keycloak operator (RHBK), including the RHBK
Subscription and OperatorGroup (both toggleable), an inline CloudNativePG `Cluster` behind an `enabled` toggle, and a
multi-realm map-driven `KeycloakRealmImport`.

This chart has no chart dependencies and renders standalone. It is designed to be consumed alongside the `keycloak-auth`
chart (which configures OpenShift authentication against the deployed Keycloak instance): an umbrella consumer composes
both and provides matching values.

Usage
-----

The chart performs the following:

- Installs the RHBK operator via a `Subscription` and `OperatorGroup` (gated by `operator.subscription.enabled` and
  `operator.operatorGroup.enabled`, both default `true` when unset) so an umbrella consumer can turn operator install
  off and manage it with the `install-operators` chart instead
- Deploys a `Keycloak` custom resource backed by PostgreSQL, using the CloudNativePG naming convention (`<name>-rw` for
  the read-write service and `<name>-app` for the credentials Secret)
- Creates an inline CloudNativePG `Cluster` behind the `postgresCluster.enabled` toggle; with it disabled, the database
  connection can be overridden with the `db` values (host, database, credentialsSecret)
- Renders a `KeycloakRealmImport` per entry in the `realms` map, with per-realm clients, roles, generated and explicit
  users, groups (explicit list or generated from roles), and `clientScopes` passthrough
- Exposes Keycloak through a reencrypt `Route` at `<name>.<wildcardDomain>`
- Optionally renders a `router-ca` trust ConfigMap in `openshift-config` (set `ingressCA` to enable it, and match it in
  the `keycloak-auth` chart)

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
global:
  wildcardDomain: apps.cluster.example.com
  toolsImage: image-registry.openshift-image-registry.svc:5000/openshift/tools:latest

replicas: 1

operator:
  channel: stable-v26.2
  subscription:
    enabled: true
  operatorGroup:
    enabled: true

postgresCluster:
  enabled: true
  name: keycloak-postgres

realms:
  sso:
    admin:
      username: admin
      password: <admin-password>
    generated:
      prefix: user
      count: 5
      password: <user-password>
    groupsFromRoles: true
    clients:
      ocp-idp:
        secret: <client-secret>
    clientScopes:
      # See values.yaml for the full example set, including the groups claim
      # mapper (oidc-usermodel-realm-role-mapper)
      - name: groups
        protocol: openid-connect
```

Values
------

See `values.yaml` for the full set of commented examples. Notable keys:

- `global.wildcardDomain` — wildcard domain under which Keycloak is exposed (`<name>.<wildcardDomain>`)
- `global.toolsImage` — image for Helm hook Job containers
- `namespace` — namespace for the Keycloak resources and the operator (default: the release namespace)
- `operator.subscription.enabled` / `operator.operatorGroup.enabled` — gates for the RHBK Subscription and OperatorGroup
  (default `true` when unset)
- `postgresCluster.enabled` — gate for the inline CloudNativePG `Cluster` (default `true` when unset)
- `db.host` / `db.database` / `db.credentialsSecret` — external database overrides used when the inline cluster is
  disabled (defaults follow the CloudNativePG naming convention from `postgresCluster.name`)
- `realms` — map of realm definitions (see the commented example for clients, roles, users, groups, and the full
  `clientScopes` set including the groups claim mapper)
- `emailDomain` — domain used for realm-user email defaults
- `ingressCA` — optional CA bundle rendered as the `router-ca` ConfigMap in `openshift-config`

Secrets are never defaulted: realm admin/user passwords and realm client secrets must be provided through values (the
realm admin password is required at render time).
