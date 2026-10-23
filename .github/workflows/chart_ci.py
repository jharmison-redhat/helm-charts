#!/usr/bin/env python3
"""Shared Helm chart CI helper: dependency resolution + lint/render.

Modes:
  prepare-local <chart-dir>  Copy the chart offline, rewrite intra-repo OCI deps
                             to prepared file:// dep copies, build bottom-up.
                             Prints ONLY the prepared copy's path on stdout.
  lint-local <chart-dir>     prepare-local, then helm lint + template on the
                             prepared path.
  build-registry <chart-dir> In-place helm dependency build with retry (only
                             when the chart declares dependencies).
  lint <chart-dir>           helm lint + template on the given directory.
"""

import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chart_metadata
import yaml

USAGE = "usage: chart_ci.py {prepare-local|lint-local|build-registry|lint} <chart-dir>"
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def run_helm(args, env_overrides=None, cwd=None, render_quiet=False):
    """Run helm with check=True; all helm output goes to stderr, except a
    quiet render check whose stdout is discarded."""
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    stdout = subprocess.DEVNULL if render_quiet else sys.stderr
    subprocess.run(["helm", *args], check=True, env=env, cwd=cwd, stdout=stdout)


def prepare_local(chart_dir, cache=None, stack=None):
    """Recursively prepare an offline copy of ``chart_dir``.

    Rewrites intra-repo OCI dependencies to file:// paths pointing at the
    prepared dependency copies, and builds them bottom-up. Returns the
    prepared copy's path. ``cache`` and ``stack`` carry recursion state; a
    dependency cycle raises SystemExit.
    """
    cache = {} if cache is None else cache
    stack = [] if stack is None else stack

    chart = chart_metadata.load_chart(chart_dir)
    name = chart["name"]

    if name in cache:
        return cache[name]
    if name in stack:
        print(f"chart_ci: ERROR: dependency cycle detected at '{name}': "
              f"{' -> '.join(stack)} -> {name}", file=sys.stderr)
        raise SystemExit(1)

    stack.append(name)
    try:
        dest = tempfile.mkdtemp(prefix="chart-ci.",
                                dir=_work_root())
        # Working-tree copy excluding gitignored vendored artifacts; they
        # would mask missing-dependency failures. Realpath dereferences
        # symlinked chart directories, which `cp -a src/.` would not copy.
        chart_dir = os.path.realpath(chart_dir)
        subprocess.run(["cp", "-a", f"{chart_dir}/.", dest], check=True)
        for artifact in ("charts", "Chart.lock"):
            artifact_path = os.path.join(dest, artifact)
            if os.path.isdir(artifact_path):
                subprocess.run(["rm", "-rf", artifact_path], check=True)
            elif os.path.exists(artifact_path):
                os.remove(artifact_path)

        file_urls = {}
        for dep in chart_metadata.dependencies(chart_dir):
            dep_name = dep["name"]
            dep_repo = dep.get("repository")
            if dep_repo is None:
                continue
            if not (dep_repo.startswith("oci://ghcr.io/")
                    and dep_repo.endswith("/helm-charts")):
                print(f"chart_ci: ERROR: external dependency repository "
                      f"'{dep_repo}' (chart '{dep_name}') is unsupported in "
                      f"local lint; the lint workflow carries no registry auth",
                      file=sys.stderr)
                raise SystemExit(1)
            dep_src = os.path.join(REPO_ROOT, "charts", dep_name)
            if not os.path.isfile(f"{dep_src}/{chart_metadata.CHART_FILE}"):
                if dep_name == name:
                    print(f"chart_ci: ERROR: dependency cycle detected at "
                          f"'{name}': {' -> '.join(stack)} -> {name}",
                          file=sys.stderr)
                else:
                    print(f"chart_ci: ERROR: intra-repo dependency "
                          f"'{dep_name}' has no chart under charts/ in {REPO_ROOT}",
                          file=sys.stderr)
                raise SystemExit(1)
            file_urls[dep_name] = f"file://{prepare_local(dep_src, cache, stack)}"

        if file_urls:
            prepared_chart = chart_metadata.load_chart(dest)
            for dep in prepared_chart.get("dependencies") or []:
                if dep["name"] in file_urls:
                    dep["repository"] = file_urls[dep["name"]]
            with open(f"{dest}/{chart_metadata.CHART_FILE}", "w") as f:
                yaml.safe_dump(prepared_chart, f, sort_keys=False)
            run_helm(["dependency", "build", dest], env_overrides={
                "HELM_REPOSITORY_CONFIG": _empty_repo_config(),
                "HELM_REPOSITORY_CACHE": tempfile.mkdtemp(
                    prefix="chart-ci-cache.", dir=_work_root()),
            }, cwd=REPO_ROOT)
    finally:
        stack.pop()

    cache[name] = dest
    return dest


def lint_local(chart_dir):
    """prepare-local, then lint and render the prepared copy."""
    prepared = prepare_local(chart_dir)
    print(f"chart_ci: linting prepared copy: {prepared}", file=sys.stderr)
    lint(prepared)


def build_registry(chart_dir):
    """In-place helm dependency build with bounded retry; skips when the chart
    declares no dependencies."""
    deps = chart_metadata.dependencies(chart_dir)
    if not deps:
        print("chart_ci: no dependencies: block; skipping helm dependency build",
              file=sys.stderr)
        return
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            run_helm(["dependency", "build", chart_dir], cwd=REPO_ROOT)
            return
        except subprocess.CalledProcessError:
            if attempt >= RETRY_ATTEMPTS:
                print(f"chart_ci: ERROR: helm dependency build failed after "
                      f"{attempt} attempts", file=sys.stderr)
                raise SystemExit(1)
            print(f"chart_ci: helm dependency build failed (attempt {attempt}/"
                  f"{RETRY_ATTEMPTS}); retrying in {RETRY_DELAY_SECONDS}s",
                  file=sys.stderr)
            time.sleep(RETRY_DELAY_SECONDS)


def lint(chart_dir):
    """helm lint plus a render check on the given directory."""
    run_helm(["lint", chart_dir], cwd=REPO_ROOT)
    run_helm(["template", chart_dir], cwd=REPO_ROOT, render_quiet=True)


def _work_root():
    root = os.environ.get("CHART_CI_TMPDIR") or os.environ.get("RUNNER_TEMP") or "/tmp"
    os.makedirs(root, exist_ok=True)
    return root


def _empty_repo_config():
    path = os.path.join(_work_root(), "chart-ci-empty-repos.txt")
    open(path, "a").close()
    return path


def main(argv):
    """CLI entrypoint; returns a process exit code."""
    if len(argv) != 3 or argv[1] not in ("prepare-local", "lint-local",
                                         "build-registry", "lint"):
        print(f"usage: {os.path.basename(argv[0])} "
              f"{{prepare-local|lint-local|build-registry|lint}} <chart-dir>",
              file=sys.stderr)
        return 2
    mode, chart_dir = argv[1], argv[2]
    if mode == "prepare-local":
        print(prepare_local(chart_dir))
    elif mode == "lint-local":
        lint_local(chart_dir)
    elif mode == "build-registry":
        build_registry(chart_dir)
    else:
        lint(chart_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
