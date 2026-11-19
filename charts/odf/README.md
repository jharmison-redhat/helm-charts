odf
===

A chart to deploy OpenShift Data Foundation: it renders the `StorageCluster` operand (named `ocs-storagecluster` — a
fixed singleton name, one per cluster) with Ceph/NooBaa visibility RBAC, console-plugin enablement, and storage-node
labeling applied by hook Jobs. The `odf-operator` itself is NOT installed by this chart — see Prerequisites.

Configuration modes
-------------------

The chart supports and validates two mutually exclusive configurations. The values combination is checked at render
time; incoherent values fail with a diagnostic naming the mode.

- **Standalone MCGW** (`multiCloudGateway.reconcileStrategy: standalone`) — NooBaa-only: no Ceph capacity and no Ceph
  tooling. This is the **recommended** object-storage deployment for MaaS workloads. Requires `storageDeviceSets`,
  `disks`, and `monDataDirHostPath` empty, and `enableCephTools` false.
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

Storage-node labeling
---------------------

Nodes pre-exist on the cluster (they may register late, e.g. freshly provisioned machines joining after the release was
applied), so the chart never renders Node manifests. Instead, when `disks` is non-empty, a post-install/post-upgrade
hook Job (`label-storage-nodes-set`) waits up to 300 seconds for every named Node to register, then sets the label
`cluster.ocs.openshift.io/openshift-storage: ""` on it, and a pre-delete hook Job (`label-storage-nodes-remove`) walks
the labels back on uninstall:

- The set hook labels a node only when the label is absent, recording a per-node proof annotation (`odf/label-proof`)
  when it does. A node already carrying the empty label value is a no-op. A node carrying a non-empty value on that
  label — a value this chart did not set — stops the hook loudly, naming the node and value, instead of overwriting it.
- The remove hook strips the label only where the proof annotation records that this chart set it, clearing the proof
  annotation with it. Foreign labels (including pre-existing empty values and non-empty values that overwrote this
  chart's label) are never touched. Nodes that no longer exist are skipped.
- The remove hook targets only the nodes named in the current `disks` list. A node dropped from `disks` in an earlier
  revision keeps its label and its `odf/label-proof` annotation indefinitely — remove them manually. The removal targets
  are the configured node names because discovering chart-labeled nodes across the cluster would require cluster-wide
  list permission the chart deliberately does not hold.
- This surface **adopts and labels pre-existing nodes**. For dedicated storage provisioned via MachineSets, the label
  belongs in the MachineSet template instead — adjusting a MachineSet does not relabel already-provisioned nodes, and
  MachineSet-managed labeling keeps the label in the same lifecycle as the machines.

Uninstall contract: the labels of the nodes named in the current `disks` list are walked back only under proof (above);
a node dropped from `disks` in an earlier revision keeps its label and proof annotation indefinitely, and emptying
`disks` removes the hook surface entirely so nothing walks back (remove any residual labels manually). The walk-back
targets are the configured node names because discovering chart-labeled nodes would require cluster-wide list permission
the chart deliberately does not hold. The `OCSInitialization` CR is never touched (see Prerequisites); the release-owned
`StorageCluster` and the console-plugin surface are removed with the release.

Standalone tiering limitations
------------------------------

In standalone MCGW mode the NooBaa default backing store has no Ceph objectstore to fall back on, and a usable default
backing store is not expected to materialize without cloud credentials in this mode. OBC provisioning may require an
explicit `BackingStore`/`BucketClass` — the default `openshift-storage.noobaa.io` storage class does not reliably
provision buckets in this mode without one.

Prerequisites
-------------

The `odf-operator` is NOT installed by this chart. It must already be installed before this chart is deployed — the
chart renders the `StorageCluster` CR directly, and a plain `helm install` fails on the missing CRDs otherwise. Use the
`install-operators` chart for the operator install; in the entry below, its ensure-namespaces hook creates the
operator's `namespace` when absent (see the `examples/maas-umbrella` README for the full two-install flow):

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

The `openshift-storage` namespace must also exist before this chart is deployed — the chart does not render it. The
two-install flow above creates it via install-operators' ensure-namespaces hook; otherwise, create it by hand. Wait for
the operator deployments (and therefore the CRDs) to be ready before the first install of this chart.

The operator auto-creates and manages the `OCSInitialization` CR (`ocsinit`, in the operator's namespace) — the chart
never renders, patches, or deletes it. The Ceph toolbox toggle (`enableCephTools`) is applied to the release-owned
`StorageCluster`'s `spec.enableCephTools` field, which the operator reconciles.

Install
-------

```sh
helm install <name> charts/odf -n openshift-storage \
  -f <mode-values>.yaml
```

Supply the mode-specific values with `-f` — one of the example files above (standalone MCGW or full Ceph+MCGW); the
render fails with a mode-naming diagnostic when the values combination is incoherent. The release namespace is free —
every namespaced object this chart renders pins `namespace: openshift-storage` outright (the StorageCluster is the
namespaced anchor that must live there, and the hook Jobs, their ConfigMaps, and their ServiceAccounts pin it too; the
ClusterRole/ClusterRoleBindings are cluster-scoped and carry no namespace), so any release
namespace works. `openshift-storage` itself is a natural choice and pre-exists through the operator install. The hook
Jobs run as non-root — the platform's restricted SCCs assign the namespace's range UID to the UID-less hook pods, which
carry a restricted-compatible securityContext — so hardened namespaces work. The chart cannot be installed into the
`default` namespace: that namespace's configuration assigns no SCC, so the UID-less non-root proof falls through to the
stock image's root user and the hook pods are rejected.

Upgrading from 0.1.x
--------------------

Chart 0.1.x rendered one partial Node manifest per `disks` entry and an `OCSInitialization` release manifest. The first
upgrade to 0.2.0 removes those release-owned objects: Helm deletes the Node objects the 0.1.x release owns (the nodes
re-register from their running machines, and the set hook re-labels them within its wait window), and the operator
recreates the `OCSInitialization` CR automatically.

Plan a maintenance window for that first upgrade: while a Node object is deleted and re-registers, pods bound to it are
eligible for garbage collection and can be deleted and rescheduled mid-window. If the deployment tooling honors
`helm.sh/resource-policy: keep` for upgrade-removed resources, pre-annotating the existing Nodes avoids their deletion
entirely.

A 0.1.x release with a non-empty `disks` list could only exist through GitOps/SSA ownership adoption — a plain
`helm install` fails on the pre-existing Node objects — so the churn primarily affects GitOps-managed installs.

Values
------

Real defaults live in `values.yaml`. Notable keys:

- `enableCephTools` — sets the `StorageCluster`'s `spec.enableCephTools` (the Ceph toolbox; default `true`; must be
  `false` in standalone MCGW mode)
- `storageDeviceSets` — Ceph OSD device sets passed through to the `StorageCluster`
- `disks` — storage nodes to label for ODF (`{node: <name>}` entries), applied by hook Jobs per the Storage-node
  labeling section
- `monDataDirHostPath` — host path for Ceph monitor data on labeled nodes (required in full Ceph+MCGW mode when `disks`
  is the only capacity source)
- `enableConsolePlugin` — runs a hook Job enabling the `odf-console` plugin on the platform Console operator object; the
  write is a resourceVersion-tested JSON patch that appends to the existing plugins list (or creates it atomically), so
  a concurrent writer's plugin entries are never lost
- `resources` — per-component resource overrides on the `StorageCluster`
- `cephConfig` — passthrough to the `StorageCluster`'s `managedResources.cephConfig` block
- `multiCloudGateway` — passed through to the `StorageCluster`'s `multiCloudGateway` block
- `resourceProfile` — the `StorageCluster` resource profile (default `balanced`)
- `rbac` — Ceph/NooBaa read-only visibility ClusterRoleBindings for an authenticated group
