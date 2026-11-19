"""Byte-parity guard for the shared files/lib helpers across the charts
that deliver them. The conditional-write engine and the bounded wait-for
helper each live as one copy per chart (``files/lib/*.sh``, delivered
chart-locally through each chart's script ConfigMaps), so a copy edited in
isolation drifts silently from its siblings — this module fails the suite
on any byte difference from the majority copy.

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

# Each shared files/lib helper with the charts that carry a copy; the
# parity assertion below requires exactly the established set per helper.
SHARED_LIB_FILES = {
    "hook-preamble.sh": ["aws-nvidia-gpu-machinesets", "cert-manager",
                         "install-operators", "keycloak-auth", "odf",
                         "openshift-ai"],
    "wait-for.sh": ["odf", "openshift-ai"],
}


class SharedLibParityTests(unittest.TestCase):
    """Every chart-local copy of each shared files/lib helper is
    byte-identical."""

    def copies(self, helper):
        return {os.path.relpath(path, REPO_ROOT).split(os.sep)[1]:
                self.read(path)
                for path in sorted(glob.glob(
                    os.path.join(REPO_ROOT, "charts", "*", "files", "lib",
                                 helper)))}

    @staticmethod
    def read(path):
        with open(path, "rb") as f:
            return f.read()

    def test_established_copies_exist_exactly(self):
        # A chart dropping its copy (or the helper's glob path moving)
        # shrinks the covered set silently; the established charts must
        # all be present for the parity assertion below to mean anything.
        for helper, established in SHARED_LIB_FILES.items():
            with self.subTest(helper=helper):
                self.assertEqual(sorted(self.copies(helper)), established)

    def test_every_copy_is_byte_identical_to_the_majority(self):
        for helper, established in SHARED_LIB_FILES.items():
            copies = self.copies(helper)
            majority, count = collections.Counter(
                copies.values()).most_common(1)[0]
            for chart, content in copies.items():
                with self.subTest(helper=helper, chart=chart):
                    self.assertEqual(
                        content, majority,
                        f"charts/{chart}/files/lib/{helper} differs from "
                        f"the majority copy ({count} of {len(copies)} "
                        f"copies carry it; expected charts: "
                        f"{', '.join(established)})")


if __name__ == "__main__":
    unittest.main()
