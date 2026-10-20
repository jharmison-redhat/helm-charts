maas-umbrella
===

This is an example harness (not a published chart) that composes the charts of this repository as subcharts, the way a
future consumer umbrella — such as the one that will replace the maas-code-assistant quickstart deployment — will. It
exists to battle-test subchart composition and to exercise the lint/render CI matrix; it is deliberately placed outside
`charts/` so the version-gated publishing script never discovers it.

How a consumer umbrella uses it
---

Each chart in this repository is subchart-friendly: values are namespaced under the chart's own key, charts support an
`enabled` toggle for conditional inclusion, and `global.*` passthrough compatibility is preserved. An umbrella consumer
declares the published OCI charts as dependencies in its `Chart.yaml` and configures them entirely through values, as
`values.yaml` in this harness demonstrates. Future charts (openshift-ai, kuadrant, keycloak, etc.) will slot in under
their own keys once they are published.

Running locally
---

Build the dependencies (Helm 3.8+ supports OCI dependencies; anonymous pulls work for public ghcr packages):

```
helm dependency build examples/maas-umbrella
```

Lint and render against the harness:

```
helm lint examples/maas-umbrella
helm template examples/maas-umbrella
```
