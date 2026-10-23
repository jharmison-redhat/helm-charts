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
re-runs on upgrades the way the source GitOps sync-wave behavior did.

Values
------

Real defaults live in `values.yaml` and are overridable by the consumer. Notable keys:

- `operatorNamespace` — namespace the restart Job's RBAC is bound in; must match the namespace the `rhcl-operator`
  Subscription was installed into (default: `kuadrant-system`)
- `kuadrant.create` — render the `Kuadrant` custom resource and the restart plumbing (default: `true`)
- `kuadrant.namespace` — namespace for the `Kuadrant`/`Authorino` resources and the restart Job (default:
  `kuadrant-system`)
- `kuadrant.spec` — free-form `Kuadrant` spec passthrough (default enables observability)
- `authorino.overwrite` — render the `Authorino` custom resource (default: `true`)
- `authorino.serviceTls` — render the serving-cert-annotated authorization Service (default: `true`)
- `authorino.spec` — free-form `Authorino` spec passthrough (default enables listener TLS against
  `authorino-server-cert`)
- `global.toolsImage` — image for the restart Job container
