"""Render-contract tests for the openshift-ai chart's configurable
GatewayClass references with namespace-selective Route attachment,
per-gateway replicas, and hostname/certificate agreement."""

from oa_support import ChartFixture


class RenderMatrixTests(ChartFixture):
    def test_no_gatewayclass_telemetry_telemetrypolicy(self):
        out = self.by_kind(self.render())
        self.assertNotIn("GatewayClass", out)
        self.assertNotIn("Telemetry", out)
        self.assertNotIn("TelemetryPolicy", out)

    def test_gateway_class_name_with_override(self):
        values = self.managed_maas()
        values["gateways"] = {
            "maasDefaultGateway": {"useRoute": True},
            "openshiftAiInference": {"create": True, "useRoute": True},
        }
        by_kind = self.by_kind(self.render(values))
        gateways = [d for d in by_kind["Gateway"]]
        self.assertTrue(all(g["spec"]["gatewayClassName"] == "data-science-gateway-class"
                            for g in gateways))
        routes = [d for d in by_kind["Route"]]
        self.assertTrue(all(r["spec"]["to"]["name"].endswith("-data-science-gateway-class")
                            for r in routes))

        values["gateways"]["maasDefaultGateway"]["gatewayClassName"] = 'q"uote-class'
        values["gateways"]["openshiftAiInference"]["gatewayClassName"] = "custom-class"
        by_kind = self.by_kind(self.render(values))
        gateways = {d["metadata"]["name"]: d for d in by_kind["Gateway"]}
        routes = {d["metadata"]["name"]: d for d in by_kind["Route"]}
        self.assertEqual(gateways["maas-default-gateway"]["spec"]["gatewayClassName"],
                         'q"uote-class')
        self.assertEqual(routes["maas-default-gateway"]["spec"]["to"]["name"],
                         'maas-default-gateway-q"uote-class')
        self.assertEqual(gateways["openshift-ai-inference"]["spec"]["gatewayClassName"],
                         "custom-class")
        self.assertEqual(routes["openshift-ai-inference"]["spec"]["to"]["name"],
                         "openshift-ai-inference-custom-class")

    def test_replicas_values(self):
        # Default nil omits the patch field entirely.
        values = self.managed_maas()
        values["gateways"] = {"maasDefaultGateway": {"useRoute": False}}
        cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                       "maas-default-gateway-config")
        self.assertNotIn("replicas", cm["data"].get("deployment", ""))

        for explicit in (0, 3):
            values["gateways"]["maasDefaultGateway"]["replicas"] = explicit
            cm = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                           "maas-default-gateway-config")
            self.assertIn(f"replicas: {explicit}", cm["data"]["deployment"])

        for invalid in (-1, 1.5):
            values["gateways"]["maasDefaultGateway"]["replicas"] = invalid
            self.render(values, expect_failure=True)

    def test_use_route_modes(self):
        for use_route in (True, False):
            values = self.managed_maas()
            values["gateways"] = {"maasDefaultGateway": {"useRoute": use_route}}
            by_kind = self.by_kind(self.render(values))
            cm = self.pick(by_kind, "ConfigMap", "maas-default-gateway-config")
            self.assertEqual("service" in cm["data"], use_route)
            # The parametersRef is independent of useRoute.
            self.assertEqual(
                self.pick(by_kind, "Gateway", "maas-default-gateway")
                ["spec"]["infrastructure"]["parametersRef"]["name"],
                "maas-default-gateway-config")
            self.assertEqual("Route" in by_kind, use_route)
            listener = self.pick(by_kind, "Gateway", "maas-default-gateway")[
                "spec"]["listeners"][0]
            self.assertEqual(listener["name"], "http" if use_route else "https")

    def test_inference_use_route_false(self):
        values = self.managed_maas()
        values["gateways"] = {"openshiftAiInference": {"useRoute": False}}
        by_kind = self.by_kind(self.render(values))
        gateway = self.pick(by_kind, "Gateway", "openshift-ai-inference")
        # parametersRef and the deployment overlay render in both useRoute
        # modes; Service/Route behavior is gated by useRoute.
        self.assertEqual(
            gateway["spec"]["infrastructure"]["parametersRef"]["name"],
            "openshift-ai-inference-config")
        self.assertEqual("Route" in by_kind, False)
        cm = self.pick(by_kind, "ConfigMap", "openshift-ai-inference-config")
        self.assertNotIn("service", cm.get("data") or {})
        self.assertNotIn("deployment", cm.get("data") or {})

    def test_route_namespace_selectors(self):
        values = self.managed_maas()
        values["gateways"] = {
            "maasDefaultGateway": {"useRoute": True},
            "openshiftAiInference": {"create": True, "useRoute": True},
        }
        gateways = {d["metadata"]["name"]: d for d in self.render(values)
                    if d["kind"] == "Gateway"}
        for name, label in (("maas-default-gateway", "maas-gateway-access"),
                            ("openshift-ai-inference", "openshift-ai-inference-access")):
            routes_ns = gateways[name]["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]
            self.assertEqual(routes_ns["from"], "Selector")
            self.assertEqual(routes_ns["selector"]["matchLabels"], {label: "true"})

        values["gateways"]["openshiftAiInference"]["routeNamespaceSelector"] = {
            "matchExpressions": [{"key": "k", "operator": "In", "values": ["a", "b"]}]}
        gateway = next(d for d in self.render(values)
                       if d["kind"] == "Gateway"
                       and d["metadata"]["name"] == "openshift-ai-inference")
        selector = gateway["spec"]["listeners"][0]["allowedRoutes"]["namespaces"]["selector"]
        self.assertEqual(
            selector["matchExpressions"],
            [{"key": "k", "operator": "In", "values": ["a", "b"]}])

    def test_nonmap_selector_fails(self):
        # A user-supplied empty map merges back to the chart-defined default
        # (Helm cannot clear a default map with {}), so the render-time
        # rejection guards non-map selector values.
        self.render(self.managed_maas() | {"gateways": {
            "maasDefaultGateway": {"useRoute": True,
                                   "routeNamespaceSelector": "scalar"}}},
            expect_failure=True)

    def test_nulled_selector_leaf_fails(self):
        # Nulling the default matchLabels leaf DELETES it (Helm drops explicit
        # nulls for keys that exist in chart defaults), leaving an empty
        # selector that would allow routes from all namespaces.
        self.render(self.managed_maas() | {"gateways": {
            "maasDefaultGateway": {"useRoute": True,
                                   "routeNamespaceSelector": {"matchLabels": None}}}},
            expect_failure=True)

    def test_nulled_gateway_class_fails(self):
        # Nulling the values.yaml default class DELETES the key; templates do
        # not duplicate the default string, so rendering fails with a required
        # message instead of an empty gatewayClassName reference.
        result = self.render(self.managed_maas() | {"gateways": {
            "openshiftAiInference": {"gatewayClassName": None}}},
            expect_failure=True)
        self.assertIn("gatewayClassName is required", result.stderr)

    def _gateway_values(self, **maas_overrides):
        values = self.managed_maas()
        values["gateways"] = {
            "maasDefaultGateway": {"useRoute": True},
            "openshiftAiInference": {"create": True, "useRoute": True},
        }
        for gateway, overrides in maas_overrides.items():
            values["gateways"][gateway].update(overrides)
        return values

    def test_host_agreement_explicit_override(self):
        # A nonempty per-gateway hostname override wins and must agree across
        # Gateway listeners, Route hosts (useRoute) and Certificate DNS names
        # (no useRoute) for both gateways.
        values = self._gateway_values(
            maasDefaultGateway={"hostname": "maas.custom.example.org",
                                "useRoute": False},
            openshiftAiInference={"hostname": "infer.custom.example.org",
                                  "useRoute": False},
        )
        by_kind = self.by_kind(self.render(values))
        for gateway, hostname in (
                ("maas-default-gateway", "maas.custom.example.org"),
                ("openshift-ai-inference", "infer.custom.example.org")):
            listeners = self.pick(by_kind, "Gateway", gateway)["spec"]["listeners"]
            self.assertTrue(all(l["hostname"] == listeners[0]["hostname"]
                                for l in listeners))
            self.assertEqual(listeners[0]["hostname"], hostname)
            self.assertEqual(
                self.pick(by_kind, "Certificate",
                          f"{gateway}-certificate")["spec"]["dnsNames"],
                [listeners[0]["hostname"]])
            self.assertNotIn("Route", by_kind)

        values = self._gateway_values(
            maasDefaultGateway={"hostname": "maas.custom.example.org"},
            openshiftAiInference={"hostname": "infer.custom.example.org"},
        )
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(self.pick(by_kind, "Route", "maas-default-gateway")
                         ["spec"]["host"], "maas.custom.example.org")
        self.assertEqual(self.pick(by_kind, "Route", "openshift-ai-inference")
                         ["spec"]["host"], "infer.custom.example.org")

    def test_host_agreement_global_fallback(self):
        # Unset and falsey hostname overrides fall back to the global cluster
        # convention across Gateway listeners, Route hosts and Certificate DNS
        # names; disabling certificates removes them.
        for hostname in (None, "", False, 0):
            values = self._gateway_values(
                maasDefaultGateway={"hostname": hostname},
                openshiftAiInference={"hostname": hostname},
            )
            by_kind = self.by_kind(self.render(values))
            self.assertEqual(
                self.pick(by_kind, "Gateway", "maas-default-gateway")
                ["spec"]["listeners"][0]["hostname"],
                "maas.apps.cluster.example.com")
            self.assertEqual(
                self.pick(by_kind, "Route", "maas-default-gateway")["spec"]["host"],
                "maas.apps.cluster.example.com")
            self.assertEqual(
                self.pick(by_kind, "Gateway", "openshift-ai-inference")
                ["spec"]["listeners"][0]["hostname"],
                "inference.apps.cluster.example.com")
            self.assertEqual(
                self.pick(by_kind, "Route", "openshift-ai-inference")["spec"]["host"],
                "inference.apps.cluster.example.com")

        values = self._gateway_values(
            maasDefaultGateway={"useRoute": False},
            openshiftAiInference={"useRoute": False},
        ) | {"global": {"cluster": {"name": "mycluster",
                                    "baseDomain": "prod.example.io"}}}
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(
            self.pick(by_kind, "Certificate", "maas-default-gateway-certificate")
            ["spec"]["dnsNames"], ["maas.apps.mycluster.prod.example.io"])
        self.assertEqual(
            self.pick(by_kind, "Certificate", "openshift-ai-inference-certificate")
            ["spec"]["dnsNames"], ["inference.apps.mycluster.prod.example.io"])
        self.assertEqual(
            self.pick(by_kind, "Gateway", "maas-default-gateway")
            ["spec"]["listeners"][0]["hostname"],
            "maas.apps.mycluster.prod.example.io")

        values["gateways"]["maasDefaultGateway"]["certificate"] = {"create": False}
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(
            [c["metadata"]["name"] for c in by_kind.get("Certificate", [])],
            ["openshift-ai-inference-certificate"])

    def test_wrong_shape_selector_fields_fail(self):
        for sel in ({"matchLabels": ["list"]},
                    {"matchExpressions": {"map": True}}):
            self.render(self.managed_maas() | {"gateways": {
                "maasDefaultGateway": {"useRoute": True,
                                       "routeNamespaceSelector": sel}}},
                expect_failure=True)
