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

    def test_default_services_render_without_disabled_list(self):
        cluster = self.pick(self.by_kind(self.render()), "Cluster",
                            "test-release-postgres-cluster")
        self.assertNotIn(
            "disabledDefaultServices",
            cluster["spec"].get("managed", {}))

    def test_removing_r_renders_it_disabled(self):
        rendered = self.render({"services": ["rw", "ro"]})
        self.assertEqual(self.disabled_services(rendered), ["r"])

    def test_removing_r_and_ro_renders_both_disabled(self):
        rendered = self.render({"services": ["rw"]})
        self.assertEqual(self.disabled_services(rendered), ["ro", "r"])

    def test_services_without_rw_fails_the_render(self):
        stderr = self.render_failure({"services": ["ro", "r"]})
        self.assertIn("the rw service cannot be disabled", stderr)


if __name__ == "__main__":
    unittest.main()
