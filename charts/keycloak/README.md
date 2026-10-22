keycloak
========

A chart to deploy a Keycloak instance using the Red Hat Build of Keycloak operator (RHBK), installing the RHBK and
CloudNativePG operators via the `install-operators` chart dependency, deploying a PostgreSQL cluster through the
`postgres-cluster` chart dependency, and rendering a multi-realm map-driven `KeycloakRealmImport`.

The chart declares OCI dependencies on the published charts of this repository
(`oci://ghcr.io/jharmison-redhat/helm-charts`), both condition-gated: `install-operators` pinned `>=0.2.0 <0.3.0` and
`postgres-cluster` pinned `>=0.1.0 <0.2.0`. Run `helm dependency build` before installing. An umbrella consumer can
disable operator install by setting `install-operators.enabled: false` and manage the operators with its own
`install-operators` composition instead, or disable the database by setting `postgres-cluster.enabled: false` and
supplying external `db:` overrides.

This chart is designed to be consumed alongside the `keycloak-auth` chart (which configures OpenShift authentication
against the deployed Keycloak instance): an umbrella consumer composes both and provides matching values.

Usage
-----

The chart performs the following:

- Installs the RHBK operator through the `install-operators` chart dependency (gated by `install-operators.enabled`,
  default `true`) with `rhbk-operator` and `cloudnative-pg` entries configured under `install-operators.operators`
- Deploys a `Keycloak` custom resource backed by PostgreSQL, wired by convention to the dependency's Cluster (see the
  `keycloak.pg.fullname` helper in `templates/_helpers.tpl`)
- Deploys a CloudNativePG `Cluster` through the `postgres-cluster` chart dependency (gated by
  `postgres-cluster.enabled`, default `true`); with it disabled, the database connection must be overridden with the
  `db` values (host, database, credentialsSecret — all required)
- Renders a `KeycloakRealmImport` per entry in the `realms` map, with per-realm clients, roles, generated and explicit
  users, groups (explicit list or generated from roles), and `clientScopes` passthrough
- Exposes Keycloak through a reencrypt `Route` at `<name>.<wildcardDomain>`

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
global:
  wildcardDomain: apps.cluster.example.com

replicas: 1

install-operators:
  enabled: true
  operators:
    rhbk-operator:
      channel: stable-v26.2

postgres-cluster:
  enabled: true

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
      # See values.yaml for a representative example, including the groups
      # claim mapper (oidc-usermodel-realm-role-mapper)
      - name: groups
        protocol: openid-connect
```

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `global.wildcardDomain` — wildcard domain under which Keycloak is exposed (`<name>.<wildcardDomain>`; default:
  `apps.cluster.example.com`)
- `namespace` — namespace for the Keycloak resources (empty by default, meaning the release namespace)
- `replicas` — number of Keycloak instances (default: `1`)
- `emailDomain` — domain used for realm-user email defaults (default: `example.com`)
- `install-operators.enabled` / `install-operators.operators` — the operator installs via the `install-operators`
  dependency (default: enabled, with `rhbk-operator` on channel `stable-v26.2` in the `keycloak` namespace with a
  matching OperatorGroup, and `cloudnative-pg` on channel `stable-v1` from the `certified-operators` catalog)
- `postgres-cluster` — the backing PostgreSQL `Cluster` via the postgres-cluster dependency (enabled by default; named
  `keycloak-postgres` via `fullnameOverride`, 1 instance, 2Gi storage, modest resource requests/limits). The cluster
  uses the CloudNativePG default bootstrap — see the `keycloak.pg.fullname` helper in `templates/_helpers.tpl` for the
  naming convention the `db` block defaults rely on
- `db.host` / `db.database` / `db.credentialsSecret` — external database overrides used when the `postgres-cluster`
  dependency is disabled (all three are then required; by default they derive from the dependency's cluster)
- `realms` — map of realm definitions (see the commented example for clients, roles, users, groups, and representative
  `clientScopes` including the groups claim mapper)

Secrets are never defaulted: realm admin/user passwords and realm client secrets must be provided through values. The
realm admin password is required at render time, as is the generated-users password when `generated.count > 0`. A realm
definition without an `admin` block renders without an admin user.
