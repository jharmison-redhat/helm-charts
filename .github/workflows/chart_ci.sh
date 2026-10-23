#!/usr/bin/env bash
# Shared Helm chart CI helper: dependency resolution + lint/render.
#
# Modes:
#   prepare-local <chart-dir>  Copy the chart offline, rewrite intra-repo OCI deps
#                              to prepared file:// dep copies, build bottom-up.
#                              Prints ONLY the prepared copy's path on stdout.
#   lint-local <chart-dir>     prepare-local, then helm lint + template on the
#                              prepared path.
#   build-registry <chart-dir> In-place helm dependency build with retry (only
#                              when the chart declares dependencies).
#   lint <chart-dir>           helm lint + template on the given directory.
set -euo pipefail

usage() {
  echo "usage: $0 {prepare-local|lint-local|build-registry|lint} <chart-dir>" >&2
  exit 2
}

[ $# -eq 2 ] || usage
MODE=$1
CHART_DIR=$2

# Repo root is derived from the script location, never CWD.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

WORK_ROOT="${CHART_CI_TMPDIR:-${RUNNER_TEMP:-/tmp}}"
mkdir -p "$WORK_ROOT"

# Recursion state for prepare-local: per-name prepared-path cache and the
# active recursion stack for cycle detection.
declare -A PREPARED_CACHE=()
declare -a PREP_STACK=()

fail() {
  echo "chart_ci: ERROR: $*" >&2
  exit 1
}

# Print "name repository" lines for each item in the dependencies: block.
# stdlib line parser scoped to the block; tolerates quoted values; expects
# block-style items with name: before repository: (true for every chart here;
# a missing pair surfaces via the post-rewrite assertion or the hard error).
list_deps() {
  python3 - "$1" <<'PY'
import re, sys
path = sys.argv[1]
in_deps = False
name = None
for line in open(path):
    if re.match(r'^\S', line):
        in_deps = line.rstrip().split(':', 1)[0] == 'dependencies'
        continue
    if not in_deps:
        continue
    m = re.match(r'^\s*-\s*name:\s*(.+)$', line)
    if m:
        name = m.group(1).strip().strip('"').strip("'")
        continue
    m = re.match(r'^\s*repository:\s*(.+)$', line)
    if m and name:
        print(name, m.group(1).strip().strip('"').strip("'"))
        name = None
PY
}

# Rewrite intra-repo OCI repository values in the prepared Chart.yaml.
# Args: chart-yaml, owner-independent match is by name mapping; pass
# "name=file://path" pairs as remaining args.
rewrite_repos() {
  local yaml="$1"; shift
  python3 - "$yaml" "$@" <<'PY'
import re, sys
path = sys.argv[1]
mapping = {}
for pair in sys.argv[2:]:
    name, url = pair.split('=', 1)
    mapping[name] = url
in_deps = False
cur = None
out = []
for line in open(path):
    if re.match(r'^\S', line):
        in_deps = line.rstrip().split(':', 1)[0] == 'dependencies'
        cur = None
    elif in_deps:
        m = re.match(r'^(\s*-\s*name:\s*)(.+)$', line)
        if m:
            cur = m.group(2).strip().strip('"').strip("'")
        elif cur in mapping:
            m = re.match(r'^(\s*repository:\s*)(.+)$', line)
            if m:
                line = m.group(1) + mapping[cur] + '\n'
    out.append(line)
open(path, 'w').write(''.join(out))
PY
}

# Assert no OCI repository remains inside the dependencies: block.
assert_all_local() {
  python3 - "$1" <<'PY'
import re, sys
in_deps = False
bad = []
for line in open(sys.argv[1]):
    if re.match(r'^\S', line):
        in_deps = line.rstrip().split(':', 1)[0] == 'dependencies'
        continue
    if in_deps and re.search(r'repository:\s*["\']?oci://', line):
        bad.append(line.strip())
if bad:
    sys.stderr.write("chart_ci: ERROR: intra-repo OCI repository not rewritten:\n" + "\n".join(bad) + "\n")
    sys.exit(1)
PY
}

helm_quiet() {
  helm "$@" >&2
}

prepare_local() {
  local src="$1"
  [ -f "$src/Chart.yaml" ] || fail "not a chart directory: $src"
  local name
  name="$(python3 -c "import re,sys; print([l.split(':',1)[1].strip() for l in open(sys.argv[1]) if l.startswith('name:')][0])" "$src/Chart.yaml")"

  if [ "${PREPARED_CACHE[$name]+set}" ]; then
    echo "${PREPARED_CACHE[$name]}"
    return 0
  fi
  for s in "${PREP_STACK[@]}"; do
    [ "$s" = "$name" ] && fail "dependency cycle detected at '$name': ${PREP_STACK[*]} -> $name"
  done

  PREP_STACK+=("$name")
  local dest
  dest="$(mktemp -d "$WORK_ROOT/chart-ci.XXXXXX")"
  # Working-tree copy excluding gitignored vendored artifacts; portable
  # cp -a + rm instead of cp --exclude.
  cp -a "$src/." "$dest/"
  rm -rf "$dest/charts" "$dest/Chart.lock"

  local dep_args=()
  while read -r dep dep_repo; do
    [ -n "$dep" ] || continue
    if [[ "$dep_repo" == oci://ghcr.io/*/helm-charts ]]; then
      local dep_src="$REPO_ROOT/charts/$dep"
      [ -f "$dep_src/Chart.yaml" ] || fail "intra-repo dependency '$dep' has no chart under charts/ in $REPO_ROOT"
      dep_args+=("$dep=file://$(prepare_local "$dep_src")")
    else
      fail "external dependency repository '$dep_repo' (chart '$dep') is unsupported in local lint; the lint workflow carries no registry auth"
    fi
  done < <(list_deps "$src/Chart.yaml")

  if [ ${#dep_args[@]} -gt 0 ]; then
    rewrite_repos "$dest/Chart.yaml" "${dep_args[@]}"
    assert_all_local "$dest/Chart.yaml"
  fi

  : > "$WORK_ROOT/chart-ci-empty-repos.txt"
  local repo_cfg="$WORK_ROOT/chart-ci-empty-repos.txt"
  if [ ${#dep_args[@]} -gt 0 ]; then
    HELM_REPOSITORY_CONFIG="$repo_cfg" HELM_REPOSITORY_CACHE="$(mktemp -d "$WORK_ROOT/chart-ci-cache.XXXXXX")" \
      helm_quiet dependency build "$dest"
  fi

  PREP_STACK=("${PREP_STACK[@]/$name/}")
  PREPARED_CACHE[$name]="$dest"
  echo "$dest"
}

has_deps() {
  grep -q '^dependencies:' "$1/Chart.yaml"
}

case "$MODE" in
  prepare-local)
    prepare_local "$CHART_DIR"
    ;;
  lint-local)
    PREPARED="$(prepare_local "$CHART_DIR")"
    echo "chart_ci: linting prepared copy: $PREPARED" >&2
    helm lint "$PREPARED"
    helm template "$PREPARED" > /dev/null
    ;;
  build-registry)
    if has_deps "$CHART_DIR"; then
      attempt=1
      while true; do
        if helm dependency build "$CHART_DIR"; then
          break
        fi
        if [ "$attempt" -ge 3 ]; then
          echo "chart_ci: ERROR: helm dependency build failed after $attempt attempts" >&2
          exit 1
        fi
        echo "chart_ci: helm dependency build failed (attempt $attempt/3); retrying in 30s" >&2
        sleep 30
        attempt=$((attempt + 1))
      done
    else
      echo "chart_ci: no dependencies: block; skipping helm dependency build" >&2
    fi
    ;;
  lint)
    helm lint "$CHART_DIR"
    helm template "$CHART_DIR" > /dev/null
    ;;
  *)
    usage
    ;;
esac
