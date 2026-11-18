"""Byte-parity guard for the shared hook preamble across the charts that
deliver it. The conditional-write engine lives as one copy per chart
(``files/lib/hook-preamble.sh``, delivered chart-locally through each
chart's script ConfigMaps), so a copy edited in isolation drifts silently
from its siblings — this module fails the suite on any byte difference
from the majority copy.

The parity contract is over behavior, not provenance: the majority
preamble's ``conditional_write`` takes the namespace as an optional
argument and omits ``-n`` when it is empty, so cluster-scoped callers
pass no namespace and no chart needs a drift-persistent variant.
"""

import collections
import glob
import os
import unittest

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

PREAMBLE_GLOB = os.path.join(REPO_ROOT, "charts", "*", "files", "lib",
                             "hook-preamble.sh")


class PreambleParityTests(unittest.TestCase):
    """Every chart-local copy of the hook preamble is byte-identical."""

    def copies(self):
        return {os.path.relpath(path, REPO_ROOT).split(os.sep)[1]:
                self.read(path)
                for path in sorted(glob.glob(PREAMBLE_GLOB))}

    @staticmethod
    def read(path):
        with open(path, "rb") as f:
            return f.read()

    def test_established_copies_exist_exactly(self):
        # A chart dropping its preamble copy (or the glob path moving)
        # shrinks the covered set silently; the established six must all
        # be present for the parity assertion below to mean anything.
        self.assertEqual(sorted(self.copies()),
                         ["aws-nvidia-gpu-machinesets", "cert-manager",
                          "install-operators", "keycloak-auth", "odf",
                          "openshift-ai"])

    def test_every_copy_is_byte_identical_to_the_majority(self):
        copies = self.copies()
        majority, count = collections.Counter(copies.values()).most_common(1)[0]
        for chart, content in copies.items():
            with self.subTest(chart=chart):
                self.assertEqual(
                    content, majority,
                    f"charts/{chart}/files/lib/hook-preamble.sh differs from "
                    f"the majority preamble ({count} of {len(copies)} "
                    "copies carry it)")


if __name__ == "__main__":
    unittest.main()
