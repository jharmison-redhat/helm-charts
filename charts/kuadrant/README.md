kuadrant
========

A chart to install and configure a basic Red Hat Connectivity Link deployment: the `Kuadrant` custom resource, an
`Authorino` instance with listener TLS, the OpenShift serving-certificate-backed authorization Service, and a restart
Job that waits for the Kuadrant operator's CSVs and restarts the operator pods so the operand controllers pick up their
configuration.

The Red Hat Connectivity Link operator is NOT installed by this chart. It must already be installed before this chart is
deployed — it renders the `Kuadrant` and `Authorino` custom resources directly, and a plain `helm install` fails on the
missing CRDs otherwise. Use the `install-operators` chart for the operator install (see the `examples/maas-umbrella`
README for the full two-install flow).

Usage
-----

The chart performs the following:

- Renders a `Kuadrant` custom resource (gated by `kuadrant.create`) with free-form `spec` passthrough
- Renders an `Authorino` custom resource (gated by `authorino.overwrite`) with listener TLS backed by the
  `authorino-server-cert` secret
- Renders the `authorino-authorino-authorization` Service annotated for an OpenShift serving certificate (gated by
  `authorino.serviceTls`)
- Renders a Namespace for the operand namespace when it differs from `operatorNamespace`
- Renders the restart plumbing: a ServiceAccount in the operand namespace, RoleBindings to `aggregate-olm-view` and
  `edit` in the operator namespace, the restart script as a ConfigMap, and the restart Job

The restart Job runs as a `post-install,post-upgrade` Helm hook (recreated on re-runs via `before-hook-creation`), so it
re-runs on upgrades the way the source GitOps sync-wave behavior did. Its waits are bounded: each operator's CSV must
report `phase=Succeeded` within 120 seconds, and the operator Deployment's rollout must complete within 300 seconds — a
timed-out wait fails the Job.

The release namespace is free — all resources, including the restart hook Job, live in the `kuadrant.namespace` operand
namespace (default `kuadrant-system`). Co-locating the release
(`helm install <name> charts/kuadrant -n kuadrant-system`) is tidy; the operand namespace must admit the root-running
restart Job. With default values the chart does not render the operand namespace — the `install-operators` ensure hook
creates it. The hook Jobs run as non-root — the platform's restricted SCCs assign the namespace's range UID to the
UID-less hook pods, which carry a restricted-compatible securityContext — so hardened namespaces work. The chart cannot
be installed into the `default` namespace: that namespace's configuration assigns no SCC, so the UID-less non-root proof
falls through to the stock image's root user and the hook pods are rejected.

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `operatorNamespace` — namespace the restart Job's RBAC is bound in; must match the namespace the `rhcl-operator`
  Subscription was installed into (default: `kuadrant-system`)
- `kuadrant.create` — render the `Kuadrant` custom resource and the restart plumbing (default: `true`)
- `kuadrant.namespace` — namespace for the `Kuadrant`/`Authorino` resources and the restart Job (default:
  `kuadrant-system`)
- `kuadrant.spec` — free-form `Kuadrant` spec passthrough (default enables observability)
- `authorino.overwrite` — render the `Authorino` custom resource (default: `true`); only applies when `kuadrant.create`
  is also true — a hands-off chart never overwrites the Authorino resource of an existing RHCL deployment
- `authorino.serviceTls` — render the serving-cert-annotated authorization Service (default: `true`); like the Authorino
  custom resource, it also depends on `kuadrant.create` and `authorino.overwrite` — a hands-off chart renders neither
- `authorino.spec` — free-form `Authorino` spec passthrough (default enables listener TLS against
  `authorino-server-cert`)
- `restart.csvReadyTimeoutSeconds` / `restart.csvReadyPollIntervalSeconds` / `restart.rolloutTimeoutSeconds` — bounded
  waits for the restart hook, in seconds: the per-operator CSV readiness deadline, the poll interval between readiness
  probes, and the `oc rollout status` timeout for the operator deployment (defaults: 120, 5, 300). The restart Job's
  `activeDeadlineSeconds` covers the hook's worst case: four operators waiting up to the CSV deadline each (4×120s), the
  rollout timeout, plus a 30s margin — 810s at the defaults
- `global.toolsImage` — image for the restart Job container
