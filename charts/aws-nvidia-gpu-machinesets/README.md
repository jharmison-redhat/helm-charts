aws-nvidia-gpu-machinesets
==========================

A chart to manage OpenShift MachineSets for NVIDIA GPU worker instances on AWS: one `MachineSet` per configured
availability zone, plus — when autoscaling is enabled — one `MachineAutoscaler` per zone and a `ClusterAutoscaler` with
`nvidia.com/gpu` resource limits.

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

The MachineSet and MachineAutoscaler templates carry no namespace — they install into the target namespace, which must
be `openshift-machine-api` because the referenced credentials and user-data secrets live there. The ClusterAutoscaler is
cluster-scoped.

Replica semantics
-----------------

The chart follows a one-AZ-at-a-time policy:

- **Autoscaling disabled** (default): only the chosen availability zone — `aws.az` if set, otherwise the last entry of
  `aws.azs` — renders with `replicas: desiredReplicas`; every other zone renders with `replicas: 0`. Scale out by
  re-running `helm upgrade` with more replicas or by setting `aws.az` to another zone.
- **Autoscaling enabled** (`autoscaling.enabled: true`): every zone renders a `MachineAutoscaler` whose
  `minReplicas`/`maxReplicas` are the chart-level `autoscaling.minReplicas`/`maxReplicas` divided by the zone count (max
  plus one), and the per-zone MachineSets render without a `replicas` field so the autoscaler owns it. The
  `ClusterAutoscaler` carries the total GPU resource limits (`nvidia.com/gpu` min/max) and the `scaleDown` tuning block.

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

- `instanceType` — the GPU EC2 instance type (default: `g6e.2xlarge`)
- `desiredReplicas` — GPU nodes launched in the chosen zone when autoscaling is disabled (default: `1`)
- `baseName` — middle segment of the MachineSet names (default: `gpu-worker`)
- `rootDeviceSizeGB` — gp3 root volume size in GB (default: `200`)
- `taints` / `nodeLabels` — applied to every rendered MachineSet's node template
- `autoscaling` — the MachineAutoscaler/ClusterAutoscaler surface, including the `scaleDown` tuning block passed through
  to the ClusterAutoscaler
- `aws.az` — optional override for which zone carries `desiredReplicas`
