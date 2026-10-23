"""Regression tests for the Python chart CI helpers.

Network (urllib), subprocess, and sleep calls are mocked; no Helm or registry
token is required.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chart_ci
import chart_metadata
import chart_versions


class ChartMetadataTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write_chart(self, text):
        with open(os.path.join(self.dir, chart_metadata.CHART_FILE), "w") as f:
            f.write(text)

    def test_loads_reordered_quoted_fields(self):
        self.write_chart('version: "1.2.3"\nname: ' + "'my-chart'\n")
        chart = chart_metadata.load_chart(self.dir)
        self.assertEqual(chart["name"], "my-chart")
        self.assertEqual(chart["version"], "1.2.3")

    def test_flow_dependencies_list(self):
        self.write_chart(
            "name: c\nversion: 1.0.0\ndependencies: [{name: dep, repository: oci://x}]\n")
        self.assertEqual(
            chart_metadata.dependencies(self.dir),
            [{"name": "dep", "repository": "oci://x"}])

    def test_missing_file_raises(self):
        with self.assertRaises(chart_metadata.ChartMetadataError):
            chart_metadata.load_chart(self.dir)

    def test_invalid_yaml_raises(self):
        self.write_chart("name: [unclosed\n")
        with self.assertRaises(chart_metadata.ChartMetadataError):
            chart_metadata.load_chart(self.dir)

    def test_missing_required_field_raises(self):
        self.write_chart("name: c\n")
        with self.assertRaises(chart_metadata.ChartMetadataError):
            chart_metadata.load_chart(self.dir)

    def test_dependency_without_name_raises(self):
        self.write_chart("name: c\nversion: 1\ndependencies:\n  - repository: oci://x\n")
        with self.assertRaises(chart_metadata.ChartMetadataError):
            chart_metadata.load_chart(self.dir)


class PreparerTest(unittest.TestCase):
    """Preparer tests build a tiny fake repo graph with stub helm."""

    def setUp(self):
        self.root_ctx = mock.patch.object(chart_ci, "REPO_ROOT", "/nonexistent")
        self.root_ctx.start()
        self.addCleanup(self.root_ctx.stop)
        real_run = subprocess.run
        self.helm_ctx = mock.patch.object(chart_ci.subprocess, "run")
        self.helm = self.helm_ctx.start()
        # Real filesystem commands (cp/rm) run; helm calls are stubbed.
        self.helm.side_effect = lambda args, **kw: (
            subprocess.CompletedProcess(args, 0)
            if args and args[0] == "helm" else real_run(args, **kw))
        self.addCleanup(self.helm_ctx.stop)

    def chart(self, name, deps=(), version="1.0.0"):
        d = tempfile.mkdtemp(prefix=name + "-")
        with open(os.path.join(d, chart_metadata.CHART_FILE), "w") as f:
            f.write(f"name: {name}\nversion: {version}\n")
            if deps:
                f.write("dependencies:\n")
                for dep_name, repo in deps:
                    f.write(f"  - name: {dep_name}\n    repository: {repo}\n")
                    f.write("    alias: pg\n    condition: pg.enabled\n")
        return d

    def prepared_chart(self, dest):
        with open(os.path.join(dest, chart_metadata.CHART_FILE)) as f:
            return chart_metadata.load_chart(dest)

    def test_self_cycle_fails(self):
        d = self.chart("selfy", deps=[("selfy", "oci://ghcr.io/o/helm-charts")])
        with self.assertRaises(SystemExit):
            chart_ci.prepare_local(d)

    def test_two_node_cycle_fails(self):
        a = self.chart("cyc-a", deps=[("cyc-b", "oci://ghcr.io/o/helm-charts")])
        b = self.chart("cyc-b", deps=[("cyc-a", "oci://ghcr.io/o/helm-charts")])
        self.assertTrue(os.path.isdir(b))
        with mock.patch.dict(os.environ, {}):
            with self.assertRaises(SystemExit):
                chart_ci.prepare_local(a, cache={}, stack=[])
        # Point REPO_ROOT at the scratch charts to form the real cycle.
        scratch = tempfile.mkdtemp()
        os.makedirs(os.path.join(scratch, "charts"))
        os.symlink(a, os.path.join(scratch, "charts", "cyc-a"))
        os.symlink(b, os.path.join(scratch, "charts", "cyc-b"))
        with mock.patch.object(chart_ci, "REPO_ROOT", scratch):
            with self.assertRaises(SystemExit):
                chart_ci.prepare_local(a, cache={}, stack=[])

    def test_diamond_prepares_shared_dep_once(self):
        a = self.chart("dia-a", version="1.0.0")
        b = self.chart("dia-b", version="1.0.0")
        scratch = tempfile.mkdtemp()
        os.makedirs(os.path.join(scratch, "charts"))
        for name, d in (("dia-a", a), ("dia-b", b)):
            os.symlink(d, os.path.join(scratch, "charts", name))
        left = self.chart("dia-left", deps=[("dia-a", "oci://ghcr.io/o/helm-charts")])
        right = self.chart("dia-right", deps=[("dia-a", "oci://ghcr.io/o/helm-charts")])
        top = self.chart(
            "dia-top",
            deps=[("dia-left", "oci://ghcr.io/o/helm-charts"),
                  ("dia-right", "oci://ghcr.io/o/helm-charts")])
        os.symlink(left, os.path.join(scratch, "charts", "dia-left"))
        os.symlink(right, os.path.join(scratch, "charts", "dia-right"))
        cache = {}
        with mock.patch.object(chart_ci, "REPO_ROOT", scratch):
            dest = chart_ci.prepare_local(top, cache=cache, stack=[])
        # dia-a was prepared once: its cached path appears in both siblings'
        # prepared Chart.yamls, and exactly one cache entry exists for it.
        self.assertEqual(sum(1 for k in cache if k == "dia-a"), 1)
        prepared = self.prepared_chart(dest)
        repos = [d["repository"] for d in prepared["dependencies"]]
        self.assertTrue(all(r.startswith("file://") for r in repos))
        for dep in prepared["dependencies"]:
            self.assertEqual(dep["alias"], "pg")
            self.assertEqual(dep["condition"], "pg.enabled")

    def test_external_dependency_fails(self):
        d = self.chart("ext", deps=[("other", "https://charts.example.com")])
        with self.assertRaises(SystemExit):
            chart_ci.prepare_local(d)

    def test_unmapped_intra_repo_dep_fails(self):
        d = self.chart("mapless", deps=[("missing", "oci://ghcr.io/o/helm-charts")])
        with self.assertRaises(SystemExit):
            chart_ci.prepare_local(d)

    def test_source_immutable_and_artifacts_excluded(self):
        src = self.chart("imm")
        chart_file = Path(src, chart_metadata.CHART_FILE)
        before = chart_file.read_text()
        dest = chart_ci.prepare_local(src, cache={}, stack=[])
        self.assertEqual(chart_file.read_text(), before)
        self.assertNotEqual(src, dest)
        self.assertFalse(os.path.exists(os.path.join(dest, "charts")))
        self.assertFalse(os.path.exists(os.path.join(dest, "Chart.lock")))

    def test_stdout_purity(self):
        d = self.chart("pure")
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            result = chart_ci.prepare_local(d, cache={}, stack=[])
        self.assertEqual(out.getvalue(), "")
        self.assertTrue(result)


class LintAndBuildTest(unittest.TestCase):
    def setUp(self):
        self.helm_ctx = mock.patch.object(chart_ci.subprocess, "run")
        self.helm = self.helm_ctx.start()
        self.helm.return_value = subprocess.CompletedProcess([], 0)
        self.addCleanup(self.helm_ctx.stop)

    def chart(self, deps=None):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, chart_metadata.CHART_FILE), "w") as f:
            f.write("name: linty\nversion: 1.0.0\n")
            if deps:
                f.write("dependencies:\n  - name: dep\n    repository: oci://x\n")
        return d

    def test_lint_runs_lint_and_template(self):
        d = self.chart()
        chart_ci.lint(d)
        self.assertEqual(
            [call.args[0][1] for call in self.helm.call_args_list],
            ["lint", "template"])

    def test_build_registry_retries_then_fails(self):
        d = self.chart(deps=True)
        self.helm.side_effect = subprocess.CalledProcessError(1, "helm")
        with mock.patch.object(chart_ci.time, "sleep") as slept:
            with self.assertRaises(SystemExit):
                chart_ci.build_registry(d)
        self.assertEqual(self.helm.call_count, 3)
        self.assertEqual(slept.call_count, 2)

    def test_build_registry_skips_dependencyless(self):
        d = self.chart()
        chart_ci.build_registry(d)
        self.helm.assert_not_called()

    def test_lint_failure_propagates_before_package(self):
        d = self.chart()
        self.helm.side_effect = subprocess.CalledProcessError(1, "helm")
        with self.assertRaises(subprocess.CalledProcessError):
            chart_ci.lint(d)


class UrllibTransportTest(unittest.TestCase):
    """Drive fetch_token/probe_manifest through a mocked urlopen."""

    @staticmethod
    def response(status, payload=None):
        resp = mock.MagicMock()
        resp.status = status
        resp.read.return_value = json.dumps(payload).encode() if payload is not None else b""
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        return resp

    def setUp(self):
        self.urlopen_ctx = mock.patch.object(
            chart_versions.urllib.request, "urlopen")
        self.urlopen = self.urlopen_ctx.start()
        self.addCleanup(self.urlopen_ctx.stop)

    def test_token_200_with_token(self):
        self.urlopen.return_value = self.response(
            200, {"token": "tok-123"})
        self.assertEqual(chart_versions.fetch_token("r/x"), "tok-123")

    def test_token_401_403_denied(self):
        for status in (401, 403):
            self.urlopen.return_value = self.response(status)
            with self.assertRaises(chart_versions.PackageDenied):
                chart_versions.fetch_token("r/x")

    def test_token_other_status_fatal(self):
        for status in (400, 404, 500):
            self.urlopen.return_value = self.response(status)
            with self.assertRaises(chart_versions.RegistryError):
                chart_versions.fetch_token("r/x")

    def test_token_empty_and_malformed_fatal(self):
        for payload in ({}, {"token": ""}, {"token": None}, [], "x"):
            self.urlopen.return_value = self.response(200, payload)
            with self.assertRaises(chart_versions.RegistryError):
                chart_versions.fetch_token("r/x")
        import urllib.error
        self.urlopen.side_effect = urllib.error.URLError("boom")
        with self.assertRaises(chart_versions.RegistryError):
            chart_versions.fetch_token("r/x")

    def test_token_network_error_fatal(self):
        import urllib.error
        self.urlopen.side_effect = urllib.error.URLError("boom")
        with self.assertRaises(chart_versions.RegistryError):
            chart_versions.fetch_token("r/x")

    def test_token_basic_header_when_github_token(self):
        self.urlopen.return_value = self.response(200, {"token": "t"})
        with mock.patch.dict(chart_versions.os.environ,
                             {"GITHUB_TOKEN": "sekrit", "GITHUB_ACTOR": "bob"}):
            chart_versions.fetch_token("r/x")
        headers = self.urlopen.call_args.args[0].headers
        self.assertIn("Authorization", headers)
        self.assertNotIn("sekrit", str(headers))
        self.assertNotIn("sekrit", repr(self.urlopen.call_args))

    def test_token_no_header_anonymous(self):
        self.urlopen.return_value = self.response(200, {"token": "t"})
        with mock.patch.dict(chart_versions.os.environ, {}, clear=True):
            chart_versions.fetch_token("r/x")
        self.assertNotIn("Authorization", self.urlopen.call_args.args[0].headers)

    def test_manifest_200_exists_404_absent(self):
        self.urlopen.return_value = self.response(200)
        self.assertTrue(chart_versions.probe_manifest("r", "1.0.0", "t"))
        self.urlopen.return_value = self.response(404)
        self.assertFalse(chart_versions.probe_manifest("r", "1.0.0", "t"))
        request = self.urlopen.call_args.args[0]
        self.assertEqual(request.headers["Accept"], chart_versions.ACCEPT)

    def test_manifest_other_status_fatal(self):
        for status in (401, 403, 500):
            self.urlopen.return_value = self.response(status)
            with self.assertRaises(chart_versions.RegistryError):
                chart_versions.probe_manifest("r", "1.0.0", "t")

    def test_manifest_network_error_fatal(self):
        import urllib.error
        self.urlopen.side_effect = urllib.error.URLError("boom")
        with self.assertRaises(chart_versions.RegistryError):
            chart_versions.probe_manifest("r", "1.0.0", "t")


class CanaryTest(unittest.TestCase):
    def setUp(self):
        self.urlopen_ctx = mock.patch.object(
            chart_versions.urllib.request, "urlopen")
        self.urlopen = self.urlopen_ctx.start()
        self.addCleanup(self.urlopen_ctx.stop)
        self.ok_response = UrllibTransportTest.response(
            200, {"token": "t"})
        self.urlopen.return_value = self.ok_response

    def test_canary_passes_on_readable_repo(self):
        # Token 200, tags list 200 with a tag, manifest 200.
        token_resp = UrllibTransportTest.response(200, {"token": "t"})
        tags_resp = UrllibTransportTest.response(200, {"tags": ["1.0.0"]})
        self.urlopen.side_effect = [token_resp, tags_resp]
        with mock.patch.object(chart_versions, "probe_manifest", return_value=True):
            self.assertTrue(chart_versions.canary_ok("ghcr.io/o/helm-charts", ["c"]))

    def test_canary_fails_when_nothing_readable(self):
        with mock.patch.object(chart_versions, "fetch_token",
                               side_effect=chart_versions.PackageDenied("x")):
            self.assertFalse(chart_versions.canary_ok("ghcr.io/o/helm-charts", ["c"]))

    def test_canary_fails_on_empty_tag_list(self):
        self.ok_response.read.return_value = json.dumps({"tags": []}).encode()
        self.assertFalse(chart_versions.canary_ok("ghcr.io/o/helm-charts", ["c"]))

    def test_canary_fails_when_manifest_absent(self):
        with mock.patch.object(chart_versions, "probe_manifest", return_value=False):
            self.assertFalse(chart_versions.canary_ok("ghcr.io/o/helm-charts", ["c"]))


class PublishSelectionTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.charts = os.path.join(self.repo, "charts")
        os.makedirs(self.charts)
        self.urlopen_ctx = mock.patch.object(
            chart_versions.urllib.request, "urlopen")
        self.urlopen = self.urlopen_ctx.start()
        self.addCleanup(self.urlopen_ctx.stop)
        self.urlopen.return_value = UrllibTransportTest.response(
            200, {"token": "t"})
        self.canary_ctx = mock.patch.object(
            chart_versions, "canary_ok", return_value=True)
        self.canary_ctx.start()
        self.addCleanup(self.canary_ctx.stop)
        self.env = {
            "REGISTRY_OWNER": "test-org",
            "GITHUB_OUTPUT": os.path.join(self.repo, "out.txt"),
        }

    def add_chart(self, name, version):
        d = os.path.join(self.charts, name)
        os.makedirs(d)
        with open(os.path.join(d, chart_metadata.CHART_FILE), "w") as f:
            f.write(f"name: {name}\nversion: {version}\n")

    def run_gate(self):
        prev_cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, prev_cwd)
        with mock.patch.dict(chart_versions.os.environ, self.env, clear=True):
            with mock.patch("sys.stdout", new=io.StringIO()):
                code = chart_versions.main(["chart_versions.py"])
        # The output file is absent when the gate failed before writing.
        output = self.env["GITHUB_OUTPUT"]
        content = Path(output).read_text() if os.path.exists(output) else None
        return code, content

    def test_current_repo_publish_false_empty_matrix(self):
        self.add_chart("pub", "1.0.0")
        with mock.patch.object(chart_versions, "probe_manifest", return_value=True):
            code, out = self.run_gate()
        self.assertEqual(code, 0)
        self.assertIn("publish=false\n", out)
        self.assertIn("matrix=" + json.dumps({"include": []}), out)

    def test_bumped_chart_selects_only_it(self):
        self.add_chart("pub", "1.0.0")
        self.add_chart("bumped", "2.0.0")
        with mock.patch.object(chart_versions, "probe_manifest",
                               side_effect=lambda repo, tag, tok: repo.endswith("pub")):
            code, out = self.run_gate()
        self.assertEqual(code, 0)
        self.assertIn("publish=true\n", out)
        matrix = json.loads(out.split("matrix=", 1)[1].strip())
        self.assertEqual([e["name"] for e in matrix["include"]], ["bumped"])
        self.assertEqual(matrix["include"][0]["path"], os.path.join("charts", "bumped"))
        self.assertEqual(matrix["include"][0]["version"], "2.0.0")

    def test_persistent_denial_warns_and_selects(self):
        self.add_chart("denied", "1.0.0")
        def deny(*args):
            raise chart_versions.PackageDenied("denied")
        with mock.patch.object(chart_versions, "fetch_token", side_effect=deny):
            with mock.patch.object(chart_versions.time, "sleep") as slept:
                with mock.patch("sys.stderr", new=io.StringIO()) as err:
                    code, out = self.run_gate()
        self.assertEqual(code, 0)
        self.assertIn("publish=true\n", out)
        self.assertIn("treating chart as new", err.getvalue())
        self.assertEqual(slept.call_count, 3)

    def test_name_mismatch_fails(self):
        self.add_chart("wrong", "1.0.0")
        with open(os.path.join(self.charts, "wrong",
                               chart_metadata.CHART_FILE), "w") as f:
            f.write("name: other\nversion: 1.0.0\n")
        with mock.patch("sys.stderr", new=io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                self.run_gate()
        self.assertIn("does not match directory basename 'wrong'", err.getvalue())

    def test_registry_failure_boundary_reports_clean_error(self):
        self.add_chart("pub", "1.0.0")

        def fail_token(*args):
            raise chart_versions.RegistryError("token request for r returned HTTP 500")
        self.env["GITHUB_TOKEN"] = "sekrit"
        self.env["GITHUB_ACTOR"] = "bob"
        # canary_ok is mocked True, so a failing fetch_token only reaches the
        # post-canary selection loop; asserting the HTTP-500 status info
        # distinguishes the new catch from the canary-fail path.
        with mock.patch.object(chart_versions, "fetch_token", side_effect=fail_token):
            with mock.patch("sys.stderr", new=io.StringIO()) as err:
                code = self.run_gate()[0]
        self.assertNotEqual(code, 0)
        self.assertIn("::error::token request for r returned HTTP 500", err.getvalue())
        self.assertNotIn("auth/registry unavailable", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertNotIn("sekrit", err.getvalue())
        self.assertFalse(os.path.exists(self.env["GITHUB_OUTPUT"]))

    def test_matrix_json_escapes_special_chars(self):
        self.add_chart('q"uote', "1.0.0")
        with mock.patch.object(chart_versions, "probe_manifest", return_value=False):
            code, out = self.run_gate()
        self.assertEqual(code, 0)
        matrix = json.loads(out.split("matrix=", 1)[1].strip())
        self.assertEqual(matrix["include"][0]["name"], 'q"uote')


if __name__ == "__main__":
    unittest.main()
