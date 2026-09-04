#!/usr/bin/env bash
#
# check-door-live.sh -- assert the doors are actually closed on the LIVE host.
#
# `make check` can only read the confs. Whether a route really answers 302 to
# the login page is a runtime fact, and it is exactly the fact that goes wrong
# silently: an unresolvable upstream, a detached bind mount (see
# check-swag-conf-drift.sh), a tinyauth that starts but rejects everything, or
# an nginx that reloaded a conf it could not see. Every one of those leaves the
# repo, the linter and the invariant checker agreeing that the door is hung.
#
# Three assertions, and the second and third matter as much as the first:
#   * every `protect` route answers 3xx to an anonymous request, and the
#     redirect points at the login page -- a 200 means the door is OPEN;
#   * every `never` route does NOT redirect to the login page -- ntfy above
#     all, because the alert channel behind a broken door is a SILENT one;
#   * the apex is public and /ops.html is not.
#
# The classification here mirrors DOOR in check-invariants.sh. It is duplicated
# rather than shared because that file is a Python heredoc inside a shell
# script and this one has to run when the compose model cannot be rendered.
# `make check`'s door-classification-complete is what keeps the two honest
# about the *set*; this file is about the live answers.
#
# Exit codes follow the repo convention (AGENTS.md):
#   0  every door is where it should be
#   1  one or more are not
#   2  fatal -- could not reach SWAG at all, so nothing was proven
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

# Read the one value needed, rather than sourcing .env. Sourcing it aborts a
# `set -u` shell the moment a value contains an unquoted `$` -- the bcrypt hash
# did exactly that and took six of `make verify-runtime`'s assertions with it.
# A .env is a data file; treat it as one.
DOMAIN="$(sed -n 's/^PUBLIC_DOMAIN=//p' .env 2>/dev/null | tail -1 | tr -d "'\"")"
if [ -z "$DOMAIN" ]; then
  echo "    !!! PUBLIC_DOMAIN is not set in .env; cannot probe any route" >&2
  exit 2
fi
LOGIN_HOST="auth.${DOMAIN}"

PROTECT="sonarr radarr lidarr bazarr prowlarr lingarr qui slskd cleanuparr
         lidarr-bulk playlist-generator ongehoord jellyseerr"
NEVER="jellyfin nextcloud ntfy auth"

probe() { curl -s -o /dev/null -m 10 -w '%{http_code} %{redirect_url}' "$1" 2>/dev/null; }

if [ -z "$(probe "https://${DOMAIN}/")" ]; then
  echo "    !!! SWAG did not answer at https://${DOMAIN}/ -- nothing proven" >&2
  exit 2
fi

rc=0

for h in $PROTECT; do
  read -r code target <<<"$(probe "https://${h}.${DOMAIN}/")"
  case "$code" in
    30[12378])
      case "$target" in
        https://${LOGIN_HOST}/*) ;;
        *) echo "    !!! ${h}: 302 but to '${target}', not ${LOGIN_HOST}" >&2; rc=1 ;;
      esac ;;
    "")
      echo "    !!! ${h}: no response" >&2; rc=1 ;;
    5*)
      # A 5xx is the door JAMMED SHUT, not open -- nginx returns 500 when the
      # auth subrequest itself fails, which is what happens when tinyauth is
      # unreachable. Measured 2026-09-04 by detaching tinyauth from
      # nas-network: every protected route went to 500 while jellyfin, ntfy,
      # nextcloud and the apex kept serving 200/302. That asymmetry is the
      # design (ADR-0034) -- but it still means these routes are DOWN.
      echo "    !!! ${h}: anonymous request got ${code}. The door is JAMMED" >&2
      echo "        SHUT, not open: nginx 500s when the auth subrequest fails," >&2
      echo "        i.e. tinyauth is unreachable. Every protected route is down" >&2
      echo "        and the unprotected ones are fine. Check tinyauth. ADR-0034" >&2
      rc=1 ;;
    *)
      echo "    !!! ${h}: anonymous request got ${code}, not a redirect to the" >&2
      echo "        login page. THE DOOR IS OPEN -- this route is answering the" >&2
      echo "        internet with no login at all. (ADR-0034)" >&2; rc=1 ;;
  esac
done

for h in $NEVER; do
  read -r code target <<<"$(probe "https://${h}.${DOMAIN}/")"
  case "$target" in
    https://${LOGIN_HOST}/*)
      echo "    !!! ${h}: redirects to the login page and must not. Read" >&2
      echo "        ADR-0034 -- ntfy especially: a door on the alert channel" >&2
      echo "        makes a broken door a SILENT one." >&2; rc=1 ;;
  esac
  [ -z "$code" ] && { echo "    !!! ${h}: no response" >&2; rc=1; }
done

read -r code target <<<"$(probe "https://${DOMAIN}/")"
[ "$code" = "200" ] || { echo "    !!! apex answered ${code}, not 200" >&2; rc=1; }
read -r code target <<<"$(probe "https://${DOMAIN}/ops.html")"
case "$target" in
  https://${LOGIN_HOST}/*) ;;
  *) echo "    !!! /ops.html answered ${code} -> '${target}'; live stack status" >&2
     echo "        is public (ADR-0034)" >&2; rc=1 ;;
esac

if [ $rc -eq 0 ]; then
  n=0; for h in $PROTECT; do n=$((n + 1)); done
  echo "    ok: ${n} doors closed, apex public, /ops.html gated"
fi
exit $rc
