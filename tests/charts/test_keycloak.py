"""Focused tests for the keycloak chart. The module covers the
KeycloakRealmImport boolean-field contract: default-true fields (realm
loginWithEmailAllowed/bruteForceProtected, client
enabled/standardFlowEnabled/directAccessGrantsEnabled/serviceAccountsEnabled,
user enabled/emailVerified) render true when the values key is absent or
null and verbatim when explicitly true or false, and client
fullScopeAllowed is omitted when absent or null and rendered verbatim
when explicitly true or false. It also pins the apiVersion of the
rendered Keycloak and KeycloakRealmImport custom resources to
k8s.keycloak.org/v2alpha1, and pins the serving-certificate Service
that carries the service-ca annotation.

Render tests run the rendered chart through ``helm template`` against a
local stub of the postgres-cluster dependency (no registry access). No
real API or network is contacted.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import yaml

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "keycloak"))

CLIENT = "test-client"
USER = "alice"


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


class ChartFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kc-test-")
        shutil.copytree(
            CHART, self.dir, dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("charts", "Chart.lock"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def stub_dependency(self):
        dep = os.path.join(self.dir, "charts", "postgres-cluster")
        os.makedirs(dep, exist_ok=True)
        with open(os.path.join(dep, "Chart.yaml"), "w") as f:
            f.write("apiVersion: v2\nname: postgres-cluster\nversion: 0.1.0\n")

    def values_file(self, values):
        fd, path = tempfile.mkstemp(prefix="kc-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(values, f)
        self.addCleanup(os.unlink, path)
        return path

    def render_docs(self, realm):
        """Renders a single-realm values set; returns all rendered docs."""
        self.stub_dependency()
        result = subprocess.run(
            ["helm", "template", "test-release", self.dir,
             "--namespace", "test-release-ns",
             "--values", self.values_file({"realms": {"test": realm}})],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return docs(result.stdout)

    def render_realm(self, realm):
        """Renders a single-realm values set; returns the realm spec of the
        rendered KeycloakRealmImport."""
        for d in self.render_docs(realm):
            if (d["kind"] == "KeycloakRealmImport"
                    and d["metadata"]["name"] == "test"):
                return d["spec"]["realm"]
        self.fail("KeycloakRealmImport 'test' not rendered")

    def base_realm(self):
        return {"clients": {CLIENT: {}}, "users": {USER: {}}}

    @staticmethod
    def set_field(realm, scope, field, value):
        if scope == "realm":
            realm[field] = value
        elif scope == "client":
            realm["clients"][CLIENT][field] = value
        else:
            realm["users"][USER][field] = value

    @staticmethod
    def get_field(realm, scope, field):
        if scope == "realm":
            return realm[field]
        if scope == "client":
            return realm["clients"][0][field]
        return next(u for u in realm["users"]
                    if u["username"] == USER)[field]


DEFAULT_TRUE_FIELDS = [
    ("realm", "loginWithEmailAllowed"),
    ("realm", "bruteForceProtected"),
    ("client", "enabled"),
    ("client", "standardFlowEnabled"),
    ("client", "directAccessGrantsEnabled"),
    ("client", "serviceAccountsEnabled"),
    ("user", "enabled"),
    ("user", "emailVerified"),
]


class DefaultTrueFieldsTests(ChartFixture):
    def test_absent_or_null_renders_true(self):
        for scope, field in DEFAULT_TRUE_FIELDS:
            for case in ("absent", None):
                realm = self.base_realm()
                if case is None:
                    self.set_field(realm, scope, field, None)
                rendered = self.render_realm(realm)
                self.assertIs(
                    self.get_field(rendered, scope, field), True,
                    (scope, field, case))

    def test_explicit_booleans_render_verbatim(self):
        for scope, field in DEFAULT_TRUE_FIELDS:
            for value in (True, False):
                realm = self.base_realm()
                self.set_field(realm, scope, field, value)
                rendered = self.render_realm(realm)
                self.assertIs(
                    self.get_field(rendered, scope, field), value,
                    (scope, field, value))


class FullScopeAllowedTests(ChartFixture):
    def client_full_scope(self, realm):
        return self.render_realm(realm)["clients"][0]

    def test_absent_or_null_omits_the_key(self):
        for case in ("absent", None):
            realm = self.base_realm()
            if case is None:
                realm["clients"][CLIENT]["fullScopeAllowed"] = None
            self.assertNotIn("fullScopeAllowed", self.client_full_scope(realm))

    def test_explicit_booleans_render_verbatim(self):
        for value in (True, False):
            realm = self.base_realm()
            realm["clients"][CLIENT]["fullScopeAllowed"] = value
            self.assertIs(
                self.client_full_scope(realm)["fullScopeAllowed"], value)


class BaselineRenderTests(ChartFixture):
    def test_custom_resource_api_versions_pin_v2alpha1(self):
        # Pinned to v2alpha1 because operators at least as old as 26.2.16
        # (the target cluster's version) reject v2beta1; later releases
        # (26.6+) still serve v2alpha1, deprecated but with identical schema.
        kinds = {d["kind"]: d["apiVersion"]
                 for d in self.render_docs(self.base_realm())}
        for kind in ("Keycloak", "KeycloakRealmImport"):
            self.assertEqual(
                kinds[kind], "k8s.keycloak.org/v2alpha1", kind)

    def test_serving_cert_service_carries_annotation(self):
        # The Service is metadata-only: the operator co-owns it (same name,
        # <cr-name>-service) and fills spec.selector/ports, so the chart
        # deliberately renders no spec.
        services = [d for d in self.render_docs(self.base_realm())
                    if d["kind"] == "Service"]
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["metadata"]["name"], "keycloak-service")
        self.assertEqual(
            services[0]["metadata"]["annotations"][
                "service.beta.openshift.io/serving-cert-secret-name"],
            "keycloak-internal-tls")
        self.assertNotIn("spec", services[0])

    def test_default_render_matches_contract(self):
        # No boolean field set anywhere: the eight default-true fields render
        # true and fullScopeAllowed is absent.
        rendered = self.render_realm(self.base_realm())
        for scope, field in DEFAULT_TRUE_FIELDS:
            self.assertIs(self.get_field(rendered, scope, field), True,
                          (scope, field))
        self.assertNotIn("fullScopeAllowed", rendered["clients"][0])
