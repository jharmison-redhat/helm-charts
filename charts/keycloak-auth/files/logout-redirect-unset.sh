#!/bin/bash
# Restores spec.authentication.logoutRedirect to unset on the platform-owned
# Console cluster object (config.openshift.io/v1, name "cluster") as a Helm
# pre-delete hook, so the console is never left redirecting logouts at a
# Keycloak that no longer exists. The write is a merge patch carrying the
# live metadata.resourceVersion: a stale version is rejected with HTTP 409
# and retried. Only the value this release set is removed — a foreign value
# is left untouched. The Console object itself is never owned or deleted by
# this release; if it is already absent, there is nothing to remove and the
# hook succeeds.
set -euo pipefail

source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"
source "${BASH_SOURCE[0]%/*}/logout-redirect.sh"

require_oc "cannot remove the console logout redirect"
select_json_tool "cannot manipulate the Console object"

REDIRECT_URL=$(read_redirect_url)

# Produces the reset patch for conditional_write per its compute-function
# contract: nothing to change when the field does not hold exactly the URL
# this release set (unset or foreign), and a fatal condition (reported here)
# when the field cannot be read.
prepare_patch() {
  local current
  if ! current=$(read_current "$1"); then
    echo "ERROR: failed to read spec.authentication.logoutRedirect from the live Console object" >&2
    return 4
  fi
  if [ "$current" != "$REDIRECT_URL" ]; then
    return 0
  fi
  compute_patch "$1" ""
}

write_rc=0
conditional_write "$CONSOLE" prepare_patch "logoutRedirect" "Console object" || write_rc=$?
case $write_rc in
  0)
    echo "Console logoutRedirect removed"
    ;;
  3)
    echo "Console logoutRedirect is unset or foreign; no changes needed"
    ;;
  2)
    echo "Console object ($CONSOLE) is absent; no logoutRedirect to remove"
    ;;
  *)
    exit 1
    ;;
esac
