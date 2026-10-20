maas-umbrella
===

This is an example umbrella chart (not published) that composes the published charts of this repository as subchart
dependencies with values overlays. It is deliberately placed outside `charts/` so the version-gated publishing script
never discovers it.

Composing charts from this repository
---

Each chart in this repository is subchart-friendly: values are namespaced under the chart's own key, charts support an
`enabled` toggle for conditional inclusion, and `global.*` passthrough compatibility is preserved. An umbrella chart
declares the published OCI charts as dependencies in its `Chart.yaml` and configures them entirely through values, as
`values.yaml` in this example demonstrates. Additional charts published from this repository can be composed as
dependencies the same way.

Running locally
---

Build the dependencies (Helm 3.8+ supports OCI dependencies; anonymous pulls work for public ghcr packages):

```
helm dependency build examples/maas-umbrella
```

Lint and render the example:

```
helm lint examples/maas-umbrella
helm template examples/maas-umbrella
```
