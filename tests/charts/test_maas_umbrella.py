"""Regression tests for the maas-umbrella example composition. The module
covers the umbrella-level wiring contract: the keycloak subchart reads
`realms` from the umbrella's top-level values (a `realms` block nested under
another subchart's key renders nothing), the ocp-idp client disables direct
access grants, and the keycloak subchart's resources land in the release
namespace so they co-locate inside the RHBK OperatorGroup's watch scope.

Render tests copy the umbrella and the real keycloak chart into a temp dir,
stub the three subchart dependencies locally (postgres-cluster nested inside
keycloak is a bare stub), and run ``helm template``. No real API or network
is contacted.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, "..", ".."))
UMBRELLA = os.path.join(REPO_ROOT, "examples", "maas-umbrella")
KEYCLOAK = os.path.join(REPO_ROOT, "charts", "keycloak")

RELEASE_NS = "keycloak"


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


class UmbrellaFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="umbrella-test-")
        self.addCleanup(shutil.rmtree, self.dir)
        self.umbrella = self.build_umbrella()

    def build_umbrella(self):
        """Copies the umbrella and its subchart dependencies into a local
        composition: the real keycloak chart with a stubbed nested
        postgres-cluster, and bare stubs for keycloak-auth and
        openshift-ai."""
        umbrella = os.path.join(self.dir, "maas-umbrella")
        shutil.copytree(
            UMBRELLA, umbrella, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("charts", "Chart.lock"))

        subcharts = os.path.join(umbrella, "charts")
        keycloak = os.path.join(subcharts, "keycloak")
        shutil.copytree(
            KEYCLOAK, keycloak, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("charts", "Chart.lock"))
        self.stub_chart(os.path.join(keycloak, "charts", "postgres-cluster"))

        for name in ("keycloak-auth", "openshift-ai"):
            self.stub_chart(os.path.join(subcharts, name))

        self.rewrite_dependencies(umbrella, subcharts)
        self.run_cmd(["helm", "dependency", "build", umbrella])
        return umbrella

    @staticmethod
    def stub_chart(path):
        os.makedirs(os.path.join(path, "templates"))
        with open(os.path.join(path, "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: %s\nversion: 0.0.0\n"
                    % os.path.basename(path))
        with open(os.path.join(path, "templates", "stub.yaml"), "w") as f:
            f.write("# stub: renders no objects\n")

    @staticmethod
    def rewrite_dependencies(chart_dir, subcharts):
        with open(os.path.join(chart_dir, "Chart.yaml")) as f:
            chart = yaml.safe_load(f)
        for dep in chart["dependencies"]:
            dep["repository"] = "file://" + os.path.join(subcharts, dep["name"])
            dep["version"] = "*"
        with open(os.path.join(chart_dir, "Chart.yaml"), "w") as f:
            yaml.safe_dump(chart, f, sort_keys=False)

    @staticmethod
    def run_cmd(cmd):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError("%s failed: %s" % (cmd[0], result.stderr))

    def render(self):
        result = subprocess.run(
            ["helm", "template", "maas-umbrella", self.umbrella,
             "--namespace", RELEASE_NS],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return docs(result.stdout)


class RealmCompositionTests(UmbrellaFixture):
    def realm_import(self):
        rendered = [d for d in self.render()
                    if d["kind"] == "KeycloakRealmImport"]
        self.assertEqual(
            [d["metadata"]["name"] for d in rendered], ["sso"],
            "exactly the global.oidc.realm must render a KeycloakRealmImport")
        return rendered[0]

    def test_realm_sso_renders_from_top_level_values(self):
        realm = self.realm_import()["spec"]["realm"]
        self.assertEqual(realm["realm"], "sso")
        self.assertEqual(realm["id"], "sso")

    def test_ocp_idp_disables_direct_access_grants(self):
        realm = self.realm_import()["spec"]["realm"]
        client = next(c for c in realm["clients"]
                      if c["clientId"] == "ocp-idp")
        self.assertIs(client["directAccessGrantsEnabled"], False)


class ReleaseNamespaceTests(UmbrellaFixture):
    def test_keycloak_resources_co_locate_in_the_release_namespace(self):
        rendered = [d for d in self.render()
                    if d["kind"] in ("Keycloak", "KeycloakRealmImport",
                                     "Route")]
        self.assertEqual(
            {d["kind"] for d in rendered},
            {"Keycloak", "KeycloakRealmImport", "Route"})
        for d in rendered:
            self.assertEqual(d["metadata"]["namespace"], RELEASE_NS,
                             (d["kind"], d["metadata"]["name"]))
