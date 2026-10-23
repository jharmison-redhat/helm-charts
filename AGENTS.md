# AGENTS.md

## Repo overview

Helm charts collection published as OCI charts from `charts/` (one chart per directory, depth-1) to
`oci://ghcr.io/jharmison-redhat/helm-charts`. `examples/` holds umbrella examples; `examples/maas-umbrella` is the
operand umbrella. CI lives in `.github/workflows/`: `lint-chart.yml` lints and renders every chart plus the
maas-umbrella example, and `publish-chart.yml` publishes version-bumped charts to ghcr.io on push to main.

## Chart dependencies — the guard that broke run 39966995703

Vendored `charts/<name>/charts/*.tgz` and `Chart.lock` are **gitignored**. Any chart with a `dependencies:` block
requires `helm dependency build` before `helm lint`, `helm template`, or `helm package`.

CRITICAL: verify `helm package` from a CLEAN checkout — never just the working tree. Simulate with
`git archive HEAD charts/<name> | tar -x` into a temp dir, then package there. Gitignored vendored deps in the working
tree mask missing-dependency failures that only surface in CI; this exact failure broke run 39966995703 (keycloak 0.3.0
failed `helm package` with "found in Chart.yaml, but missing in charts/ directory: postgres-cluster").

## Intra-repo dependency ordering

A chart depending on another chart in this repo requires the dependency chart to be published first. The publish matrix
runs jobs in parallel, so same-run publishes are a race — mitigated by `fail-fast: false`, a bounded retry on
`helm dependency build` inside publish-chart.yml, and manual dispatch as a fallback. When adding an intra-repo OCI
dependency: confirm the dependency chart is already published (or lands in the same push first), and bump BOTH the
dependency's version pin and the dependent's version in the same push.

## Versioning and push discipline

SemVer bump per functional change, `version:` and `appVersion` in lockstep. `chart_versions.sh` publishes a chart only
when its `Chart.yaml` `version:` changed. Use stacked commits with dedicated push points: push only when the repo is in
a consistent, publishable state (version bumps + dependent pins together). Charts referencing unpublished versions break
CI lint until the publish lands.

## Verification commands

```sh
helm lint charts/<name>
helm template charts/<name>
helm dependency build charts/<name>   # login required for OCI deps
echo "$TOKEN" | helm registry login ghcr.io -u <user> --password-stdin
# Clean-checkout package simulation:
git archive HEAD charts/<name> | tar -x -C /tmp/opencode/pkg-check && helm package /tmp/opencode/pkg-check/charts/<name>
```

The umbrella example under `examples/maas-umbrella` is never published — `chart_versions.sh` scans `charts/` only. Its
CI job 403s on `helm dependency build` until its pinned versions are published.
