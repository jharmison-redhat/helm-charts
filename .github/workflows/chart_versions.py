#!/usr/bin/env python3
"""Registry-tag publish gate: select charts whose Chart.yaml version is not
already a ghcr tag, and emit the publish flag plus matrix for Actions.

Fail closed: a pre-flight canary must prove an existing chart repo is readable
before anything is selected, so a credential or registry outage cannot cause a
whole-repo republish.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chart_metadata

TOKEN_TIMEOUT_SECONDS = 30
TOKEN_RETRIES = 3
TOKEN_RETRY_DELAY_SECONDS = 5
ACCEPT = ("application/vnd.oci.image.manifest.v1+json, "
          "application/vnd.docker.distribution.manifest.v2+json")


class RegistryError(Exception):
    """Fatal registry, credential, or network failure."""


class PackageDenied(Exception):
    """HTTP 401/403 for one package: possibly a genuinely new chart."""


def _open(url, headers=None, data=None):
    """Open a URL with a timeout and never leak credentials in errors."""
    request = urllib.request.Request(url, headers=headers or {}, data=data)
    try:
        return urllib.request.urlopen(request, timeout=TOKEN_TIMEOUT_SECONDS)
    except urllib.error.HTTPError as exc:
        return exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(f"registry request to {url.split('?')[0]} failed: {exc}")


def _token_headers(actor):
    """Build optional Basic auth headers; the token is never logged."""
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return {}
    credentials = base64.b64encode(
        f"{os.environ.get('GITHUB_ACTOR', actor)}:{token}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def fetch_token(repo):
    """Fetch a bearer token for ``repo``'s pull scope.

    Returns the token for HTTP 200 with a non-empty JSON ``token``; raises
    PackageDenied for 401/403 and RegistryError for any other status, a
    malformed response, or a network failure.
    """
    url = f"https://ghcr.io/token?service=ghcr.io&scope=repository:{repo}:pull"
    with _open(url, headers=_token_headers(repo)) as response:
        if response.status in (401, 403):
            raise PackageDenied(repo)
        if response.status != 200:
            raise RegistryError(f"token request for {repo} returned HTTP {response.status}")
        try:
            body = json.load(response)
        except (ValueError, UnicodeDecodeError) as exc:
            raise RegistryError(f"token response for {repo} is not valid JSON: {exc}")
    token = body.get("token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RegistryError(f"token response for {repo} had no token")
    return token


def probe_manifest(repo, tag, token):
    """Manifest check with an explicit Accept header; without it ghcr returns
    404 even for existing tags.

    Returns True when the tag exists (HTTP 200), False when absent (404);
    raises RegistryError for any other status or a network failure.
    """
    url = f"https://ghcr.io/v2/{repo}/manifests/{tag}"
    with _open(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": ACCEPT,
    }) as response:
        if response.status == 200:
            return True
        if response.status == 404:
            return False
        raise RegistryError(f"manifest probe for {repo}:{tag} returned HTTP {response.status}")


def canary_ok(registry, chart_names):
    """At least one existing chart repo must yield a token, a non-empty tag
    list, and an existing-tag manifest. Per-repo misses are quiet so a
    first-pass gate stays clean."""
    for chart in chart_names:
        repo = f"{registry}/{chart}"
        try:
            token = fetch_token(repo)
            with _open(f"https://ghcr.io/v2/{repo}/tags/list",
                       headers={"Authorization": f"Bearer {token}"}) as response:
                if response.status != 200:
                    continue
                try:
                    tags = json.load(response).get("tags") or []
                except (ValueError, UnicodeDecodeError):
                    continue
            if not tags:
                continue
            if probe_manifest(repo, tags[0], token):
                return True
        except (PackageDenied, RegistryError):
            continue
    return False


def main(argv):
    """CLI entrypoint; returns a process exit code."""
    owner = os.environ.get("REGISTRY_OWNER") or os.environ.get("GITHUB_REPOSITORY_OWNER") or ""
    if not owner:
        print("::error::REGISTRY_OWNER is not set and GITHUB_REPOSITORY_OWNER is empty",
              file=sys.stderr)
        return 1

    registry = f"ghcr.io/{owner}/helm-charts"
    chart_names = sorted(
        entry for entry in os.listdir("charts")
        if os.path.isdir(os.path.join("charts", entry)))

    if not canary_ok(registry, chart_names):
        print("::error::auth/registry unavailable: no existing chart repo is "
              "readable; refusing to select charts (would republish the whole repo)",
              file=sys.stderr)
        return 1

    try:
        selected = _select_charts(registry, chart_names)
    except RegistryError as exc:
        # Expected registry/network failures are reported at the CLI
        # boundary as a clean ::error:: instead of a traceback; messages
        # never include credentials. Programming bugs still propagate.
        print(f"::error::{exc}", file=sys.stderr)
        return 1

    publish = "true" if selected else "false"
    matrix = json.dumps({"include": selected})
    _write_output(publish, matrix)
    return 0


def _select_charts(registry, chart_names):
    """Select charts whose version tags are absent; returns the matrix entries."""
    selected = []
    for chart in chart_names:
        chart_dir = os.path.join("charts", chart)
        chart_data = _chart_metadata_for(chart_dir, chart)
        version = chart_data["version"]

        repo = f"{registry}/{chart}"
        denied = False
        for attempt in range(1, TOKEN_RETRIES + 1):
            try:
                token = fetch_token(repo)
                denied = False
                break
            except PackageDenied:
                denied = True
            time.sleep(TOKEN_RETRY_DELAY_SECONDS)

        if denied:
            # The canary proved credentials work, so a persistent per-repo
            # denial is treated as an absent (new) chart; the push is the
            # backstop.
            print(f"::warning::token request for {repo} failed repeatedly "
                  f"after the canary passed; treating chart as new", file=sys.stderr)
            exists = False
        else:
            exists = probe_manifest(repo, version, token)

        if exists:
            print(f"{chart}: {version} (already published)")
        else:
            print(f"{chart}: {version} (new)")
            selected.append({"name": chart, "path": chart_dir, "version": version})

    return selected


def _chart_metadata_for(chart_dir, chart):
    """Validate the chart's metadata, including name == directory basename."""
    data = chart_metadata.load_chart(chart_dir)
    if data["name"] != chart:
        print(f"::error::{chart_dir}/{chart_metadata.CHART_FILE}: name "
              f"'{data['name']}' does not match directory basename '{chart}'",
              file=sys.stderr)
        raise SystemExit(1)
    return data


def _write_output(publish, matrix):
    """Append the paired Actions output keys to GITHUB_OUTPUT."""
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as f:
            f.write(f"publish={publish}\n")
            f.write(f"matrix={matrix}\n")
    else:
        print(f"publish={publish}")
        print(f"matrix={matrix}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
