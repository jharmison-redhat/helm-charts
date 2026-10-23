keycloak
========

A chart to deploy a Keycloak instance using the Red Hat Build of Keycloak operator, deploying a PostgreSQL cluster
through the `postgres-cluster` chart dependency, and rendering a multi-realm map-driven `KeycloakRealmImport`.

The Red Hat Build of Keycloak and CloudNativePG operators are NOT installed by this chart. They must already be
installed before this chart is deployed — it renders the `Keycloak` and `KeycloakRealmImport` custom resources directly,
and a plain `helm install` fails on the missing CRDs otherwise. Use the `install-operators` chart for the operator
install (see the `examples/maas-umbrella` README for the full two-install flow).

The chart declares an OCI dependency on the published `postgres-cluster` chart of this repository
(`oci://ghcr.io/jharmison-redhat/helm-charts`), pinned `>=0.1.0 <0.2.0` and condition-gated. Run `helm dependency build`
before installing, or set `postgres-cluster.enabled: false` and supply external `db:` overrides.

This chart is designed to be consumed alongside the `keycloak-auth` chart (which configures OpenShift authentication
against the deployed Keycloak instance): an umbrella consumer composes both and provides the OIDC values once under
`global.oidc` — both charts read `global.oidc.{realm,clientId,secret}` through global propagation.

Usage
-----

The chart performs the following:

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
  oidc:
    realm: sso
    clientId: ocp-idp
    secret: <client-secret>

replicas: 1

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
        # Client entry string values may reference global.oidc.* via a
        # template string
        secret: "{{ .Values.global.oidc.secret }}"
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
- `global.oidc` — single-source OIDC settings shared with the `keycloak-auth` chart through global propagation; `realm`
  must match a `realms` map key and `clientId` must match a client name within it (defaults: `sso`, `ocp-idp`, empty
  secret). Setting `realm` to a name with no matching `realms` entry makes the issuer point at a realm that was never
  imported (no fail-fast)
- `namespace` — namespace for the Keycloak resources (empty by default, meaning the release namespace)
- `replicas` — number of Keycloak instances (default: `1`)
- `emailDomain` — domain used for realm-user email defaults (default: `example.com`)
- `postgres-cluster` — the backing PostgreSQL `Cluster` via the postgres-cluster dependency (enabled by default; named
  `keycloak-postgres` via `fullnameOverride`, 1 instance, 2Gi storage, modest resource requests/limits). The cluster
  uses the CloudNativePG default bootstrap — see the `keycloak.pg.fullname` helper in `templates/_helpers.tpl` for the
  naming convention the `db` block defaults rely on
- `db.host` / `db.database` / `db.credentialsSecret` — external database overrides used when the `postgres-cluster`
  dependency is disabled (all three are then required; by default they derive from the dependency's cluster)
- `realms` — map of realm definitions (see the commented example for clients, roles, users, groups, and representative
  `clientScopes` including the groups claim mapper). Client entry string values may reference `global.oidc.*` via a
  template string, rendered through tpl by the chart; realm map keys and client names stay literal

Secrets are never defaulted: realm admin/user passwords and realm client secrets must be provided through values. The
realm admin password is required at render time, as is the generated-users password when `generated.count > 0`. A realm
definition without an `admin` block renders without an admin user.
