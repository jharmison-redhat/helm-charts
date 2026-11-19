"""Render-contract tests for the openshift-ai chart's configurable
GatewayClass references with namespace-selective Route attachment,
per-gateway replicas, and hostname/certificate agreement."""

from oa_support import ChartFixture


class GatewayRenderTests(ChartFixture):
    def test_default_render_inventory(self):
        # By default only the MaaS default gateway renders (with no Route —
        # useRoute is off); no GatewayClass, Telemetry, or TelemetryPolicy
        # ever renders from this chart.
        by_kind = self.by_kind(self.render(self.managed_maas()))
        for kind in ("GatewayClass", "Route", "Telemetry",
                     "TelemetryPolicy"):
            self.assertNotIn(kind, by_kind)
        self.assertEqual([g["metadata"]["name"] for g in by_kind["Gateway"]],
                         ["openshift-ai-inference", "maas-default-gateway"])

        values = self.managed_maas()
        values["gateways"] = {
            "maasDefaultGateway": {"useRoute": True},
            "openshiftAiInference": {"create": True, "useRoute": True},
        }
        by_kind = self.by_kind(self.render(values))
        gateways = {d["metadata"]["name"]: d for d in by_kind["Gateway"]}
        self.assertEqual(
            sorted(gateways), ["maas-default-gateway", "openshift-ai-inference"])
        self.assertTrue(
            all(g["spec"]["gatewayClassName"] == "data-science-gateway-class"
                for g in gateways.values()))
        routes = {d["metadata"]["name"] for d in by_kind["Route"]}
        self.assertEqual(
            sorted(routes), ["maas-default-gateway", "openshift-ai-inference"])
        for name, label in (("maas-default-gateway", "maas-gateway-access"),
                            ("openshift-ai-inference",
                             "openshift-ai-inference-access")):
            routes_ns = gateways[name]["spec"]["listeners"][0][
                "allowedRoutes"]["namespaces"]
            self.assertEqual(routes_ns["from"], "Selector")
            self.assertEqual(routes_ns["selector"]["matchLabels"],
                             {label: "true"})

    def test_nonmap_selector_fails(self):
        self.render(self.managed_maas() | {"gateways": {
            "maasDefaultGateway": {"useRoute": True,
                                   "routeNamespaceSelector": "scalar"}}},
            expect_failure=True)
