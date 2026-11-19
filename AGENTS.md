# AGENTS.md

## Repo overview

Helm charts collection published as OCI charts from `charts/` (one chart per directory, depth-1) to
`oci://ghcr.io/jharmison-redhat/helm-charts`. `examples/` holds umbrella examples; `examples/maas-umbrella` is the
operand umbrella. CI lives in `.github/workflows/` and is split across two workflows with native-Python helpers:
`.github/workflows/chart_ci.py` (dependency resolution + lint/render, shared by both workflows),
`.github/workflows/chart_versions.py` (registry-tag publish gate), and `.github/workflows/chart_metadata.py` (shared
safe-YAML metadata validation); `.github/workflows/requirements.txt` pins PyYAML. The unit-test suites are
`.github/workflows/test_chart_helpers.py` (workflow helpers) and every `tests/charts/test_*.py` (chart render tests,
discovered from `tests/charts`, sharing the fixtures in `tests/charts/support.py`). The openshift-ai tests additionally
share a chart-local harness module, `tests/charts/oa_support.py`, which unittest discovery skips by its non-`test_`
name. `lint-chart.yml` runs on pull requests touching the charts and examples it validates (`charts/**`,
`examples/**`) plus the helpers, test suites (`tests/charts/**`), and requirements it runs, plus manual dispatch,
and lints every chart and the umbrella against locally resolved sub-charts; `publish-chart.yml` remains push-to-main
and publishes only charts whose `version:` has no registry tag yet.

## Porting discipline

Charts are ports of components from the historical all-in-one repo into composable, independently publishable charts.
Source parity is not a goal — the contract is correct behavior under normal Helm usage (`helm install`, `helm upgrade`,
`helm uninstall`):

- Jobs whose spec can change across chart versions are hooks (`post-install,post-upgrade` +
  `helm.sh/hook-delete-policy: before-hook-creation`), since plain Jobs are immutable on upgrade.
- Operand charts never install operators; operator Subscriptions belong to the `install-operators` chart.
- ArgoCD-only mechanics (sync waves, `SkipDryRunOnMissingResource`, `ignoreDifferences`) do not port; replace them with
  render-time validation (clear diagnostics) and documented prerequisites (operator/CRD establishment, pre-existing
  namespaces).

### Platform-object ownership

- Classify before porting: every object the source manifests touch is either chart-created (a new object — release
  ownership is correct) or platform-owned (exists on every cluster, or is created and reconciled by another controller —
  e.g. `OAuth/cluster`, `APIServer/cluster`, `IngressController/default`, `Console/cluster`, `Proxy/cluster`, cluster
  `Nodes`, operator-created Deployments, operator-suggested or otherwise pre-existing Namespaces, and CRs the operator
  auto-creates such as `OCSInitialization/ocsinit`).
- Platform-owned objects are never release manifests. Partial patch-shaped YAML is not a patch in Helm's ownership
  model: plain Helm rejects the install or upgrade unless ownership is explicitly taken, while Flux and SSA defaults
  silently adopt the object and turn it into an uninstall deletion target — for a shared Namespace that cascades to
  everything inside it (`helm uninstall` deletes chart-declared Namespaces; Kubernetes background propagation cascades
  the contents).
- Mutate platform-owned objects only through conditional-write hook Jobs following the established pattern (reference
  implementations: `charts/keycloak-auth` OAuth IdP + logout-redirect, `charts/cert-manager` cluster trust + serving
  certs): scripts in `files/` delivered via ConfigMap, the chart-local `files/lib/hook-preamble.sh` conditional-write
  engine, dual jq/python3 fail-closed, read-modify-write carrying `metadata.resourceVersion` with a bounded 409 retry,
  foreign-value STOP, by-value idempotence, and resourceName-scoped RBAC. `create` is the one grant RBAC cannot scope by
  name — when it is unavoidable, call it out in the README.
- Singletons created by user or platform action may already exist with foreign configuration (e.g.
  `ClusterAutoscaler/default`) and are never release manifests when they may pre-exist: a conditional-write hook creates
  them when absent (recording that it did) and stops loudly on foreign values when present; the pre-delete walk-back
  follows the same proof rules as for other hook-created objects. When the requester explicitly wants coexistence with
  an existing singleton's configuration, an explicitly-configured take-over-with-restore field surface is the sanctioned
  variant of that foreign-STOP default: the set hook records the pre-existing values of only the fields it manages in a
  proof annotation before changing them and never touches unmanaged fields, and the remove hook restores the recorded
  priors — the chart README documents exactly which field surfaces use it (reference implementation:
  `charts/aws-nvidia-gpu-machinesets` ClusterAutoscaler). The enabling values surface (for example
  `autoscaling.enabled: true`) is itself the declaration that the chart may take over the field surfaces it manages; the
  chart README documents those fields and how teardown restores them.
- A pre-delete hook walks back only what the set hook can prove it set (ownership annotations or recorded history on the
  mutated object), never foreign content; objects the hook created are deleted on uninstall only under the same proof.
  Uninstall contracts (what persists, what is walked back, what degrades) are documented in the chart README.
- Render-time validation closes the rest: invalid compositions (conflicting values, unknown options, cross-value
  membership failures) fail the render with an actionable message rather than reaching a cluster.

### Release namespace guidance

- Every chart whose placement matters documents its recommended release namespace in the chart README with an install
  command, and states the mechanics that force the choice: objects that only function in a specific namespace (operator
  watch scopes, secrets referenced by name, operator-watched custom resources), hook Jobs that run in the release
  namespace, and the uninstall contract for the namespace (chart-created namespaces are never release-owned; uninstall
  never removes them).
- Hook Jobs run with the charts' shared tools image under a guard-without-pin securityContext (container-level
  `runAsNonRoot`, no privilege escalation, ALL capabilities dropped, RuntimeDefault seccomp): restricted SCCs assign the
  namespace's range UID to UID-less pods, so hardened namespaces work. The `default` namespace is excluded — charts
  cannot be installed there because its configuration assigns no SCC, and the kubelet's non-root proof falls through to
  the image's root user. Never pin `runAsUser`/`runAsGroup` in hook pods: platform assignment is the mechanism. The
  keycloak-auth `remove-kubeadmin` hook is the sanctioned exception: its fixed namespace `kube-system` is PSA-privileged
  and bypasses SCC assignment entirely (no range-UID injection), so the guards would make the opt-in surface unrunnable
  — it carries no securityContext and runs as the stock image's default user in the platform-privileged namespace; the
  Job survives `helm uninstall` because hook resources are tracked outside the release manifest, while its RBAC is
  ordinary release content and is removed with the release.
- When the release namespace is genuinely free, the README says so; when hooks are hardcoded to a fixed object
  namespace, the non-root contract applies there too and the README says where.
- When a chart has a canonical object namespace (operator watch scopes, platform namespaces its objects require), the
  templates pin it outright — the release namespace carries no chart objects, and the README says the release namespace
  is free.

### Hook-surface layout

- New hook surfaces follow their chart's existing pattern, not a repo-wide uniform one — the charts intentionally
  differ: install-operators bundles each surface's objects into shared weighted-delivery renders, odf names each
  surface's Job, RBAC, and script `<surface>-{job,rbac,script}.yaml` while cert-manager names the Job first
  (`job-<surface>.yaml`) with `<surface>-{config,rbac}.yaml` siblings, and openshift-ai groups per-feature script
  families. The rule is chart-local consistency.
- Scripts live under `files/` or `files/<family>/`, with `files/lib/` holding the shared hook preamble plus each chart's
  shared helper modules (cert-manager's `trust-bundle.sh`, keycloak-auth's `logout-redirect.sh`, openshift-ai's
  `set/delete-chart-created-cr.sh`). A ConfigMap that globs (e.g. openshift-ai's maas-db `files/*` glob) must not be
  able to absorb sibling families' scripts — the no-root-level-script-leakage invariant is guarded by a unit test
  (`test_maas_db_glob_cannot_absorb_tenant_script`).

## Testing

The offline suite under `tests/charts/` is a regression net, not a specification of Helm or of cluster behavior. Live
deploys on a real cluster are the gate (Versioning and push discipline); this suite is the cheap early-warning layer, so
the marginal value of asserting more template surface offline is very low. It exists for exactly four purposes:

1. Catch behavioral regressions in what a chart renders: an object missing, wrong, or misplaced; a value that stopped
   having an effect; wrong namespace; a broken gate.
2. Catch template failures `helm lint` cannot see — also covered repo-wide by the CI strict-render gate
   (`chart_ci.py`), which handles parse-level breakage wholesale.
3. Enforce the repo's structural invariants: platform-objects-never-render-as-release-manifests, the ConfigMap-glob
   no-leakage rule, guard-without-pin securityContext on hook Jobs, and shared-lib byte parity (`hook-preamble.sh`,
   `wait-for.sh`).
4. The per-hook-script smoke set: ONE happy-path run per hook script (python3-only tool path) plus ONE fail-closed
   no-tool check per chart — not per script.

Non-goals — a test exhibiting one of these is a reason to delete it, not to keep it:

- Asserting Helm's own template-engine behavior: conditionals, range, toYaml, `fail` delivery mechanics, gated-off
  renders emitting empty/nothing.
- Walking every conditional branch or values combination: no render matrices; one representative case per behavior,
  default values where possible.
- Pinning exact validation-message text: a failing render asserts that failure occurred, not the wording.
- Behavioral simulation of cluster state (fake-API state machines, timeout-loop simulations with canned responses):
  the class is brittle, slow (process-spawn multiplication), and cannot prove what a live deploy proves.
- Coverage targets. When in doubt, delete the test.

Per-chart budget: roughly one default render asserting the core object inventory (kinds/names, coarse — not
field-by-field), the invariant tests, at most one representative render-failure test, and the per-script smoke set.
A chart exceeding that budget is over-tested by default.

Rules for keeping the suite honest:

- Live validation is scoped to the change: exercise the surfaces the change touches (the hook surfaces it modifies, the
  values it alters, the objects it adds or re-homes); unchanged surfaces keep their prior live validation and are not
  re-proven — live testing is not a re-run of every possible universe on every change.
- Hook-script behavior beyond the smoke set — state-machine behavior (retries, foreign-value STOP, walk-back,
  concurrency) — is proven by real deploys on a live cluster, not by fixture simulation.
- The openshift-ai tests share a chart-local harness module, `tests/charts/oa_support.py`, which unittest discovery
  skips by its non-`test_` name; keep fake-oc harness consolidation to folding single-consumer fixtures into their
  consumer, never inventing new harness machinery.

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
lints without waiting for a publish and enforces the pin against the working-tree chart versions; publish resolves the
real OCI pins from the registry, and the tag gate checks tag existence only. When adding an intra-repo OCI dependency:
confirm the dependency chart is already published (or lands in the same push first), and bump BOTH the dependency's
version pin and the dependent's version in the same push.

## Versioning and push discipline

SemVer bump per functional change, `version:` and `appVersion` in lockstep. A chart publishes only when its `version:`
tag is absent from ghcr; a bump whose publish failed is retried by the next push that touches `charts/**` (or a manual
dispatch). `workflow_dispatch` publishes missing tags only and there is no force/republish input. Use stacked commits
with dedicated push points: a publish bump must land together with any dependent's pin bump in the same push.
Re-publishing an existing chart version — for example a manual `helm push` outside the gate — silently overwrites the
OCI tag: the gate checks tag existence only, and nothing at the registry level prevents an overwrite.

- No chart version publishes — whether a fresh chart import or a version bump — until a real deploy on a live cluster
  validates the changed chart. Local commits land without the gate; the push that publishes versions happens only after
  live validation of the surfaces the change actually touches — unchanged surfaces keep their prior live validation. CI
  (lint/render/unit tests) is not substitute proof.

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
uv run --no-project --python 3.13 --with-requirements .github/workflows/requirements.txt \
  python -m unittest discover -s tests/charts -p 'test_*.py'
echo "$TOKEN" | helm registry login ghcr.io -u <user> --password-stdin
helm dependency build charts/<name>   # login required for OCI deps
helm lint charts/<name>
helm template charts/<name>
# Clean-checkout package simulation:
git archive HEAD charts/<name> | tar -x -C /tmp/opencode/pkg-check && helm package /tmp/opencode/pkg-check/charts/<name>
```

`lint-local` lints a prepared copy (working-tree changes, vendored `charts/` and `Chart.lock` excluded), so it needs no
registry auth; an external (non-intra-repo) dependency is a hard error there. A chart-local `ci-values.yaml` (if
present) is passed as an extra `-f` to the CI render check so charts with required per-cluster values can lint and
publish; it is CI-only and never referenced by installs, and charts using the convention must add `ci-values.yaml` to a
chart-local `.helmignore` so the placeholder values are not published in the packaged chart.

The umbrella example under `examples/maas-umbrella` is never published — `chart_versions.py` scans `charts/` only. It is
linted locally in PRs, composed with local sub-charts.
