leaderworkerset
===============

A chart to deploy the `LeaderWorkerSetOperator` cluster custom resource, enabling the LeaderWorkerSet operator once it
is installed. No operand resources are rendered — LeaderWorkerSet instances are created at runtime (for example by
KServe when deploying LLM inference services).

The LeaderWorkerSet operator is NOT installed by this chart. It must already be installed before this chart is deployed
— the `LeaderWorkerSetOperator` CRD exists only after the operator publishes it, and a plain `helm install` fails on the
missing CRD otherwise. Use the `install-operators` chart for the operator install (see the `examples/maas-umbrella`
README for the full two-install flow).

Operator install values
-----------------------

The operator installs into its own namespace with namespace-wide scope only (it does not support cluster-wide
installation). Add this entry to the `operators` map when using the `install-operators` chart:

```yaml
operators:
  leader-worker-set:
    channel: stable-v1.0
    namespace: openshift-lws-operator
    operatorGroup:
      enabled: true
      targetNamespaces:
        - openshift-lws-operator
```

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. The `spec` block is passed through to the
`LeaderWorkerSetOperator` resource: `spec.managementState`, `spec.logLevel`, and `spec.operatorLogLevel` (defaults:
`Managed`, `Normal`, `Normal`).
