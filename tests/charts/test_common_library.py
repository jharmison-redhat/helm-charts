"""Invariants guarding the shared ``common`` library chart and its
consumers. The library chart delivers the hook-script payloads through
named templates, so a consumer render resolves its ``common`` dependency:
the pin-sync test mechanically enforces the one-push rule (a consumer
that renders a ``common.*`` include must declare the dependency, and any
declared pin's range must admit the current ``common`` version, so a
``common`` release cannot leave consumers pinned out of range), the
strays test prevents silent reintroduction of per-chart payload copies,
and the payload guard keeps Go-template actions out of the delivered
script bytes.
"""

import glob
import os
import re
import unittest

import yaml

from support import REPO_ROOT

# The delivered payload copies that must never reappear chart-locally;
# the shared payloads live only in charts/common/templates.
STRAY_LIB_FILES = ("hook-preamble.sh", "wait-for.sh")

INCLUDE_PATTERN = re.compile(r'include\s+"common\.[^"]+"')


def common_version():
    with open(os.path.join(REPO_ROOT, "charts", "common", "Chart.yaml")) as f:
        return yaml.safe_load(f)["version"]


def declared_common_pins():
    """Charts declaring the ``common`` dependency, mapped to their pin."""
    pins = {}
    for chart_yaml in sorted(glob.glob(
            os.path.join(REPO_ROOT, "charts", "*", "Chart.yaml"))):
        chart = os.path.basename(os.path.dirname(chart_yaml))
        with open(chart_yaml) as f:
            deps = yaml.safe_load(f).get("dependencies") or []
        for dep in deps:
            if dep.get("name") == "common":
                pins[chart] = dep["version"]
    return pins


def constraint_admits(constraint, version):
    """True when the whitespace-separated constraint list admits the
    dotted version (the pin shapes this repo uses: e.g. ``>=0.1.0 <0.2.0``)."""
    def compare(bound):
        a = [int(part) for part in bound.split(".")]
        b = [int(part) for part in version.split(".")]
        return (a > b) - (a < b)

    admitted = {
        ">=": lambda c: c <= 0, "<=": lambda c: c >= 0,
        "==": lambda c: c == 0, ">": lambda c: c < 0, "<": lambda c: c > 0,
    }
    for part in constraint.split():
        op = next((o for o in admitted if part.startswith(o)), None)
        if op is None:
            raise AssertionError(f"unsupported constraint {part!r}")
        if not admitted[op](compare(part[len(op):])):
            return False
    return True


def common_include_renderers():
    """Charts whose templates render any ``common.*`` include."""
    renderers = set()
    for template in glob.glob(os.path.join(
            REPO_ROOT, "charts", "*", "templates", "*")):
        with open(template) as f:
            if INCLUDE_PATTERN.search(f.read()):
                renderers.add(
                    os.path.basename(os.path.dirname(os.path.dirname(template))))
    return renderers


class CommonLibraryInvariantsTests(unittest.TestCase):
    def test_every_renderer_declares_an_admitting_common_pin(self):
        renderers = common_include_renderers()
        pins = declared_common_pins()
        version = common_version()
        for chart in sorted(renderers):
            with self.subTest(chart=chart):
                self.assertIn(
                    chart, pins,
                    f"charts/{chart} renders a common.* include but "
                    f"declares no common dependency")
                self.assertTrue(
                    constraint_admits(pins[chart], version),
                    f"charts/{chart} pins common {pins[chart]!r}, which "
                    f"does not admit the current common version {version}")
        self.assertTrue(renderers, "no consumer renders a common.* include")

    def test_no_stray_payload_copies(self):
        strays = [match for name in STRAY_LIB_FILES for match in glob.glob(
            os.path.join(REPO_ROOT, "charts", "*", "files", "lib", name))]
        self.assertEqual(
            strays, [],
            "per-chart payload copies reintroduced; the payloads live "
            "only in charts/common/templates")

    def test_payloads_contain_no_template_actions(self):
        # Scanned strictly between the define and end wrappers: the
        # non-emitting header comments above the defines legitimately
        # contain literal {{ text.
        for template in sorted(glob.glob(os.path.join(
                REPO_ROOT, "charts", "common", "templates", "_*.tpl"))):
            with open(template) as f:
                lines = f.read().splitlines()
            define = next(i for i, line in enumerate(lines)
                          if "{{- define" in line)
            end = next(i for i, line in enumerate(lines)
                       if i > define and line.strip() == "{{ end }}")
            payload = lines[define + 1:end]
            for number, line in enumerate(payload, start=define + 2):
                with self.subTest(template=os.path.basename(template),
                                  line=number):
                    self.assertNotIn(
                        "{{", line,
                        "template action inside the payload would render "
                        "as script text or break the delivered bytes")


if __name__ == "__main__":
    unittest.main()
