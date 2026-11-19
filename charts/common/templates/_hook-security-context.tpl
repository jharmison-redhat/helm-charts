{{/* SecurityContext carried by every hook Job container: the restricted
hardening guards with no runAsUser/runAsGroup pin. UID-less pods match the
restricted SCCs, which assign the namespace's range UID at admission; a pin
would fight that platform assignment and break portability. Any edit to this
fragment is a rendered-output change across ALL consumers at once and
re-owes live validation of each one's hook surfaces. */}}
{{- define "common.hook-security-context" -}}
runAsNonRoot: true
allowPrivilegeEscalation: false
capabilities:
  drop:
    - ALL
seccompProfile:
  type: RuntimeDefault
{{- end }}
