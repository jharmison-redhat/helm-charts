"""Render tests for the postgres-cluster chart's `services` knob: the
default keep-list renders, removing `ro`/`r` renders the complement as
`managed.services.disabledDefaultServices`, and removing `rw` fails the
render — CloudNativePG's admission webhook rejects disabling the read-write
service, so the chart rejects the composition up front.
"""

import os
import unittest

import support

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts",
    "postgres-cluster"))


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "postgres-cluster-test-"
    values_prefix = "postgres-cluster-values-"

    def disabled_services(self, rendered):
        cluster = self.pick(self.by_kind(rendered), "Cluster",
                            "test-release-postgres-cluster")
        return cluster["spec"]["managed"]["services"][
            "disabledDefaultServices"]


class ServicesTests(ChartFixture):

    def test_default_services_render(self):
        # The default keep-list renders no disabled list; a representative
        # non-default keep-list still lands the complement on the Cluster.
        cluster = self.pick(self.by_kind(self.render()), "Cluster",
                            "test-release-postgres-cluster")
        self.assertNotIn(
            "disabledDefaultServices",
            cluster["spec"].get("managed", {}))
        self.assertEqual(
            self.disabled_services(self.render({"services": ["rw"]})),
            ["ro", "r"])

    def test_services_without_rw_fails_the_render(self):
        self.render_failure({"services": ["ro", "r"]})


if __name__ == "__main__":
    unittest.main()
