# common

A Helm library chart carrying the collection's shared hook-surface templates as named templates: `common.hook-preamble`
(the conditional-write preamble engine) and `common.wait-for` (the bounded wait helper) emit shell script payloads, and
`common.hook-security-context` emits the guard-without-pin SecurityContext YAML fragment rendered into every consuming
chart's hook Job containers.

## Charter

Shared hook helpers for the charts in this collection land here. New shared template payloads or helpers adopt the
library by dependency pin bump rather than growing per-chart copies. Named templates in this library use kebab-case.

## The payloads are delivered text, not callable logic

Two templates emit shell script text verbatim — payloads rendered into consumers' script ConfigMaps, which source them
sibling-relative from a ConfigMap mount; nothing executes the templates as logic. The third emits a YAML fragment that
consumers embed in their hook Job pod specs. That describes the current payloads, not what the library admits — shared
template helpers of other kinds land here per the charter. Consumer charts declare a dependency on `common` and deliver
a template with `include`:

```yaml
{{- include "common.hook-preamble" . | nindent 4 }}
```

## The future-edits rule

Any edit to these payloads is a rendered-output change across ALL consumer charts at once, and re-owes live validation
of every consumer's hook surfaces.

## Library charts are not installable

`type: library` charts cannot be installed with `helm install`; they exist only to be declared as a dependency and
included by other charts.
