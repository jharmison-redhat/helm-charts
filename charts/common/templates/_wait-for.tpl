{{/* Rendered verbatim into consumer charts' script ConfigMaps as a
payload, not callable logic. Consumers source it sibling-relative.
Any edit to this payload is a rendered-output change across ALL
consumers at once and re-owes live validation of each one's hook
surfaces. The payload must never contain {{ (Go-template action) */}}
{{- define "common.wait-for" -}}
#!/bin/bash
# Shared bounded-wait helper for this repo's hook scripts, sourced as a
# sibling key of the consuming script's ConfigMap after hook-preamble.sh.
#
# wait_for <deadline_epoch> <interval_seconds> <retry_format> <check…>
#
# Polls <check> until it succeeds. The check contract:
#   - exit 0         the awaited condition holds; the wait ends
#   - exit 3         retryable absence; the check records what is missing in
#                    $WAIT_DETAIL (and emits nothing itself)
#   - any other exit the check reports its own fatal error and exits the
#                    script outright
# Between polls (while the deadline has not passed) the helper emits the
# caller's <retry_format> as a printf format over $WAIT_DETAIL — an empty
# format stays silent — and sleeps <interval_seconds>. Returns 0 on success
# and 1 when <deadline_epoch> passes without a successful check; the caller
# owns the timeout diagnostic (it reads $WAIT_DETAIL for the last observed
# absence). The deadline is caller-supplied so sequential waits in one
# script can share a single budget.
WAIT_DETAIL=""

wait_for() {
  local deadline="$1" interval="$2" retry_format="$3" rc=0
  shift 3
  while :; do
    "$@" && rc=0 || rc=$?
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    if [ "$rc" -ne 3 ]; then
      return "$rc"
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
      return 1
    fi
    if [ -n "$retry_format" ]; then
      printf "$retry_format" "$WAIT_DETAIL" >&2
    fi
    sleep "$interval"
  done
}
{{ end }}
