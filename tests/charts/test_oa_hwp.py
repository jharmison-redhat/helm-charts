"""Render tests for openshift-ai HardwareProfile Node/Queue scheduling."""

import os
import re

import yaml

from oa_support import CHART, ChartFixture


class HwpProfileTests(ChartFixture):
    """Render tests for HardwareProfile scheduling: top-level type/kueue
    values contract. Shape-only — no claim of live CRD acceptance."""

    def hwp_values(self, profiles):
        return {"hardwareProfiles": profiles}

    def values_example(self):
        """The commented hardwareProfiles example in values.yaml, uncommented."""
        with open(os.path.join(CHART, "values.yaml")) as f:
            lines = f.read().splitlines()
        out, capture = [], False
        for line in lines:
            if line.startswith("# hardwareProfiles:"):
                capture = True
            elif capture and not line.startswith("#"):
                break
            if capture:
                out.append(re.sub(r"^# ?", "", line))
        return yaml.safe_load("\n".join(out))

    def test_default_and_explicit_node(self):
        profiles = {
            "plain": {"displayName": "Plain"},
            "noded": {"type": "Node", "node": {"tolerations": [
                {"key": "nvidia.com/gpu", "effect": "NoSchedule",
                 "operator": "Exists"}]}},
        }
        by_kind = self.by_kind(self.render(self.hwp_values(profiles)))
        plain = self.pick(by_kind, "HardwareProfile", "plain")["spec"]["scheduling"]
        self.assertEqual(plain["type"], "Node")
        self.assertEqual(plain["node"], {})
        self.assertNotIn("kueue", plain)
        noded = self.pick(by_kind, "HardwareProfile", "noded")["spec"]["scheduling"]
        self.assertEqual(noded["type"], "Node")
        self.assertEqual(noded["node"], {"tolerations": [
            {"key": "nvidia.com/gpu", "effect": "NoSchedule",
             "operator": "Exists"}]})

    def test_queue_kueue_map(self):
        profiles = {"a100-queue": {
            "type": "Queue",
            "kueue": {"localQueueName": "gpu-queue",
                      "extra": {"supported": "keys preserved"}},
            "displayName": "NVIDIA A100 (Kueue queue)",
        }}
        by_kind = self.by_kind(self.render(self.hwp_values(profiles)))
        scheduling = self.pick(by_kind, "HardwareProfile", "a100-queue")[
            "spec"]["scheduling"]
        self.assertEqual(scheduling["type"], "Queue")
        self.assertEqual(scheduling["kueue"], {
            "localQueueName": "gpu-queue",
            "extra": {"supported": "keys preserved"}})
        self.assertNotIn("node", scheduling)

    def test_queue_missing_local_queue_name_fails(self):
        for kueue in (None, {}, {"extra": {"supported": "keys preserved"}}):
            profiles = {"a100-queue": {"type": "Queue"}}
            if kueue is not None:
                profiles["a100-queue"]["kueue"] = kueue
            result = self.render(self.hwp_values(profiles), expect_failure=True)
            self.assertIn("hardwareProfiles.a100-queue", result.stderr)

    def test_values_example_renders_queue(self):
        by_kind = self.by_kind(self.render(self.hwp_values(
            self.values_example()["hardwareProfiles"])))
        scheduling = self.pick(by_kind, "HardwareProfile", "a100-queue")[
            "spec"]["scheduling"]
        self.assertEqual(scheduling["type"], "Queue")
        self.assertEqual(scheduling["kueue"]["localQueueName"], "gpu-queue")
        l40s = self.pick(by_kind, "HardwareProfile", "l40s")["spec"]["scheduling"]
        self.assertEqual(l40s["type"], "Node")
        self.assertNotIn("kueue", l40s)
