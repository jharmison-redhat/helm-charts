postgres-cluster
================

A chart to deploy and manage a PostgreSQL cluster with the CloudNativePG operator: a `Cluster` custom resource with
initdb/recovery bootstrap, optional per-database `Database` custom resources and basic-auth credentials Secrets,
`ScheduledBackup` resources, and a cert-manager `Certificate` covering the cluster's services.

The chart is self-contained (no chart dependencies) and renders standalone, with values namespaced under the
`postgres-cluster` key when consumed as a subchart.

Usage
-----

The chart performs the following:

- Renders a CloudNativePG `Cluster` with `instances` replicas and the configured storage/resources. With `recovery` set,
  the cluster bootstraps from that recovery target; otherwise it bootstraps via initdb from the first `databases` entry,
  or via the operator's default bootstrap (creating the `app` database owned by `app` with the credentials in a
  `<name>-app` Secret) when no databases are specified
- Renders one `Database` custom resource per entry in the `databases` map, plus a basic-auth Secret named
  `<name>-<user>-<database>` when the entry provides a `password` (wired to a managed role)
- Renders one `ScheduledBackup` per entry in the `backups` map, and wires the Cluster's volume-snapshot backup
  configuration from the first entry
- Renders a cert-manager `Certificate` (and the TLS Secret placeholder) covering `<name>-<service>` DNS names for every
  service in `services`, when `certificate.enabled` is true

The rendered service names follow the CloudNativePG convention (`<name>-rw`, `<name>-app`); see the
`keycloak.pg.fullname` helper in the keycloak chart for the cross-chart naming contract built on these names.

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
nameOverride: keycloak-postgres

instances: 1

storage:
  size: 2Gi

backups:
  daily:
    schedule: "0 30 1 * * 6"
    method: volumeSnapshot
    ownerReference: self
    volumeSnapshot:
      className: csi-aws-vsc

databases:
  keycloak:
    user: keycloak
    password: <password>
```

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `nameOverride` / `fullnameOverride` — override the rendered resource names (empty by default, deriving from the
  release name and the chart name)
- `namespace` — namespace for the resources (empty by default, meaning the release namespace)
- `image` — PostgreSQL server image (empty by default, using the operator default)
- `instances` — number of PostgreSQL instances in the cluster (default: `2`)
- `storage` / `walStorage` — storage configuration passed through to the Cluster CR (default: 10Gi; an empty
  `storageClass` uses the cluster default StorageClass)
- `resources` — resource requests/limits for the cluster instances
- `services` — operator-created default services (default: `rw`, `ro`, `r`)
- `certificate` — the server `Certificate` (enabled by default; `issuerRef` defaults to a `ca-issuer` ClusterIssuer from
  cert-manager.io, and `clusterDomain` adds a suffix below the `.svc` DNS level). The `issuerRef` must resolve to a
  CA-capable issuer (CA, SelfSigned, or Vault): the referenced Secret doubles as `serverCASecret` and needs a `ca.crt`,
  which cert-manager only populates for CA-capable issuers — with an ACME issuer the Cluster breaks.
- `backups` — map of backup schedules (commented example; one `ScheduledBackup` per entry, `schedule` required per
  entry, only the `volumeSnapshot` method is implemented)
- `databases` — map of application databases (commented example; one `Database` CR per entry plus a basic-auth Secret
  when `password` is provided; with the map empty, the default bootstrap creates the `app` database)
- `recovery` — bootstrap recovery target; when set, it replaces the initdb bootstrap

Passwords are never defaulted: the `databases` entries' `password` field must be provided through values when a
basic-auth Secret is wanted.
