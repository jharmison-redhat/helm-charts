aws-nvidia-gpu-machinesets
==========================

A chart to manage OpenShift MachineSets for NVIDIA GPU worker instances on AWS: one `MachineSet` per configured
availability zone, plus — when autoscaling is enabled — one `MachineAutoscaler` per zone and a hook-configured
`ClusterAutoscaler` with `nvidia.com/gpu` resource limits.

The MachineSets reuse the cluster's existing worker infrastructure by tag: they select the `<infraID>-node` and
`<infraID>-lb` security groups, the `<infraID>-subnet-private-<az>` subnets, and the `<infraID>-worker-profile` IAM
instance profile, and tag the instances with `kubernetes.io/cluster/<infraID>: owned`. The `aws-cloud-credentials` and
`worker-user-data` secret references point at the secrets the installer created in `openshift-machine-api`. Nodes get
the `nvidia.com/gpu` NoSchedule taint and the `node-role.kubernetes.io/gpu` label by default, and a gp3 root volume
sized by `rootDeviceSizeGB`.

**The rendered MachineSets create real, billable GPU instances.** With autoscaling disabled, the chosen availability
zone (see `aws.az`) launches `desiredReplicas` GPU nodes as soon as the MachineSet reconciles; the other zones sit at
`replicas: 0`.

Required per-cluster values
---------------------------

These values are cluster-specific and the chart refuses to render until they are set:

- `cluster.infraID` — the cluster's infrastructure ID. It constructs the MachineSet names (`<infraID>-gpu-worker-<az>`)
  and every AWS resource name listed above. To find it, look at the names of the existing worker MachineSets in
  `openshift-machine-api` — they carry the infraID (for example, on a cluster with infra ID `cluster-9w6jz` the worker
  MachineSets are named `cluster-9w6jz-worker-<az>`).
- `aws.region` — the region the cluster runs in (the MachineSet placement region).
- `aws.azs` — the availability zones to cover; one MachineSet is rendered per entry.
- `aws.ami` — the RHCOS worker AMI ID for the cluster's region. Copy it from an existing worker MachineSet's
  providerSpec (`.spec.template.spec.providerSpec.value.ami.id`).

Install
-------

```sh
helm install <name> charts/aws-nvidia-gpu-machinesets -n openshift-machine-api \
  --set cluster.infraID=<infraID> \
  --set aws.region=<region> \
  --set aws.azs='{<az1>,<az2>}' \
  --set aws.ami=<ami-id>
```

The release namespace is free — every namespaced object this chart renders pins `namespace: openshift-machine-api`
outright: the MachineSets and MachineAutoscalers must live there because the referenced credentials and user-data
secrets the installer created live there, and the hook Jobs, their ConfigMap, and their ServiceAccount pin it too (the
ClusterRole/ClusterRoleBinding are cluster-scoped, as is the ClusterAutoscaler the hook pair configures). Any release
namespace works; `openshift-machine-api` itself is a natural choice and pre-exists on every cluster. The hook Jobs run
as non-root — the platform's restricted SCCs assign the namespace's range UID to the UID-less hook pods, which carry a
restricted-compatible securityContext — so hardened namespaces work. The chart cannot be installed into the `default`
namespace: that namespace's configuration assigns no SCC, so the UID-less non-root proof falls through to the stock
image's root user and the hook pods are rejected.

Replica semantics
-----------------

The chart follows a one-AZ-at-a-time policy:

- **Autoscaling disabled** (default): only the chosen availability zone — `aws.az` if set, otherwise the last entry of
  `aws.azs` — renders with `replicas: desiredReplicas`; every other zone renders with `replicas: 0`. Scale out by
  re-running `helm upgrade` with more replicas or by setting `aws.az` to another zone.
- **Autoscaling enabled** (`autoscaling.enabled: true`): every zone renders a `MachineAutoscaler` whose
  `minReplicas`/`maxReplicas` distribute the chart-level `autoscaling.minReplicas`/`maxReplicas` totals exactly across
  the zones — the first `total mod zoneCount` zones take `floor(total/zoneCount) + 1` and the rest take
  `floor(total/zoneCount)`, independently for min and max, so the per-zone bounds always sum to the requested totals
  (see the example below) — and the per-zone MachineSets render without a `replicas` field so the autoscaler owns it.
  The `ClusterAutoscaler` hook carries the total GPU resource limits (`nvidia.com/gpu` min/max) and the `scaleDown`
  tuning block.

With two zones, chart-level `minReplicas: 4` and `maxReplicas: 7` render as:

| Zone    | minReplicas | maxReplicas |
| ------- | ----------- | ----------- |
| zone-a  | 2           | 4           |
| zone-b  | 2           | 3           |
| **sum** | **4**       | **7**       |

A truncating division (`floor(total/zones)`) would have lost one GPU from the max total, which is why the remainder
zones take the extra replica. `maxNodesTotal` on any pre-existing ClusterAutoscaler must encompass these sums.

The MachineAutoscaler CRD enforces `maxReplicas >= 1` per zone, so `autoscaling.maxReplicas` must be at least the number
of entries in `aws.azs` — with fewer, the exact-sum distribution would render `maxReplicas: 0` zones the API server
rejects. The render fails with a diagnostic naming `autoscaling.maxReplicas` and `aws.azs` below that minimum.

ClusterAutoscaler ownership
---------------------------

The `ClusterAutoscaler/default` object is a cluster singleton that typically already exists (the console installer and
many administrators configure it). It is therefore **never a release manifest** in this chart — `helm uninstall` never
deletes a pre-existing instance (an instance this chart's hook created **is** deleted on uninstall, under the recorded
creation proof). It is configured by two hook Jobs (gated on `autoscaling.enabled`) that modify only the fields this
chart manages: `spec.podPriorityThreshold`, `spec.scaleDown`, and the `nvidia.com/gpu` entry of
`spec.resourceLimits.gpus`. Anything else on the object — `maxNodesTotal`, the `cores`/`memory` limits — is never read
or written, and `gpus` entries for other accelerator types are never modified in value (the whole `gpus` array is read
and rewritten as a unit on each gpus change), so an existing autoscaler configuration coexists with this chart.

The hooks write conditionally (read-modify-write carrying `metadata.resourceVersion` with a bounded retry on conflicts)
and record what they change in an `aws-nvidia-gpu-machinesets/proof` annotation on the object:

- **Object absent** — the set hook creates it with this chart's full spec and a proof annotation recording the creation.
  The remove hook (pre-delete) deletes it on uninstall **only** under that creation proof.
- **Object present** — before changing a managed field, the set hook records the field's pre-existing value in the proof
  annotation (a recorded `null` means the field was absent); it then sets this chart's value. Values already equal to
  this chart's are left alone, and fields that never change are never recorded.
- **Uninstall with a pre-existing object** — the remove hook restores every recorded prior (a recorded `null` removes
  the field) and clears the proof annotation. The `gpus` array is rebuilt only when the proof records a `gpuEntry`: a
  recorded prior replaces this chart's `nvidia.com/gpu` entry in place, a recorded `null` removes the entry, and an
  unrecorded `gpuEntry` — the entry already equaled this chart's values when the set hook first saw it — leaves the
  array untouched. The whole `gpus` array is rewritten on each gpus change because the CRD declares no list type for it
  (atomic by default): foreign accelerator entries are preserved in place, but concurrent edits to that one array are
  only protected by the resourceVersion conflict retry.
- **Uninstall without a proof annotation** — the object was never configured by this chart (or the annotation was
  removed); the remove hook leaves it exactly as it is.

Values the user changes between upgrades are recorded the first time the hook observes them differ; the earliest
observed pre-chart state always wins, so repeated installs and upgrades do not accumulate records. The record is a
coexistence contract, not a backup: if the object's managed fields are edited by hand between install and uninstall,
uninstall restores the recorded priors over those edits.

**Disabling autoscaling after it was enabled leaves the chart's ClusterAutoscaler footprint in place.** With
`autoscaling.enabled: false`, neither hook renders — Helm does not run pre-delete hooks on upgrade, and a release that
stopped rendering the hooks has nothing to walk its changes back, so on upgrade or even before `helm uninstall` the
managed values (`spec.podPriorityThreshold`, `spec.scaleDown`, the `nvidia.com/gpu` gpus entry) and the proof annotation
persist indefinitely. Either set `autoscaling.enabled: true` again (the next upgrade's set hook re-manages the fields
and a later uninstall with it enabled runs the remove hook), or clean up manually: re-set or unset the managed fields on
`clusterautoscalers.autoscaling.openshift.io/default` (preserving any foreign `gpus` entries when editing the array) and
remove the `aws-nvidia-gpu-machinesets/proof` annotation — for example with `oc edit`.

GPU instance type
-----------------

The default `instanceType` is `g6e.2xlarge` (NVIDIA L40S). Pick the instance type that matches your workload — the
[AWS EC2 instance type catalog](https://aws.amazon.com/ec2/instance-types/#Accelerated_Computing) lists the
accelerated-computing families. Verify the chosen type is available in your region and availability zones, and that your
account quota covers it before scaling beyond a single node.

Relationship to other charts
----------------------------

- `nvidia-gpu-enablement` renders the software stack: the `NodeFeatureDiscovery` and GPU Operator `ClusterPolicy` custom
  resources. Its operators are NOT installed by that chart — use the `install-operators` chart (the `nfd` and
  `gpu-operator-certified` entries; see the `examples/maas-umbrella` README for the two-install flow).
- This chart provides the GPU capacity itself. Deploy it first; the GPU Operator's workloads tolerate the
  `nvidia.com/gpu` taint by default, and user workloads need matching tolerations to schedule onto these nodes.

Values
------

Real defaults live in `values.yaml`. Notable keys beyond the required ones:

- `global.toolsImage` — image for the ClusterAutoscaler hook Job containers (the chart-local registry name keeps
  installs working on disconnected clusters that mirror `openshift/tools`)
- `instanceType` — the GPU EC2 instance type (default: `g6e.2xlarge`)
- `desiredReplicas` — GPU nodes launched in the chosen zone when autoscaling is disabled (default: `1`)
- `baseName` — middle segment of the MachineSet names (default: `gpu-worker`)
- `rootDeviceSizeGB` — gp3 root volume size in GB (default: `200`)
- `taints` / `nodeLabels` — applied to every rendered MachineSet's node template
- `autoscaling` — the MachineAutoscaler/ClusterAutoscaler surface; `maxReplicas` must be at least the `aws.azs` count
  when enabled (the render fails below the CRD minimum of 1 per zone), and `scaleDown` is passed through to the
  hook-configured ClusterAutoscaler (omitted from the object entirely when empty)
- `aws.az` — optional override for which zone carries `desiredReplicas`; must be a member of `aws.azs` when set (the
  render fails otherwise), and defaults to the last `aws.azs` entry when unset

RBAC note: the hook Jobs hold get/patch/delete on exactly the `default` ClusterAutoscaler, but **create on
`clusterautoscalers` is unscoped** — RBAC cannot scope create by resource name, and the set hook must create the
singleton when it is absent. This is the one grant broader than the hook's write surface.
