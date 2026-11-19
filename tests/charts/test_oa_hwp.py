"""Render tests for openshift-ai HardwareProfile Node/Queue scheduling."""

import os
import re

import yaml

from oa_support import CHART, ChartFixture


class HwpProfileTests(ChartFixture):
    """One representative render of HardwareProfile scheduling: the
    shipped values.yaml example renders both scheduling types with their
    payloads. Shape-only — no claim of live CRD acceptance."""

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

    def test_values_example_renders(self):
        by_kind = self.by_kind(self.render(self.hwp_values(
            self.values_example()["hardwareProfiles"])))
        queue = self.pick(by_kind, "HardwareProfile", "a100-queue")[
            "spec"]["scheduling"]
        self.assertEqual(queue["type"], "Queue")
        self.assertEqual(queue["kueue"]["localQueueName"], "gpu-queue")
        node = self.pick(by_kind, "HardwareProfile", "l40s")[
            "spec"]["scheduling"]
        self.assertEqual(node["type"], "Node")
        self.assertNotIn("kueue", node)
