# AGENTS.md

## Repo overview

Helm charts collection published as OCI charts from `charts/` (one chart per directory, depth-1) to
`oci://ghcr.io/jharmison-redhat/helm-charts`. `examples/` holds umbrella examples; `examples/maas-umbrella` is the
operand umbrella. CI lives in `.github/workflows/` and is split across two workflows with native-Python helpers:
`.github/workflows/chart_ci.py` (dependency resolution + lint/render, shared by both workflows),
`.github/workflows/chart_versions.py` (registry-tag publish gate), and `.github/workflows/chart_metadata.py` (shared
safe-YAML metadata validation); `.github/workflows/requirements.txt` pins PyYAML and
`.github/workflows/test_chart_helpers.py` is the unit-test suite. `lint-chart.yml` runs on pull requests touching the
charts and examples it validates (`charts/**`, `examples/**`) plus the helpers, test file, and requirements it runs,
plus manual dispatch, and lints every chart and the umbrella against locally resolved sub-charts; `publish-chart.yml`
remains push-to-main and publishes only charts whose `version:` has no registry tag yet.

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
dependencies locally (`file://` rewrite of the prepared copies), so a PR that bumps a dependency chart (or its pin)
lints without waiting for a publish; publish resolves the real OCI pins from the registry, and the tag gate rejects pins
that exclude the working-tree version against the local chart versions. When adding an intra-repo OCI dependency:
confirm the dependency chart is already published (or lands in the same push first), and bump BOTH the dependency's
version pin and the dependent's version in the same push.

## Versioning and push discipline

SemVer bump per functional change, `version:` and `appVersion` in lockstep. A chart publishes only when its `version:`
tag is absent from ghcr; a bump whose publish failed is retried by the next push that touches `charts/**` (or a manual
dispatch). `workflow_dispatch` publishes missing tags only and there is no force/republish input. Use stacked commits
with dedicated push points: a publish bump must land together with any dependent's pin bump in the same push.

## Verification commands

Because push-to-main is no longer linted except by the publish gate, run `chart_ci.py lint-local` for every changed
chart and the umbrella before committing. The helpers run under Python 3.13 with PyYAML from the pinned requirements;
use `uv` locally (first use downloads the 3.13 interpreter and the pinned dependency, then reuses the cache, and
`--offline` works after that provisioning):

```sh
uv run --no-project --python 3.13 --with-requirements .github/workflows/requirements.txt \
  python .github/workflows/chart_ci.py lint-local charts/<name>
uv run --no-project --python 3.13 --with-requirements .github/workflows/requirements.txt \
  python .github/workflows/chart_ci.py lint-local examples/maas-umbrella
uv run --no-project --python 3.13 --with-requirements .github/workflows/requirements.txt \
  python -m unittest discover -s .github/workflows -p 'test_*.py'
echo "$TOKEN" | helm registry login ghcr.io -u <user> --password-stdin
helm dependency build charts/<name>   # login required for OCI deps
helm lint charts/<name>
helm template charts/<name>
# Clean-checkout package simulation:
git archive HEAD charts/<name> | tar -x -C /tmp/opencode/pkg-check && helm package /tmp/opencode/pkg-check/charts/<name>
```

`lint-local` lints a prepared copy (working-tree changes, vendored `charts/` and `Chart.lock` excluded), so it needs no
registry auth; an external (non-intra-repo) dependency is a hard error there.

The umbrella example under `examples/maas-umbrella` is never published — `chart_versions.py` scans `charts/` only. It is
linted locally in PRs, composed with local sub-charts.
