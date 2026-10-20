keycloak
========

A chart to deploy a Keycloak instance using the Red Hat Build of Keycloak operator (RHBK), installing the RHBK operator
via the `install-operators` chart dependency, an inline CloudNativePG `Cluster` behind an `enabled` toggle, and a
multi-realm map-driven `KeycloakRealmImport`.

The chart declares an OCI dependency on the published `install-operators` chart of this repository
(`oci://ghcr.io/jharmison-redhat/helm-charts/install-operators`, pinned `>=0.2.0 <0.3.0`), condition-gated by
`install-operators.enabled`. Run `helm dependency build` before installing. An umbrella consumer can disable operator
install by setting `install-operators.enabled: false` and manage the operator with its own `install-operators`
composition instead.

This chart is designed to be consumed alongside the `keycloak-auth` chart (which configures OpenShift authentication
against the deployed Keycloak instance): an umbrella consumer composes both and provides matching values.

Usage
-----

The chart performs the following:

- Installs the RHBK operator through the `install-operators` chart dependency (gated by `install-operators.enabled`,
  default `true`) with an `rhbk-operator` entry configured under `install-operators.operators`
- Deploys a `Keycloak` custom resource backed by PostgreSQL, using the CloudNativePG naming convention (`<name>-rw` for
  the read-write service and `<name>-app` for the credentials Secret)
- Creates an inline CloudNativePG `Cluster` behind the `postgresCluster.enabled` toggle; with it disabled, the database
  connection can be overridden with the `db` values (host, database, credentialsSecret)
- Renders a `KeycloakRealmImport` per entry in the `realms` map, with per-realm clients, roles, generated and explicit
  users, groups (explicit list or generated from roles), and `clientScopes` passthrough
- Exposes Keycloak through a reencrypt `Route` at `<name>.<wildcardDomain>`

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
global:
  wildcardDomain: apps.cluster.example.com
  toolsImage: image-registry.openshift-image-registry.svc:5000/openshift/tools:latest

replicas: 1

install-operators:
  enabled: true
  operators:
    rhbk-operator:
      channel: stable-v26.2

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

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `global.wildcardDomain` — wildcard domain under which Keycloak is exposed (`<name>.<wildcardDomain>`; default:
  `apps.cluster.example.com`)
- `global.toolsImage` — image for Helm hook Job containers (default: the in-cluster OpenShift tools image)
- `namespace` — namespace for the Keycloak resources (empty by default, meaning the release namespace)
- `replicas` — number of Keycloak instances (default: `1`)
- `emailDomain` — domain used for realm-user email defaults (default: `example.com`)
- `install-operators.enabled` / `install-operators.operators.rhbk-operator` — the RHBK operator install via the
  `install-operators` dependency (default: enabled, channel `stable-v26.2`, namespace `keycloak` with a matching
  OperatorGroup)
- `postgresCluster` — the inline CloudNativePG `Cluster` (enabled by default; name `keycloak-postgres`, 1 instance, 2Gi
  storage, modest resource requests/limits)
- `db.host` / `db.database` / `db.credentialsSecret` — external database overrides used when the inline cluster is
  disabled (defaults follow the CloudNativePG naming convention from `postgresCluster.name`)
- `realms` — map of realm definitions (see the commented example for clients, roles, users, groups, and the full
  `clientScopes` set including the groups claim mapper)

Secrets are never defaulted: realm admin/user passwords and realm client secrets must be provided through values. The
realm admin password is required at render time, as is the generated-users password when `generated.count > 0`. A realm
definition without an `admin` block renders without an admin user.
