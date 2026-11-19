"""Focused tests for the keycloak chart: the default render's contract
(apiVersion pins, serving-certificate Service annotation, keycloak
namespace pinning, the default-true boolean contract, and the single-logout
client-attribute injection), plus the postgres-namespace render guard.

Render tests run the rendered chart through ``helm template`` against a
local stub of the postgres-cluster dependency (no registry access). No
real API or network is contacted.
"""

import os

import support

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "keycloak"))

CLIENT = "test-client"
USER = "alice"

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


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "kc-test-"
    values_prefix = "kc-values-"

    def helm_template(self, values=None, namespace=None):
        self.stub_dependency()
        return super().helm_template(values, namespace)

    def base_realm(self):
        return {"clients": {CLIENT: {}}, "users": {USER: {}}}

    def render_realm(self, realm):
        """Renders a single-realm values set; returns the realm spec of the
        rendered KeycloakRealmImport."""
        for d in self.render({"realms": {"test": realm}}):
            if (d["kind"] == "KeycloakRealmImport"
                    and d["metadata"]["name"] == "test"):
                return d["spec"]["realm"]
        self.fail("KeycloakRealmImport 'test' not rendered")

    def rendered_client(self, values, realm_name, client_id):
        """Renders arbitrary values; returns the realm client with the
        given clientId from the named KeycloakRealmImport."""
        for d in self.render(values):
            if (d["kind"] == "KeycloakRealmImport"
                    and d["metadata"]["name"] == realm_name):
                return next(c for c in d["spec"]["realm"]["clients"]
                            if c["clientId"] == client_id)
        self.fail(f"KeycloakRealmImport '{realm_name}' not rendered")

    @staticmethod
    def get_field(realm, scope, field):
        if scope == "realm":
            return realm[field]
        if scope == "client":
            return realm["clients"][0][field]
        return next(u for u in realm["users"]
                    if u["username"] == USER)[field]


class DefaultRenderTests(ChartFixture):
    def test_default_render_matches_contract(self):
        docs = self.render({"realms": {"test": self.base_realm()}})
        # Pinned to v2alpha1 because operators at least as old as 26.2.16
        # (the target cluster's version) reject v2beta1; later releases
        # (26.6+) still serve v2alpha1, deprecated but with identical schema.
        kinds = {d["kind"]: d["apiVersion"] for d in docs}
        for kind in ("Keycloak", "KeycloakRealmImport"):
            self.assertEqual(kinds[kind], "k8s.keycloak.org/v2alpha1", kind)
        # The RHBK OperatorGroup watches the keycloak namespace only, so
        # every template pins it outright — the release namespace is free
        # (the fixture renders into test-release-ns)
        for d in docs:
            self.assertEqual(d["metadata"]["namespace"], "keycloak", d)
        # The Service is metadata-only: the operator co-owns it (same name,
        # <cr-name>-service) and fills spec.selector/ports, so the chart
        # deliberately renders no spec.
        services = [d for d in docs if d["kind"] == "Service"]
        self.assertEqual(len(services), 1)
        self.assertEqual(services[0]["metadata"]["name"], "keycloak-service")
        self.assertEqual(
            services[0]["metadata"]["annotations"][
                "service.beta.openshift.io/serving-cert-secret-name"],
            "keycloak-internal-tls")
        # No boolean field set anywhere: the eight default-true fields render
        # true and fullScopeAllowed is absent.
        realm = self.render_realm(self.base_realm())
        for scope, field in DEFAULT_TRUE_FIELDS:
            self.assertIs(self.get_field(realm, scope, field), True,
                          (scope, field))
        self.assertNotIn("fullScopeAllowed", realm["clients"][0])

    def test_matching_client_receives_logout_redirect_attribute(self):
        # The single-logout client-attribute injection: the client named by
        # global.oidc.clientId rendering in the realm named by
        # global.oidc.realm receives post.logout.redirect.uris pointing at
        # the derived console host.
        values = {"realms": {"sso": {"clients": {"ocp-idp": {}}}}}
        client = self.rendered_client(values, "sso", "ocp-idp")
        self.assertEqual(client["attributes"], {
            "post.logout.redirect.uris":
                "https://console-openshift-console.apps.cluster.example.com",
        })


class PostgresNamespaceGuardTests(ChartFixture):
    """The postgres namespace pin is render-enforced: the Keycloak CR pins
    the keycloak namespace and the db host defaults to same-namespace
    short-name DNS, so a Cluster anywhere else silently breaks the database
    connection — the render fails instead."""

    def test_foreign_postgres_namespace_fails_render(self):
        self.render_failure(
            {"postgres": {"enabled": True, "namespace": "database"}})
