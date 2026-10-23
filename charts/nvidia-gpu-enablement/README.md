nvidia-gpu-enablement
=====================

A chart to deploy the NodeFeatureDiscovery and NVIDIA GPU Operator custom resources: the `NodeFeatureDiscovery` instance
(with the PCI whitelist that surfaces GPUs in NFD labels), the GPU Operator's `ClusterPolicy`, the device-plugin
time-slicing ConfigMap (when time slices are enabled), and the DCGM Exporter dashboard for the OpenShift console.

The NFD and NVIDIA GPU Operator operators are NOT installed by this chart. They must already be installed before this
chart is deployed — it renders the `NodeFeatureDiscovery` and `ClusterPolicy` custom resources directly, and a plain
`helm install` fails on the missing CRDs otherwise. Use the `install-operators` chart for the operator install (see the
`examples/maas-umbrella` README for the full two-install flow).

Operator install values
-----------------------

Add these entries to the `operators` map when using the `install-operators` chart. The `namespaceLabels` carry the
privileged pod-security labels (the GPU operator's privileged workloads require them) and enable cluster monitoring in
the operator namespaces:

```yaml
operators:
  nfd:
    channel: stable
    namespace: openshift-nfd
    namespaceLabels:
      openshift.io/cluster-monitoring: "true"
      pod-security.kubernetes.io/audit: privileged
      pod-security.kubernetes.io/warn: privileged
    operatorGroup:
      enabled: true
      targetNamespaces:
        - openshift-nfd

  gpu-operator-certified:
    channel: v26.3
    namespace: nvidia-gpu-operator
    catalog: certified-operators
    namespaceLabels:
      openshift.io/cluster-monitoring: "true"
      pod-security.kubernetes.io/audit: privileged
      pod-security.kubernetes.io/warn: privileged
    operatorGroup:
      enabled: true
      targetNamespaces:
        - nvidia-gpu-operator
```

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `nfd.namespace` — namespace for the `NodeFeatureDiscovery` custom resource; must match the namespace the `nfd`
  Subscription was installed into (default: `openshift-nfd`)
- `nfd.operand` — NFD operand passthrough (service port, image, pull policy)
- `nfd.prunerOnDelete` / `nfd.topologyUpdater` — `NodeFeatureDiscovery` feature flags
- `nfd.configData` — NFD worker config, including the PCI device whitelist that surfaces GPUs in NFD labels
- `timeSlices` — number of GPU time slices (default: `1`, no time slicing). A value greater than 1 renders the
  device-plugin ConfigMap in the `nvidia-gpu-operator` namespace and makes the ClusterPolicy's
  `devicePlugin.config.name` point at it
- `gpuOperator.policy` — free-form `ClusterPolicy` spec passthrough; string values are template-rendered, so nested keys
  can reference chart values (for example `devicePlugin.config.name` referencing `timeSlices`). Includes the
  `nvidia.com/gpu` NoSchedule toleration for the operator's daemonsets by default
