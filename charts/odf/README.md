odf
===

A chart to deploy OpenShift Data Foundation: it renders the `StorageCluster` operand (named `ocs-storagecluster` — a
fixed singleton name, one per cluster) along with the optional `StorageSystem`, `OCSInitialization`, Ceph/NooBaa
visibility RBAC, console-plugin enablement, and storage-node labeling. The `odf-operator` itself is NOT installed by
this chart — see Prerequisites.

Configuration modes
-------------------

The chart supports and validates two mutually exclusive configurations. The values combination is checked at render
time; incoherent values fail with a diagnostic naming the mode.

- **Standalone MCGW** (`multiCloudGateway.reconcileStrategy: standalone`) — NooBaa-only: no Ceph capacity, no Ceph
  tooling, no `StorageSystem`. This is the **recommended** object-storage deployment for MaaS workloads. Requires
  `storageDeviceSets`, `disks`, and `monDataDirHostPath` empty, and `createStorageSystem`/`enableCephTools` false.
- **Full Ceph+MCGW** (reconcileStrategy unset or not `standalone`) — Ceph block/file/object storage plus NooBaa.
  Requires Ceph capacity: either `storageDeviceSets` or hostPath `disks` (with `monDataDirHostPath`) must be provided.

Standalone MCGW mode example
----------------------------

```yaml
enableCephTools: false
multiCloudGateway:
  reconcileStrategy: standalone
  dbStorageClassName: <storage-class-for-noobaa-db>
resources:
  noobaa-core:
    limits:
      cpu: 3
      memory: 4Gi
    requests:
      cpu: 2
      memory: 4Gi
  noobaa-db:
    limits:
      cpu: 3
      memory: 4Gi
    requests:
      cpu: 2
      memory: 4Gi
  noobaa-endpoint:
    limits:
      cpu: 3
      memory: 4Gi
    requests:
      cpu: 2
      memory: 4Gi
```

The storage class above is a placeholder — pick the cluster's storage class for the NooBaa database (for example, on an
AWS IPI cluster the default `gp3-csi` works).

Full Ceph+MCGW mode example
---------------------------

```yaml
monDataDirHostPath: /mnt/odf-storage
disks:
  - node: <worker-node-1>
  - node: <worker-node-2>
  - node: <worker-node-3>
```

or, with device sets against a shared storage class:

```yaml
storageDeviceSets:
  - name: ocs-deviceset
    dataPVCTemplate:
      spec:
        storageClassName: <storage-class-for-ceph-osds>
        accessModes:
          - ReadWriteOnce
        volumeMode: Block
        resources:
          requests:
            storage: 1Ti
    replica: 3
```

Ceph monitors require `monDataDirHostPath` when storage comes from labeled hostPath nodes; the `disks` entries label
those nodes (`cluster.ocs.openshift.io/openshift-storage: ""`). Device sets need no node labeling.

Live-validation caveat (standalone tiering)
-------------------------------------------

In standalone MCGW mode the NooBaa default backing store has no Ceph objectstore to fall back on, and the tiering
behavior (whether a usable default backing store materializes without cloud credentials) is being validated live on a
test cluster. OBC provisioning may require an explicit `BackingStore`/`BucketClass` — do not assume the default
`openshift-storage.noobaa.io` storage class provisions buckets in this mode until the live validation completes.

Prerequisites
-------------

The `odf-operator` is NOT installed by this chart. It must already be installed before this chart is deployed — the
chart renders the `StorageCluster` CR directly, and a plain `helm install` fails on the missing CRDs otherwise. Use the
`install-operators` chart for the operator install (see the `examples/maas-umbrella` README for the full two-install
flow):

```yaml
# install-operators values
operators:
  odf-operator:
    # stable-4.22 is the live packagemanifest default channel for odf-operator
    channel: stable-4.22
    namespace: openshift-storage
    operatorGroup:
      enabled: true
      targetNamespaces:
        - openshift-storage
```

The `openshift-storage` namespace must also exist before this chart is deployed — create it before install; the chart
does not render it. Wait for the operator deployments (and therefore the CRDs) to be ready before the first install of
this chart.

Install
-------

```sh
helm install <name> charts/odf -n openshift-storage \
  -f <mode-values>.yaml
```

Supply the mode-specific values with `-f` — one of the example files above (standalone MCGW or full Ceph+MCGW); the
render fails with a mode-naming diagnostic when the values combination is incoherent. Install the release into
`openshift-storage` — the console-plugin ClusterRoleBinding references the operator's ServiceAccount there.

Values
------

Real defaults live in `values.yaml`. Notable keys:

- `enableCephTools` — renders an `OCSInitialization` enabling Ceph tooling (default `true`; must be `false` in
  standalone MCGW mode)
- `createStorageSystem` — renders the `StorageSystem` CR (default `false`)
- `storageDeviceSets` — Ceph OSD device sets passed through to the `StorageCluster`
- `disks` — storage nodes to label for ODF (`{node: <name>}` entries)
- `monDataDirHostPath` — host path for Ceph monitor data on labeled nodes (required in full Ceph+MCGW mode when `disks`
  is the only capacity source)
- `enableConsolePlugin` — runs a Job enabling the `odf-console` plugin
- `resources` — per-component resource overrides on the `StorageCluster`
- `cephConfig` — passthrough to the `StorageCluster`'s `managedResources.cephConfig` block
- `multiCloudGateway` — passed through to the `StorageCluster`'s `multiCloudGateway` block
- `resourceProfile` — the `StorageCluster` resource profile (default `balanced`)
- `rbac` — Ceph/NooBaa read-only visibility ClusterRoleBindings for an authenticated group
