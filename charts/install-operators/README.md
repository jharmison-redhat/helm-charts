install-operators
===

A chart to install Operators on OpenShift from Operator Lifecycle Mangaer (OLM) v0, including Manual InstallPlan
approvals.

Usage
---

This chart allows you to install Operators from Operator Hub on OpenShift, using OLM v0 APIs (Subscription,
OperatorGroup, etc.) including the following features:

- Using `installPlanApproval: Manual` mode to prevent auto-updating
- Explicitly stating a `startingCSV` and having that version explicitly approved (without automatically approving
  updates)
- Specifying an array of `approveCSVs` to enable approving updates to specific versions, but not others (so you can be
  really specific and know the versions you want are all there will ever be)
- Picking other namespaces to install your operator in, whether using cluster-wide or namespace scopes
- Installing an operator Subscription using a Job to apply it, to prevent GitOps from trying to own the Subscription and
  reconciling it continuously (to enable behaviors like the `ingress` ClusterOperator's ownership of an OSSM
  Subscription without conflicts)
- Specifying an `env` array for a Subscription easily for an operator, to override default behavior.

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
operators:
  rhcl-operator: # pin to a very specific version, in a specific namespace, with an env var and all-namespaces RBAC
    channel: stable
    namespace: kuadrant-system
    ownSubscription: false
    installPlanApproval: Manual
    startingCSV: rhcl-operator.v1.3.4
    approveCSVs:
      - rhcl-operator.v1.3.4
    operatorGroup:
      enabled: true
    env:
      AUTH_SERVICE_TIMEOUT: 2s
  cloudnative-pg: # install an operator into openshift-operators, with Automatic updates, for all namespaces from an alternate catalog
    channel: stable-v1
    catalog: certified-operators
  rhods-operator: # install an operator into its intended namespace, with cluster-wide scope, and automatically install udpates from a specific channel
    channel: stable-3.4
    namespace: redhat-ods-operator
    operatorGroup:
      enabled: true
  leader-worker-set: # install an operator into a specific namespace, with namespace-wide scope only (especially useful for operators that don't support cluster-wide installation, like this one)
    channel: stable-v1.0
    namespace: openshift-lws-operator
    operatorGroup:
      enabled: true
      targetNamespaces:
        - openshift-lws-operator
```

Understanding what to put in values
---

The above example is great if you know what all those things mean, but it's probably useful to understand how OLM v0
resolves the information you're providing it, so that it is able to actually make the installations you request. Also,
we keep referring to "OLM v0", so it's good to know that it's in the process of being replaced with
[OLM v1](https://www.redhat.com/en/blog/announcing-olm-v1-next-generation-operator-lifecycle-management). The obvious
place to point for learning OLM v0 is the
[documentation](https://olm.operatorframework.io/docs/concepts/olm-architecture/). It does somewhat gloss over how the
Catalog Operator behaves, though, so here's the essence of it as a crash course:

- OpenShift includes `CatalogSource` objects by default, pointing to four Operator Package Manager (OPM) images. These
  OPM images expose an API that allows enumerating OLM packages and the data necessary for OLM to act on these packages.
- The Catalog Operator will unpack bundles from these OPM catalogs, populating an API in OpenShift called the
  `PackageManifest`. You can see every operator in your cluster's catalogs with `oc get packagemanifest`.
- This is the API that populates the OperatorHub cards (or the Operator cards in the Software Catalog in newer versions
  of OpenShift, where other artifacts like Helm Charts are mixed in). The human-friendly names, short descriptions,
  provider, etc. are all derived from fields reflected in this API. Even the icon is from the bundle that got unpacked
  to populate this API, though it's not added in the PackageManifest itself. in the catalog are populated from fields in
  this API.
- When you click through to install an Operator from a card on the catalog, the UI is pre-populated with a channel and
  version. The channel that's selected was listed in the PackageManifest object under `status.defaultChannel`, and the
  version that's prepopulated is from that channel's `currentCSV` field.
- On the next page, the operative things for this chart, mostly drawn from the PackageManifest are:
  - Installation mode (drawn from the `installModes` in a given CSV, in priority order of the ones supported)
  - Installed Namespace (drawn from the `operatorframework.io/suggested-namespace` annotation in a given CSV, defaulting
    to `openshift-operators` if not specified)
  - Update approval (always defaults to "Automatic")
  - You might see a "Console plugin" option, depending on which operator you've picked. This is unrelated to the
    resources managed by this Chart, but is drawn from the optional `console.openshift.io/plugins` annotation on the
    CSV.

Let's map an example from a PackageManifest in the cluster to some values. If you run
`oc get packagemanifest advanced-cluster-management -oyaml`, we'll build some values to install it. The output at the
time of writing on OpenShift 4.22, trimmed down to what we care about, looks like this:

```
apiVersion: packages.operators.coreos.com/v1
kind: PackageManifest
metadata:
  labels:
    catalog: redhat-operators
    catalog-namespace: openshift-marketplace
    operatorframework.io/arch.amd64: supported
    operatorframework.io/arch.arm64: supported
    operatorframework.io/arch.ppc64le: supported
    operatorframework.io/arch.s390x: supported
    operatorframework.io/os.linux: supported
    provider: Red Hat
    provider-url: ""
  name: advanced-cluster-management
  namespace: default
spec: {}
status:
  catalogSource: redhat-operators
  catalogSourceDisplayName: Red Hat Operators
  catalogSourceNamespace: openshift-marketplace
  catalogSourcePublisher: Red Hat
  channels:
  - currentCSV: advanced-cluster-management.v2.16.2
    currentCSVDesc:
      [... snip ...]
    entries:
    - name: advanced-cluster-management.v2.16.2
      version: 2.16.2
    - deprecation:
        message: |
          ACM 2.16.1 contains a critical issue and should not be installed.
          Existing installations should upgrade to 2.16.2 or later.
          Please contact Red Hat support if you have questions.
      name: advanced-cluster-management.v2.16.1
      version: 2.16.1
    - name: advanced-cluster-management.v2.16.0
      version: 2.16.0
    name: release-2.16
  - currentCSV: advanced-cluster-management.v2.17.0
    currentCSVDesc:
      annotations:
        alm-examples: '[{"apiVersion": "operator.open-cluster-management.io/v1", "kind":
          "MultiClusterHub", "metadata": {"name": "multiclusterhub", "namespace":
          "open-cluster-management"}, "spec": {}}]'
        capabilities: Seamless Upgrades
        categories: Integration & Delivery
        certified: "true"
        createdAt: "2026-06-15T18:56:28Z"
        description: Advanced provisioning and management of OpenShift and Kubernetes
          clusters
        features.operators.openshift.io/cnf: "false"
        features.operators.openshift.io/cni: "false"
        features.operators.openshift.io/csi: "false"
        features.operators.openshift.io/disconnected: "true"
        features.operators.openshift.io/fips-compliant: "true"
        features.operators.openshift.io/proxy-aware: "true"
        features.operators.openshift.io/tls-profiles: "false"
        features.operators.openshift.io/token-auth-aws: "false"
        features.operators.openshift.io/token-auth-azure: "false"
        features.operators.openshift.io/token-auth-gcp: "false"
        olm.properties: '[{"type": "olm.maxOpenShiftVersion", "value": "5.0"}]'
        olm.skipRange: '>=2.15.0 <2.17.0'
        operatorframework.io/initialization-resource: '{"apiVersion":"operator.open-cluster-management.io/v1",
          "kind":"MultiClusterHub","metadata":{"name":"multiclusterhub","namespace":"open-cluster-management"},
          "spec": {}}'
        operatorframework.io/suggested-namespace: open-cluster-management
        operators.openshift.io/infrastructure-features: '["disconnected", "proxy-aware",
          "fips"]'
        operators.openshift.io/must-gather-image: registry.redhat.io/rhacm2/acm-must-gather-rhel9@sha256:1a4f21543e04c163a27ffba289dcab16464b014c20a625189c8e878c8534467d
        operators.openshift.io/valid-subscription: '["OpenShift Platform Plus", "Red
          Hat Advanced Cluster Management for Kubernetes"]'
        repository: https://github.com/open-cluster-management/multiclusterhub-operator
        support: Red Hat
      apiservicedefinitions: {}
      customresourcedefinitions:
        owned:
        - description: |-
            MulticlusterHub defines the configuration
            for an instance of a multicluster hub, a central point for managing multiple
            Kubernetes-based clusters. The deployment of multicluster hub components
            is determined based on the configuration that is defined in this resource.
          displayName: MultiClusterHub
          kind: MultiClusterHub
          name: multiclusterhubs.operator.open-cluster-management.io
          version: v1
      description: "Red Hat Advanced Cluster Management for Kubernetes provides the
        [... snip ...]
      displayName: Advanced Cluster Management for Kubernetes
      installModes:
      - supported: true
        type: OwnNamespace
      - supported: false
        type: SingleNamespace
      - supported: false
        type: MultiNamespace
      - supported: false
        type: AllNamespaces
      keywords:
      - acm
      - advanced-cluster-management
      - install
      - multiclusterhub
      - rhacm
      links:
      - name: Documentation
        url: https://access.redhat.com/documentation/en-us/red_hat_advanced_cluster_management_for_kubernetes/2.17/
      maintainers:
      - email: acm-contact@redhat.com
        name: Red Hat
      maturity: stable
      provider:
        name: Red Hat
      relatedImages:
        [... snip ...]
      version: 2.17.0
    entries:
    - name: advanced-cluster-management.v2.17.0
      version: 2.17.0
    name: release-2.17
  defaultChannel: release-2.17
  packageName: advanced-cluster-management
  provider:
    name: Red Hat
```

The `status.channels` array has two entries, and the `status.defaultChannel` field selects `release-2.17` as the
default. This channel has one CSV entry, and its name is `advanced-cluster-management.v2.17.0`. This CSV supports
"OwnNamespace" mode only, without "AllNamespaces" support. This generally means that the operator is expecting to be
installed in the suggested namespace, so we should be sure to use that - in this case `open-cluster-management`. Because
it doesn't support AllNamespaces, we need to make sure that our Namespace's OperatorGroup is targeting just that one
namespace. Also note the `status.catalogSource` is `redhat-operators` - this means this is a first-party Red Hat
provided operator.

To install this operator, with automatic updates, on the default channel, we would construct values like this:

```
operators:
  advanced-cluster-management:
    channel: release-2.17
    namespace: open-cluster-management
    operatorGroup:
      enabled: true
      targetNamespaces:
        - open-cluster-management
```

If we wanted to pin specifically to the 2.17.0 version, and not allow any automatic upgrades (making upgrades depend on
an update to the chart), we would use these values:

```
operators:
  advanced-cluster-management:
    channel: release-2.17
    namespace: open-cluster-management
    installPlanApproval: Manual
    startingCSV: advanced-cluster-management.v2.17.0
    approveCSVs:
      - advanced-cluster-management.v2.17.0
    operatorGroup:
      enabled: true
      targetNamespaces:
        - open-cluster-management
```

In the future, if we wanted to approve a pending upgrade to v2.17.1, we would just add it to the `approveCSVs` array and
run a `helm upgrade`.
