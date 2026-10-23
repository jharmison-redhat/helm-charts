"""Shared safe-YAML loading and strict chart metadata validation.

This module must not import Helm or shell helpers; both CLI entrypoints use it
for chart metadata access.
"""

import yaml

CHART_FILE = "Chart.yaml"


class ChartMetadataError(Exception):
    """Raised when a Chart.yaml is missing, unparseable, or invalid."""


def load_chart(chart_dir):
    """Load and validate the Chart.yaml in ``chart_dir``.

    Returns the parsed mapping. Raises ChartMetadataError when the file is
    missing, is not a mapping, lacks required scalar ``name``/``version``
    fields, or declares malformed dependencies. Each dependency must be a
    mapping with a string ``name``; ``repository``, ``alias``, and
    ``condition``, when present, must be strings.
    """
    path = f"{chart_dir}/{CHART_FILE}"
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ChartMetadataError(f"not a chart directory: {chart_dir} ({path} missing)")
    except yaml.YAMLError as exc:
        raise ChartMetadataError(f"{path}: invalid YAML: {exc}")

    if not isinstance(data, dict):
        raise ChartMetadataError(f"{path}: top-level document must be a mapping")

    for field in ("name", "version"):
        value = data.get(field)
        if not isinstance(value, str) or not value:
            raise ChartMetadataError(f"{path}: required field '{field}' must be a non-empty string")

    deps = data.get("dependencies", [])
    if deps is None:
        deps = []
    if not isinstance(deps, list):
        raise ChartMetadataError(f"{path}: 'dependencies' must be a list")

    for index, dep in enumerate(deps):
        if not isinstance(dep, dict):
            raise ChartMetadataError(f"{path}: dependency #{index} must be a mapping")
        name = dep.get("name")
        if not isinstance(name, str) or not name:
            raise ChartMetadataError(f"{path}: dependency #{index} requires a non-empty 'name'")
        for field in ("repository", "alias", "condition"):
            if field in dep and not isinstance(dep[field], str):
                raise ChartMetadataError(f"{path}: dependency '{name}' field '{field}' must be a string")

    return data


def dependencies(chart_dir):
    """Return the validated dependencies list of the chart (empty when absent)."""
    return load_chart(chart_dir).get("dependencies") or []
