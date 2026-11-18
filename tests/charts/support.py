"""Shared fixtures for the chart test modules under tests/charts.

ChartFixture copies the chart under test into a temp directory and drives
``helm template`` against it. FakeOcScriptFixture executes rendered hook
scripts on a restricted, symlink-only PATH against a canned ``oc`` program;
the fake ``oc`` program itself stays in each test module because each chart's
fake serves different resources and records a different call surface.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import yaml


def docs(text):
    """All non-null documents in a multi-document YAML stream."""
    return [d for d in yaml.safe_load_all(text) if d is not None]


def hook_jobs(rendered):
    """The rendered Job manifests carrying a helm.sh/hook annotation."""
    return [d for d in rendered
            if d.get("kind") == "Job"
            and "helm.sh/hook" in d.get("metadata", {}).get("annotations", {})]


def assert_guard_without_pin(testcase, pod):
    """Asserts the hook guard-without-pin contract on a rendered pod spec
    (a Job's ``spec.template.spec``).

    Every container carries all four restricted-pod guards (runAsNonRoot,
    no privilege escalation, ALL capabilities dropped, RuntimeDefault
    seccomp), and no securityContext anywhere in the pod pins a
    runAsUser/runAsGroup — the UID-less shape is the contract: restricted
    SCCs assign the namespace's range UID to UID-less pods at admission,
    and a pin would fight that platform assignment.
    """
    containers = pod.get("initContainers", []) + pod.get("containers", [])
    testcase.assertTrue(containers, "pod carries no containers")
    for container in containers:
        sc = container.get("securityContext") or {}
        with testcase.subTest(container=container.get("name")):
            testcase.assertIs(sc.get("runAsNonRoot"), True)
            testcase.assertIs(sc.get("allowPrivilegeEscalation"), False)
            testcase.assertEqual(sc.get("capabilities"), {"drop": ["ALL"]})
            testcase.assertEqual(
                sc.get("seccompProfile"), {"type": "RuntimeDefault"})
            testcase.assertNotIn("runAsUser", sc)
            testcase.assertNotIn("runAsGroup", sc)
    pod_sc = pod.get("securityContext") or {}
    testcase.assertNotIn("runAsUser", pod_sc)
    testcase.assertNotIn("runAsGroup", pod_sc)


class ChartFixture(unittest.TestCase):
    """Copies the chart under test into a temp dir and renders it with
    ``helm template``.

    Subclasses set ``chart`` (absolute path) and may override
    ``tmp_prefix``, ``values_prefix``, and ``release_namespace``. The
    chart's vendored ``charts/`` directory and ``Chart.lock`` are excluded
    from the copy; charts with an intra-repo dependency override
    ``helm_template`` to call ``stub_dependency()`` first.
    """

    chart = None
    tmp_prefix = "chart-test-"
    values_prefix = "chart-values-"
    release_namespace = "test-release-ns"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix=self.tmp_prefix)
        self.addCleanup(shutil.rmtree, self.dir)
        shutil.copytree(
            self.chart, self.dir, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("charts", "Chart.lock"))

    def stub_dependency(self):
        """Bare local stub of the postgres-cluster dependency so
        ``helm template`` needs no registry access."""
        dep = os.path.join(self.dir, "charts", "postgres-cluster")
        os.makedirs(dep, exist_ok=True)
        with open(os.path.join(dep, "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: postgres-cluster\nversion: 0.1.0\n")

    def values_file(self, values):
        fd, path = tempfile.mkstemp(prefix=self.values_prefix, suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(values, f)
        self.addCleanup(os.unlink, path)
        return path

    def helm_template(self, values=None, namespace=None):
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", namespace or self.release_namespace]
        if values is not None:
            cmd += ["--values", self.values_file(values)]
        return subprocess.run(cmd, capture_output=True, text=True)

    def render_raw(self, values=None, namespace=None):
        """Returns the CompletedProcess; callers must check ``returncode``."""
        return self.helm_template(values, namespace)

    def render_stdout(self, values=None, namespace=None):
        """Asserts success and returns the raw rendered stream."""
        result = self.helm_template(values, namespace)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def render(self, values=None, namespace=None):
        """Renders the values; asserts success and returns all docs."""
        result = self.render_raw(values, namespace)
        self.assertEqual(result.returncode, 0, result.stderr)
        return docs(result.stdout)

    def render_failure(self, values=None, namespace=None):
        """Renders values expected to trip validation; returns stderr."""
        result = self.render_raw(values, namespace)
        self.assertNotEqual(
            result.returncode, 0,
            "expected a validation failure, but rendering succeeded")
        return result.stderr

    @staticmethod
    def by_kind(rendered):
        by_kind = {}
        for d in rendered:
            by_kind.setdefault(d["kind"], []).append(d)
        return by_kind

    @staticmethod
    def pick(by_kind, kind, name, namespace=None):
        matches = [d for d in by_kind[kind]
                   if d["metadata"]["name"] == name
                   and (namespace is None
                        or d["metadata"].get("namespace") == namespace)]
        assert len(matches) == 1, (kind, name, by_kind[kind])
        return matches[0]


class FakeOcScriptFixture(ChartFixture):
    """Executes rendered hook scripts against a fake ``oc`` binary.

    Each run gets a restricted PATH built solely from symlinks in an
    isolated bin directory (the configured tools, python3 when requested,
    and the fake oc), so no ambient host tool or real oc can be reached.
    The child environment is an explicit minimal set — no parent variables
    are inherited.

    Override points: ``work_prefix``, ``bin_tools``, ``fake_oc_script``
    (the canned oc program — per chart, since each fake serves different
    resources and records a different call surface), ``get_file_name`` and
    ``scripts`` (the staged rendered scripts), and the ``canned_doc`` and
    ``script_env`` hooks.
    """

    work_prefix = "chart-script-"
    bin_tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env", "sort"]
    fake_oc_script = None
    get_file_name = None
    scripts = {}

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix=self.work_prefix)
        self.addCleanup(shutil.rmtree, self.work)

    def build_bin_dir(self, include_python=True, include_oc=True):
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for name in os.listdir(bin_dir):
            os.unlink(os.path.join(bin_dir, name))
        tools = list(self.bin_tools)
        if include_python:
            self.assertTrue(
                shutil.which("python3"),
                "python3 requested but not available on the host")
            tools.append("python3")
        for tool in tools:
            os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
        if include_oc:
            self.assertIsNotNone(
                self.fake_oc_script,
                "fake_oc_script must name the canned oc program")
            fake_oc = os.path.join(bin_dir, "oc")
            with open(fake_oc, "w") as f:
                f.write(self.fake_oc_script)
            os.chmod(fake_oc, 0o755)
        return bin_dir

    def stage_scripts(self, scripts=None):
        # Every staged file lands in the same directory as the entry script
        # because the scripts source their shared preamble from their own
        # directory, mirroring the single ConfigMap mount.
        for name, content in (self.scripts if scripts is None
                              else scripts).items():
            with open(os.path.join(self.work, name), "w") as f:
                f.write(content)

    def clear_run_state(self, *extra):
        # Each run is self-contained: drop logs, captured payloads, and the
        # fake oc's resourceVersion counters left by any earlier run.
        for stale in ("commands.log", "patches.log", *extra):
            stale_path = os.path.join(self.work, stale)
            if os.path.exists(stale_path):
                os.unlink(stale_path)
        for stale in os.listdir(self.work):
            if stale.endswith(".rv"):
                os.unlink(os.path.join(self.work, stale))

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def log_lines(self):
        return self.log().splitlines()

    def patch_lines(self):
        path = os.path.join(self.work, "patches.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [line for line in f.read().splitlines() if line]

    def patches(self):
        return [json.loads(line) for line in self.patch_lines()]

    def run_child(self, script_path, env):
        return subprocess.run(
            ["/bin/bash", script_path], capture_output=True, text=True,
            env=env, timeout=120)

    def canned_doc(self):
        """Canned object the fake oc serves for get."""
        raise NotImplementedError

    def script_env(self):
        """Capability-specific entries merged into the child environment."""
        return {}

    def run_script(self, script_name, canned_doc=None, include_python=True):
        # Single tool path for the smoke set: python3-only restricted PATH.
        bin_dir = self.build_bin_dir(include_python=include_python)
        self.stage_scripts()
        if canned_doc is None:
            canned_doc = self.canned_doc()
        get_file = os.path.join(self.work, self.get_file_name)
        with open(get_file, "w") as f:
            json.dump(canned_doc, f)
        self.clear_run_state()
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            **self.script_env(),
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_GET_FILE": get_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
        }
        return self.run_child(os.path.join(self.work, script_name), env)
