"""Focused tests for the kuadrant chart's hook guard surface: every hook
Job container carries the guard-without-pin securityContext and no
unguarded hook Job renders for any values combination.

Render tests run the chart through ``helm template`` (kuadrant has no chart
dependencies, so no stubs or registry access are needed). The restart hook
Job renders when ``kuadrant.create`` is true (the default).
"""

import os
import unittest

import support

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts",
    "kuadrant"))


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "kuadrant-test-"
    values_prefix = "kuadrant-values-"


class HookGuardTests(ChartFixture):
    """Every hook Job container carries the guard-without-pin
    securityContext: the four restricted-pod guards and no
    runAsUser/runAsGroup anywhere in the pod — restricted SCCs assign the
    namespace range UID to UID-less pods, and a pin would fight that
    platform assignment."""

    def test_every_hook_job_is_guarded_without_pin(self):
        jobs = support.hook_jobs(self.render())
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in jobs),
            ["restart-kuadrant-operator"])
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])

    def test_no_hook_jobs_render_when_kuadrant_create_is_false(self):
        self.assertEqual(
            support.hook_jobs(self.render({"kuadrant": {"create": False}})),
            [])


if __name__ == "__main__":
    unittest.main()
