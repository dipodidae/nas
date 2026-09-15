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
# Four assertions, and the second and third matter as much as the first:
#   * every `protect` route answers 3xx to an anonymous request, and the
#     redirect points at the login page -- a 200 means the door is OPEN;
#   * every `never` route does NOT redirect to the login page -- ntfy above
#     all, because the alert channel behind a broken door is a SILENT one;
#   * the apex is public and /ops.html is not;
#   * every `protect` route answers a request WITH A BODY the same way and
#     just as fast. Added 2026-09-11 after a 21-hour outage in which each of
#     the three probes above passed continuously: the subrequest kept the
#     parent's Content-Length while sending no body, so nginx waited 60s to
#     write a body it never sends and 500'd -- but ONLY for requests that
#     carried one. Every GET stayed green, so the apps loaded perfectly and
#     only *submits* failed. A door proven with GET alone is not proven.
#     ADR-0036 (sequel).
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
         lidarr-bulk playlist-generator ongehoord jellyseerr adguardhome
         navidrome"
NEVER="jellyfin nextcloud ntfy auth"

probe() { curl -s -o /dev/null -m 10 -w '%{http_code} %{redirect_url}' "$1" 2>/dev/null; }

# Same probe, but carrying a one-byte body -- the case the GET probes cannot
# see. The timeout is deliberately SHORT: the failure mode this catches is a
# 60s stall (nginx's default proxy_read_timeout), so anything that does not
# answer well inside that window has already failed. A healthy route answers
# in ~6ms.
probe_body() {
  curl -s -o /dev/null -m 15 -w '%{http_code} %{redirect_url}' \
    -X POST --data-binary 'x' "$1" 2>/dev/null
}

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

# Every protected route, again, with a body. See the header comment.
for h in $PROTECT; do
  read -r code target <<<"$(probe_body "https://${h}.${DOMAIN}/")"
  case "$code" in
    30[12378])
      case "$target" in
        https://${LOGIN_HOST}/*) ;;
        *) echo "    !!! ${h}: body request redirected to '${target}', not ${LOGIN_HOST}" >&2; rc=1 ;;
      esac ;;
    000|"")
      # curl reports 000 for "no HTTP response at all", which for this probe
      # means the 60s stall -- NOT an open door. Keep this branch ahead of the
      # catch-all: a stall misreported as "THE DOOR IS OPEN" sends the reader
      # looking for a security hole instead of a hung subrequest.
      echo "    !!! ${h}: a request WITH A BODY got no response, while the same" >&2
      echo "        route answers a GET fine. That asymmetry is the /tinyauth" >&2
      echo "        subrequest stalling on an inherited Content-Length: nginx" >&2
      echo "        announces a body it never sends and never reads tinyauth's" >&2
      echo "        reply. Check that swag/tinyauth-server.conf still pairs" >&2
      echo "        proxy_pass_request_body off with Content-Length \"0\"." >&2
      echo "        Symptom: apps load, every submit fails. ADR-0036" >&2
      rc=1 ;;
    5*)
      echo "    !!! ${h}: a request WITH A BODY got ${code}, while a GET is fine." >&2
      echo "        Read /config/log/nginx/error.log: 'auth request unexpected" >&2
      echo "        status: 504' is the Content-Length stall (ADR-0036 sequel)," >&2
      echo "        '400' is the proxy.conf / empty-Content-Length fault." >&2
      rc=1 ;;
    *)
      echo "    !!! ${h}: a request WITH A BODY got ${code}, not a redirect to" >&2
      echo "        the login page. THE DOOR IS OPEN to anything carrying a" >&2
      echo "        body. (ADR-0034)" >&2; rc=1 ;;
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

# Navidrome's Subsonic API is path-scoped OPEN, and that needs asserting in the
# same breath as the door itself -- it is the half that breaks silently. If the
# `location /rest` block ever loses its place in the conf, `location /` catches
# the path instead, the route starts 302ing, and every phone stops playing music
# while the browser UI and every check above stay perfectly green. ADR-0044.
read -r code target <<<"$(probe "https://navidrome.${DOMAIN}/rest/ping.view")"
case "$target" in
  https://${LOGIN_HOST}/*)
    echo "    !!! navidrome /rest redirects to the login page. That is the" >&2
    echo "        Subsonic API: no mobile client can follow a 302, so every" >&2
    echo "        phone has silently stopped playing. Check that the" >&2
    echo "        'location /rest' block still exists in" >&2
    echo "        swag/proxy-confs/navidrome.subdomain.conf. ADR-0044" >&2
    rc=1 ;;
  *)
    [ -z "$code" ] && { echo "    !!! navidrome /rest: no response" >&2; rc=1; } ;;
esac

if [ $rc -eq 0 ]; then
  n=0; for h in $PROTECT; do n=$((n + 1)); done
  echo "    ok: ${n} doors closed (GET and with a body), apex public, /ops.html gated"
fi
exit $rc
