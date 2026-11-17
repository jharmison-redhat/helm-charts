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
- Maintaining each operator's Subscription through hook Jobs rather than release ownership: a
  `post-install,post-upgrade` hook converges the chart-managed fields (`source`, `sourceNamespace`, `channel`,
  `installPlanApproval`, `startingCSV`, and the `env` list) to the configured values on every install and upgrade, and a
  `pre-delete` hook removes the Subscription on uninstall only when the chart created it (a Subscription that
  pre-existed is left in place). `ownSubscription: true` opts a specific operator into a release-owned Subscription
  instead, which lets a GitOps tool own and continuously reconcile it — at the cost of those tools reporting the
  Subscription's health and readiness as the release's, and of ordering issues when OLM-created resources (CSVs,
  InstallPlans) are not yet established during the first sync.
- Ensuring the operator's namespace through a `pre-install,pre-upgrade` hook rather than release ownership: an
  explicitly-configured `namespace` that does not exist is created with the chart's provenance labels and the entry's
  `namespaceLabels`/`namespaceAnnotations`; one that exists is merged additively with those keys only, and the hook
  refuses to overwrite any of them with a foreign value. The hook runs over the unique namespace set of the enabled
  entries and runs before Subscriptions apply, so the namespaces and their RBAC exist by the time the Subscriptions
  land. Two entries sharing a namespace must declare identical `namespaceLabels`/`namespaceAnnotations` values — a
  conflict fails the render naming both entries and the key. The shared `openshift-operators` default namespace is never
  created or labeled by this chart. The hook's ServiceAccount, ClusterRole, ClusterRoleBinding, and script ConfigMap are
  hooks themselves: they are created at hook weight -10 (before the weight-0 Job runs), removed after each successful
  run, and re-created on the next install or upgrade, so nothing in the hook surface is release-tracked — the Job alone
  is retained between a run and its retry via `before-hook-creation`.
- Specifying an `env` array for a Subscription easily for an operator, to override default behavior.
- Toggling any operator off entirely with `enabled: false` (default is `true` when unset), so its OperatorGroup,
  Subscription, and approval resources are not rendered. A Namespace is never rendered regardless of the toggle —
  operator namespaces are ensured by hook rather than owned by the release.
- Manual InstallPlan approval Jobs run as Helm `post-install,post-upgrade` hooks (recreated on re-runs via
  `before-hook-creation`); under ArgoCD these map to `PostSync` and behave the same way.

An example `values.yaml` you could use to actuate some of these features would look like this:

```yaml
operators:
  rhcl-operator: # pin to a very specific version, in a specific namespace, with an env var and all-namespaces RBAC
    channel: stable
    namespace: kuadrant-system
    ownSubscription: true
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

(`ownSubscription: true` above opts the rhcl-operator into a release-owned Subscription; every other operator's
Subscription is maintained by the chart's hook Jobs.)

Release namespace
---

The release namespace hosts the subscription-management and namespace-ensure hook Jobs: the operator namespaces are
ensured by hook, never release-owned, and uninstall never removes them. When `installPlanApproval: Manual` or
`approveCSVs` are set, the InstallPlan approval Job renders in each operator namespace instead, running the same
root-running tools image — those namespaces are created fresh by the ensure hook, so they admit it. Co-locating the
release with an operators' namespace suits single-namespace topologies (for example
`helm install <name> charts/install-operators -n openshift-storage`); otherwise use any namespace that exists at install
time and does not enforce restricted Pod Security Admission, which the hook Jobs' root-running tools image trips in
hardened namespaces (e.g. `default`). Installing into a namespace created by `--create-namespace` or pre-created
manually takes the ensure hook's merge path: the chart's labels land, and the chart's creation-proof annotation exists
only on namespaces the hook creates itself (informational only). Pin `operatorGroup.targetNamespaces` on real installs —
the rendered default (no targets) is an OLM global OperatorGroup, which conflicts with an existing global group.

Uninstall contract
---

- **Namespaces persist.** Operator namespaces are never release manifests, so `helm uninstall` never deletes them —
  including namespaces this chart created. Labels and annotations the hook merged into a pre-existing namespace, or
  wrote into one it created, stay after the release is removed.
- **Hook-created Subscriptions are removed.** The `pre-delete` hook deletes a Subscription only when it carries the
  chart's `install-operators/proof` creation-proof annotation; a Subscription that existed before the release (or was
  created outside it) is left in place with whatever field values the chart last converged.
- **Release-owned Subscriptions are removed** with the release, as any release manifest is.

Upgrading from 0.2.x
---

- **Namespaces are adopted, not deleted.** 0.2.x rendered each operator's Namespace as a release manifest, so a plain
  upgrade would delete it — Helm removes old-manifest objects absent from the new manifest, and a Namespace deletion
  cascades to everything inside it. On the upgrade path the pre-upgrade namespace hook runs before Helm's deletion pass
  and stamps `helm.sh/resource-policy: keep` onto every managed namespace carrying the release's provenance labels
  (`app.kubernetes.io/name: install-operators` plus a matching `app.kubernetes.io/instance`), as part of the same write
  that merges the chart's metadata. From that point on the namespace is not release-owned, and `helm uninstall` no
  longer removes it — the uninstall contract above applies in full.
- **Subscriptions flip to hook ownership by default.** In 0.2.x each Subscription was a release manifest. Under the
  0.3.0 default (`ownSubscription: false`) the Subscription is maintained by the chart's hook Jobs, so the first upgrade
  deletes the previously release-owned Subscription along with the old manifest and the post-upgrade set hook recreates
  it once (OLM then re-resolves the operator's CSVs from the Subscription). Set `ownSubscription: true` on an operator
  to preserve release ownership for its Subscription instead.

RBAC
---

The hook service accounts are scoped to exactly what their scripts do: `get`/`patch`/`delete` on the named Subscription
in its namespace, and `get`/`patch` on the named Namespaces. Kubernetes RBAC cannot scope `create` by resource name, so
creating a Subscription or Namespace requires one unscoped `create` grant per hook — the single exception to the
name-scoping rule, and the reason the chart creates objects only when they are absent rather than re-creating them.

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

With `installPlanApproval: Manual`, OLM resolves newer versions against the Subscription but leaves their InstallPlans
pending until approved. To approve an upgrade to a specific version (for example v2.17.1), add it to the `approveCSVs`
array and run a `helm upgrade` — the post-upgrade approval hook re-runs and approves the pending InstallPlan once its
CSV is listed.
