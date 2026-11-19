"""Render contracts for the kuadrant chart plus the restart hook script's
smoke set.

Render tests run the chart through ``helm template`` (kuadrant has no chart
dependencies, so no stubs or registry access are needed). The restart hook
Job renders when ``kuadrant.create`` is true (the default).

Per the repo's unit-test policy the script-execution layer is a minimal
smoke set: one happy-path run of the rendered restart script plus the
chart's fail-closed no-tool check, against a fake ``oc`` on a restricted
PATH — state-machine behavior is proven by live deploys, not fixture
simulation.
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
    """The default render's hook-Job inventory, with every hook Job
    container carrying the guard-without-pin securityContext: the four
    restricted-pod guards and no runAsUser/runAsGroup anywhere in the pod —
    restricted SCCs assign the namespace range UID to UID-less pods, and a
    pin would fight that platform assignment."""

    def test_every_hook_job_is_guarded_without_pin(self):
        jobs = support.hook_jobs(self.render())
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in jobs),
            ["restart-kuadrant-operator"])
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])


# Fake oc for the restart-script smoke runs: serves the canned
# installedCSV for a Subscription get and the canned phase for a CSV get,
# and records every invocation so tests can assert the exact oc call
# surface (including the rollout --timeout).
FAKE_OC_RESTART = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
case "$1" in
  get)
    if [[ "$*" == *subscription* ]]; then
      printf '%s' "$OC_INSTALLED_CSV"
    else
      printf '%s' "$OC_CSV_PHASE"
    fi
    ;;
  delete) exit 0 ;;
  rollout) exit 0 ;;
  *)
    echo "unexpected oc invocation: $*" >&2
    exit 64
    ;;
esac
"""


class RestartScriptSmokeTests(ChartFixture, support.FakeOcScriptFixture):
    """Executes the rendered restart script against a fake oc: a restricted
    PATH built solely from symlinks in an isolated bin directory (sleep and
    the fake oc), the rendered ConfigMap staged as the entry script
    (mirroring the single ConfigMap mount), and an explicit minimal child
    environment. Timeouts are shrunk via the script's environment overrides
    so the bounded-wait failure path is cheap to exercise."""

    work_prefix = "kuadrant-smoke-"
    bin_tools = ["sleep"]
    fake_oc_script = FAKE_OC_RESTART
    scripts = {}

    def stage_rendered_script(self, values=None):
        by_kind = self.by_kind(self.render(values))
        self.assertEqual(len(by_kind["ConfigMap"]), 1)
        self.scripts = by_kind["ConfigMap"][0]["data"]
        self.stage_scripts()

    def run_script(self, installed_csv="rhcl-operator.v1.2.3",
                   csv_phase="Succeeded", env_extra=None):
        env = {
            "PATH": self.build_bin_dir(include_python=False),
            "HOME": self.work,
            "TMPDIR": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_INSTALLED_CSV": installed_csv,
            "OC_CSV_PHASE": csv_phase,
            "CSV_READY_TIMEOUT_SECONDS": "2",
            "CSV_READY_POLL_INTERVAL_SECONDS": "1",
        }
        if env_extra:
            env.update(env_extra)
        return self.run_child(
            os.path.join(self.work, "restart-kuadrant-operator.sh"), env)

    def test_happy_path_smoke(self):
        self.stage_rendered_script()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = self.log()
        # Every operator's readiness is probed on the actual installedCSV of
        # its Subscription, then confirmed on the CSV's own phase.
        for operator in ("authorino", "dns", "limitador", "rhcl"):
            self.assertIn(
                "-ojsonpath={.items[0].status.installedCSV}", log)
            self.assertIn(
                f"operators.coreos.com/{operator}-operator.kuadrant-system",
                log)
        self.assertIn(
            "delete pod -n kuadrant-system "
            "-l app=kuadrant,control-plane=controller-manager", log)
        # The rollout status carries a bounded --timeout.
        self.assertIn(
            "rollout status -n kuadrant-system "
            "deployment/kuadrant-operator-controller-manager "
            "--timeout=300s", log)

    def test_no_oc_fails_closed(self):
        # The chart's fail-closed no-tool check.
        self.stage_rendered_script()
        bin_dir = self.build_bin_dir(include_python=False, include_oc=False)
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "TMPDIR": self.work,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "CSV_READY_TIMEOUT_SECONDS": "1",
            "CSV_READY_POLL_INTERVAL_SECONDS": "1",
        }
        result = self.run_child(
            os.path.join(self.work, "restart-kuadrant-operator.sh"), env)
        self.assertNotEqual(result.returncode, 0)
        # No oc on PATH means oc was never invoked, so no call log exists
        # and the pod delete / rollout never ran.
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")))


if __name__ == "__main__":
    unittest.main()
