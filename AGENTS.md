# AGENTS.md

## Repo overview

Helm charts collection published as OCI charts from `charts/` (one chart per directory, depth-1) to
`oci://ghcr.io/jharmison-redhat/helm-charts`. `examples/` holds umbrella examples; `examples/maas-umbrella` is the
operand umbrella. CI lives in `.github/workflows/` and is split across two workflows with
`.github/workflows/chart_ci.sh` as the shared helper: `lint-chart.yml` runs on pull requests (paths touching
`charts/**`, `examples/**`, or the workflow files) plus manual dispatch, and lints every chart and the umbrella against
locally resolved sub-charts; `publish-chart.yml` remains push-to-main and publishes only charts whose `version:` has no
registry tag yet.

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
`helm dependency build` inside publish-chart.yml, and manual dispatch as a fallback. PR lint resolves intra-repo
dependencies locally (`file://`), so a PR that bumps a dependency chart lints without waiting for a publish; publish
resolves the real OCI pins from the registry. When adding an intra-repo OCI dependency: confirm the dependency chart is
already published (or lands in the same push first), and bump BOTH the dependency's version pin and the dependent's
version in the same push.

## Versioning and push discipline

SemVer bump per functional change, `version:` and `appVersion` in lockstep. A chart publishes only when its `version:`
tag is absent from ghcr; a bump whose publish failed is retried by the next push that touches `charts/**` (or a manual
dispatch). `workflow_dispatch` publishes missing tags only and there is no force/republish input. Use stacked commits
with dedicated push points: a publish bump must land together with any dependent's pin bump in the same push.

## Verification commands

Because push-to-main is no longer linted except by the publish gate, run `chart_ci.sh lint-local` for every changed
chart and the umbrella before committing:

```sh
.github/workflows/chart_ci.sh lint-local charts/<name>
.github/workflows/chart_ci.sh lint-local examples/maas-umbrella
helm lint charts/<name>
helm template charts/<name>
helm dependency build charts/<name>   # login required for OCI deps
echo "$TOKEN" | helm registry login ghcr.io -u <user> --password-stdin
# Clean-checkout package simulation:
git archive HEAD charts/<name> | tar -x -C /tmp/opencode/pkg-check && helm package /tmp/opencode/pkg-check/charts/<name>
```

`lint-local` lints a prepared copy (working-tree changes, vendored `charts/` and `Chart.lock` excluded), so it needs no
registry auth; an external (non-intra-repo) dependency is a hard error there.

The umbrella example under `examples/maas-umbrella` is never published — `chart_versions.sh` scans `charts/` only. It is
linted locally in PRs, composed with local sub-charts.
