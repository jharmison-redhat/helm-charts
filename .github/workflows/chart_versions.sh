#!/usr/bin/env bash

set -euo pipefail

owner="${REGISTRY_OWNER:-${GITHUB_REPOSITORY_OWNER:-}}"
if [ -z "$owner" ]; then
  echo "::error::REGISTRY_OWNER is not set and GITHUB_REPOSITORY_OWNER is empty" >&2
  exit 1
fi

output="${GITHUB_OUTPUT:-/dev/null}"

registry="ghcr.io/${owner}/helm-charts"
accept='application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'

# Extract a top-level scalar field from a Chart.yaml. Prints the first match;
# empty output when absent.
chart_field() {
  local field="$1" yaml="$2"
  python3 - "$field" "$yaml" <<'EOF'
import re, sys
field, path = sys.argv[1], sys.argv[2]
for line in open(path):
    m = re.match(rf'{field}:\s*(\S+)\s*$', line)
    if m:
        print(m.group(1))
        break
EOF
}

# Fetch an anonymous bearer token for a public package, authenticated with -u
# when a token is present. Returns 0 with the token on stdout for HTTP 200 with
# a non-empty .token; 2 for 401/403; 1 for any other status, curl failure, 000,
# or an empty token (fatal).
fetch_token() {
  local repo="$1" status resp body token
  local args=(-sS --max-time 30 -w $'\n%{http_code}' \
    "https://ghcr.io/token?service=ghcr.io&scope=repository:${repo}:pull")
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    args+=(-u "${GITHUB_ACTOR:-}:${GITHUB_TOKEN}")
  fi
  if ! resp="$(curl "${args[@]}")"; then
    echo "::error::token request failed for ${repo}" >&2
    return 1
  fi
  status="${resp##*$'\n'}"
  body="${resp%$'\n'*}"
  case "$status" in
    200)
      token="$(jq -r '.token // empty' <<<"$body")"
      if [ -z "$token" ]; then
        echo "::error::token response for ${repo} had no token" >&2
        return 1
      fi
      printf '%s' "$token"
      ;;
    401|403) return 2 ;;
    *) echo "::error::token request for ${repo} returned ${status}" >&2; return 1 ;;
  esac
}

# Manifest check with an explicit Accept header; without it ghcr returns 404
# even for existing tags. 200 -> exists (0), 404 -> absent (2), else fatal (1).
probe_manifest() {
  local repo="$1" tag="$2" token="$3"
  local status
  status="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer ${token}" -H "Accept: ${accept}" \
    "https://ghcr.io/v2/${repo}/manifests/${tag}")" || status="000"
  case "$status" in
    200) return 0 ;;
    404) return 2 ;;
    *) echo "::error::manifest probe for ${repo}:${tag} returned ${status}" >&2; return 1 ;;
  esac
}

# Pre-flight canary: at least one existing chart repo must yield a token, a
# non-empty tag list, and a 200 manifest. Returns 0 when a repo passes, 1 when
# none do; per-repo misses are quiet so a first-pass gate stays clean.
canary_ok() {
  local chart repo token body tag
  while read -rd $'\0' chart; do
    repo="${registry}/$(basename "$chart")"
    token=""
    if ! token="$(fetch_token "$repo")"; then continue; fi
    if ! body="$(curl -fsS --max-time 30 -H "Authorization: Bearer ${token}" \
      "https://ghcr.io/v2/${repo}/tags/list")"; then continue; fi
    tag="$(jq -r '.tags[0] // empty' <<<"$body")"
    if [ -z "$tag" ]; then continue; fi
    if probe_manifest "$repo" "$tag" "$token"; then
      return 0
    fi
  done < <(find charts -mindepth 1 -maxdepth 1 -type d -print0)
  return 1
}

if ! canary_ok; then
  echo "::error::auth/registry unavailable: no existing chart repo is readable; refusing to select charts (would republish the whole repo)" >&2
  exit 1
fi

publish=false
declare -A publish_charts
declare -A chart_version

while read -rd $'\0' path; do
  name="$(basename "$path")"

  yaml="$path/Chart.yaml"
  chart_name="$(chart_field name "$yaml")"
  if [ "$chart_name" != "$name" ]; then
    echo "::error::${yaml}: name '${chart_name}' does not match directory basename '${name}'" >&2
    exit 1
  fi
  version="$(chart_field version "$yaml")"

  repo="${registry}/${name}"
  token=""; attempts=0
  while [ $attempts -lt 3 ]; do
    token_rc=0
    if token="$(fetch_token "$repo")"; then break; fi
    token_rc=$?
    if [ "$token_rc" -eq 1 ]; then exit 1; fi
    attempts=$((attempts + 1))
    sleep 5
  done

  if [ -z "$token" ]; then
    # Canary proved credentials work, so a persistent per-repo denial is
    # treated as an absent (new) chart; the push is the backstop.
    echo "::warning::token request for ${repo} failed repeatedly after the canary passed; treating chart as new" >&2
    select_chart=2
  else
    probe_rc=0
    probe_manifest "$repo" "$version" "$token" || probe_rc=$?
    case "$probe_rc" in
      0) select_chart=0 ;;
      2) select_chart=2 ;;
      *) exit 1 ;;
    esac
  fi

  case $select_chart in
    0) echo "$name: $version (already published)" ;;
    2)
      echo "$name: $version (new)"
      publish=true
      publish_charts["$name"]="$path"
      chart_version["$name"]="$version"
      ;;
  esac
done < <(find charts -mindepth 1 -maxdepth 1 -type d -print0)

charts='{"include": ['
for chart in "${!publish_charts[@]}"; do
  charts+='{"name": "'"$chart"'", "path": "'"${publish_charts[$chart]}"'", "version": "'"${chart_version[$chart]}"'"}, '
done
charts="${charts%, }]}"

printf 'publish=%s\nmatrix=%s\n' "$publish" "$charts" | tee -a "$output"
