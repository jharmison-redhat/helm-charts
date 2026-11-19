# common

A Helm library chart carrying the collection's shared hook-script payloads as named templates: `common.hook-preamble`
(the conditional-write preamble engine) and `common.wait-for` (the bounded wait helper).

## Charter

Shared hook helpers for the charts in this collection land here. New shared template payloads or helpers adopt the
library by dependency pin bump rather than growing per-chart copies.

## The payloads are script text, not callable logic

Both templates emit shell script text verbatim: payloads rendered into consumers' script ConfigMaps, which source them
sibling-relative from a ConfigMap mount; nothing executes the templates as logic. That describes these two payloads,
not what the library admits — shared template helpers of other kinds land here per the charter. Consumer charts declare
a dependency on `common` and deliver a payload with `include`:

```yaml
{{- include "common.hook-preamble" . | nindent 4 }}
```

## The future-edits rule

Any edit to these payloads is a rendered-output change across ALL consumer charts at once, and re-owes live validation
of every consumer's hook surfaces.

## Library charts are not installable

`type: library` charts cannot be installed with `helm install`; they exist only to be declared as a dependency and
included by other charts.
