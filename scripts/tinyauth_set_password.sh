#!/usr/bin/env bash
#
# tinyauth_set_password.sh -- rotate THE password for the whole public surface.
#
# There is one tinyauth credential and it opens all 13 protected routes
# (ADR-0034), so this is not a small change and the script treats it as one:
# it proves the new hash works BEFORE writing it anywhere, and rolls .env back
# if the live service does not accept it afterwards.
#
# What it does, in order:
#   1. reads the pinned image from the COMPOSE MODEL, never a literal, so the
#      hash is always minted by the version that will verify it
#   2. prompts for the password twice (or --stdin) and never echoes it
#   3. mints a bcrypt hash with the vendored `tinyauth user create`
#   4. PROVES the hash against a throwaway tinyauth on a random loopback port:
#      the new password must return 200 and a wrong one must return 401. If
#      either fails, nothing has been touched yet and it exits 2
#   5. backs up .env, then rewrites it atomically with the hash SINGLE-QUOTED
#   6. `make tinyauth-users`, revoke sessions, stop + `up -d` (a RESTART is
#      required: tinyauth parses the users file only at start, and `up -d`
#      alone is a no-op because compose compares config, not file contents)
#   7. verifies through SWAG over HTTPS: new password 200, and a protected route
#      still 302s to the login page anonymously. NO wrong-password probe here --
#      it would consume one of tinyauth's three retry slots and can lock the
#      account; the throwaway in step 4 already proved a wrong one is rejected
#   8. rolls .env back and restarts if step 7 fails, rather than leaving you
#      locked out of every protected route
#
# WHY SESSIONS ARE REVOKED BY DEFAULT. tinyauth's sessions live in
# ${CONFIG_DIRECTORY}/tinyauth/tinyauth.db keyed by uuid, with no reference to
# the password -- verified 2026-09-04, 4 rows, columns
# (uuid, username, email, name, provider, totp_pending, oauth_groups, expiry).
# So a password change ALONE leaves every existing session valid until it
# expires (default 24h). If you are rotating because the old password leaked,
# the sessions minted with it are exactly what you need gone. `--keep-sessions`
# opts out. Sessions are deleted while the container is STOPPED, so nothing
# writes to that SQLite concurrently.
#
# ACCEPTED EXPOSURE, stated rather than hidden: `tinyauth user create` takes
# `--password` as an argument, and its `--interactive` form needs a real TTY
# (bubbletea: could not open TTY) so it cannot be piped. The password is
# therefore visible in `ps` on this host for roughly one second. That is
# accepted because anyone who can read /proc here can already read .env and
# secrets/tinyauth-users. No bcrypt library is installed to hash it in-process,
# and adding one to scripts/requirements.txt for this alone is not worth the
# CI surface.
#
# `set -e` is deliberately ABSENT (as in offsite_backup.sh): this script has a
# rollback path and must reach it rather than dying at the failing line.
#
# Usage:
#   scripts/tinyauth_set_password.sh                 # prompt twice
#   scripts/tinyauth_set_password.sh --stdin         # read one line from stdin
#   scripts/tinyauth_set_password.sh --user alice    # also change the username
#   scripts/tinyauth_set_password.sh --keep-sessions --yes
#
# Proving the rollback still works (it is the reason this is safe to run at
# all, so it must not rot):
#   TINYAUTH_ROTATE_FORCE_VERIFY_FAIL=1 scripts/tinyauth_set_password.sh --stdin --yes
# It should restore .env, restart tinyauth, leave the OLD password working, and
# exit 2.
#
# Exit codes (AGENTS.md):
#   0  rotated and verified
#   1  rotated and verified, but `make check` reported a separate violation
#   2  fatal -- nothing changed, or changed and rolled back
set -uo pipefail
IFS=$'\n\t'

cd "$(dirname "$0")/.." || exit 2

USERNAME=""
READ_STDIN=0
REVOKE=1
ASSUME_YES=0
FORCE=0
MIN_LEN=12

while [ $# -gt 0 ]; do
  case "$1" in
    --user)           USERNAME="${2:?--user needs a value}"; shift 2 ;;
    --stdin)          READ_STDIN=1; shift ;;
    --keep-sessions)  REVOKE=0; shift ;;
    --yes|-y)         ASSUME_YES=1; shift ;;
    --force)          FORCE=1; shift ;;
    -h|--help)        sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "FATAL: unknown argument '$1' (try --help)" >&2; exit 2 ;;
  esac
done

# Read .env WITHOUT sourcing it. An unquoted value executes as a command in any
# shell, and the bcrypt hash specifically aborts a `set -u` shell with
# `$2: unbound variable` -- see ADR-0034.
env_get() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1 | sed "s/^['\"]//;s/['\"]$//"; }

die() { echo "FATAL: $*" >&2; exit 2; }

# ------------------------------------------------------------------ preflight

[ -w .env ]     || die ".env is not writable from $(pwd)"
[ -d secrets ] || mkdir -p secrets || die "cannot create secrets/"
command -v docker  >/dev/null 2>&1 || die "docker not found"
command -v python3 >/dev/null 2>&1 || die "python3 not found"
command -v curl    >/dev/null 2>&1 || die "curl not found"
command -v make    >/dev/null 2>&1 || die "make not found"

DOMAIN="$(env_get PUBLIC_DOMAIN)"
[ -n "$DOMAIN" ] || die "PUBLIC_DOMAIN is not set in .env"

IMAGE="$(docker compose config --format json 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["tinyauth"]["image"])' 2>/dev/null)"
[ -n "$IMAGE" ] || die "could not read tinyauth's image from the compose model"

[ -n "$USERNAME" ] || USERNAME="$(env_get TINYAUTH_USER)"
[ -n "$USERNAME" ] || die "no username: pass --user NAME or set TINYAUTH_USER in .env"
case "$USERNAME" in *:*) die "username may not contain ':' -- it is the users-file separator" ;; esac

echo "==> rotating the tinyauth password"
echo "    user   : $USERNAME"
echo "    image  : $IMAGE  (from the compose model, so it matches the pin)"
echo "    door   : https://auth.$DOMAIN  --  opens every protected route"
if [ "$REVOKE" -eq 1 ]; then
  echo "    sessions: WILL BE REVOKED (every browser must log in again)"
else
  echo "    sessions: kept -- existing logins stay valid until they expire"
fi

# ------------------------------------------------------------ read the secret

if [ "$READ_STDIN" -eq 1 ]; then
  IFS= read -r PW || die "no password on stdin"
else
  [ -t 0 ] || die "no TTY for the prompt; use --stdin"
  printf '    new password: ' >&2; IFS= read -rs PW; printf '\n' >&2
  printf '    again       : ' >&2; IFS= read -rs PW2; printf '\n' >&2
  [ "$PW" = "$PW2" ] || die "the two entries do not match; nothing changed"
  unset PW2
fi

[ -n "$PW" ] || die "empty password"
case "$PW" in *$'\n'*|*$'\r'*) die "password may not contain a newline" ;; esac
if [ "${#PW}" -lt "$MIN_LEN" ] && [ "$FORCE" -eq 0 ]; then
  die "password is ${#PW} chars; minimum is $MIN_LEN because this one credential
       opens 13 routes. Override with --force if you really mean it."
fi

if [ "$ASSUME_YES" -eq 0 ]; then
  [ -t 0 ] || die "not a TTY and --yes was not given"
  printf '    proceed? [y/N] ' >&2; IFS= read -r ans
  case "$ans" in [yY]*) ;; *) echo "Nothing done."; exit 0 ;; esac
fi

# --------------------------------------------------------------- scratch space

TMP="$(mktemp -d)" || die "mktemp failed"
PROBE=""
cleanup() {
  [ -n "$PROBE" ] && docker rm -f "$PROBE" >/dev/null 2>&1
  [ -n "${TMP:-}" ] && rm -rf "$TMP"
}
trap cleanup EXIT
umask 077

# ------------------------------------------------------------- mint the hash

echo "==> minting a bcrypt hash with the pinned image"
RAW="$(docker run --rm "$IMAGE" user create \
        --username "$USERNAME" --password "$PW" 2>&1 \
        | sed -e 's/\x1b\[[0-9;]*m//g')"
HASH="$(printf '%s\n' "$RAW" | sed -n "s|^ *- ${USERNAME}:||p" | tail -1)"

if ! printf '%s' "$HASH" | grep -qE '^\$2[aby]\$[0-9]{2}\$.{53}$'; then
  echo "$RAW" >&2
  die "did not get a bcrypt hash back from '$IMAGE user create' (see above).
       The subcommand has moved between majors -- check \`user create --help\`."
fi
echo "    ok: ${HASH:0:7}... (${#HASH} chars)"

# ------------------------------------- prove it BEFORE writing it anywhere

echo "==> proving the hash against a throwaway tinyauth (nothing changed yet)"
printf '%s:%s\n' "$USERNAME" "$HASH" > "$TMP/users"
chmod 0600 "$TMP/users"
mkdir -p "$TMP/data"

PROBE="tinyauth-pwprobe-$$"
# A well-formed FAKE hostname, set here rather than inline: a `#` comment inside
# a backslash-continued command silently swallows the rest of it, which is how
# the first version of this script lost its IMAGE argument.
#
# v5.1.3 refuses an IP ("ip addresses not allowed") and refuses bare "localhost"
# ("invalid app url, must be in format subdomain.domain.tld or domain.tld") --
# it derives the session cookie domain from this. The probe only reads HTTP
# status codes, never a cookie, so the hostname need not resolve.
PROBE_APPURL="http://probe.tinyauth.test"
# -p 127.0.0.1:0:3000 lets Docker pick a free host port, so there is no
# race against `ss` and no collision with lidarr-bulk's 3000 (ADR-0023).
docker rm -f "$PROBE" >/dev/null 2>&1
if ! docker run -d --name "$PROBE" \
      -u "$(id -u):$(id -g)" \
      -p 127.0.0.1:0:3000 \
      -v "$TMP/data:/data" -v "$TMP/users:/secrets/users:ro" \
      -e TINYAUTH_APPURL="$PROBE_APPURL" \
      -e TINYAUTH_AUTH_USERSFILE=/secrets/users \
      -e TINYAUTH_LABELPROVIDER=none \
      -e TINYAUTH_DATABASE_PATH=/data/tinyauth.db \
      -e TINYAUTH_ANALYTICS_ENABLED=false \
      "$IMAGE" >/dev/null 2>&1; then
  die "could not start the verification container"
fi

PORT="$(docker port "$PROBE" 3000/tcp 2>/dev/null | head -1 | sed 's/.*://')"
[ -n "$PORT" ] || die "could not learn the verification container's port"

ok=0
for _ in $(seq 1 30); do
  if docker exec "$PROBE" tinyauth healthcheck >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
[ "$ok" -eq 1 ] || { docker logs "$PROBE" 2>&1 | tail -20 >&2; die "verification container never became healthy"; }

login_code() { # $1 = base url, $2 = password
  python3 - "$1" "$USERNAME" "$2" <<'PY'
import json, sys, urllib.error, urllib.request
base, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
req = urllib.request.Request(
    f"{base}/api/user/login",
    data=json.dumps({"username": user, "password": pw}).encode(),
    headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.status)
except urllib.error.HTTPError as e:
    print(e.code)
except Exception as e:                      # noqa: BLE001 -- report, do not raise
    print(f"ERR {e}")
PY
}

GOOD="$(login_code "http://127.0.0.1:$PORT" "$PW")"
BAD="$(login_code "http://127.0.0.1:$PORT" "definitely-not-the-password-$$")"
echo "    new password -> $GOOD   (want 200)"
echo "    wrong one    -> $BAD   (want 401)"
[ "$GOOD" = "200" ] || die "the minted hash does NOT accept the password you typed. Nothing changed."
[ "$BAD" = "401" ] || die "a wrong password was not rejected ($BAD). Nothing changed."
docker rm -f "$PROBE" >/dev/null 2>&1; PROBE=""

# ------------------------------------------------------------- commit to .env

BACKUP=".env.bak.$(date +%Y%m%d-%H%M%S)"      # .env.bak* is gitignored
cp -p .env "$BACKUP" || die "could not back up .env"
echo "==> .env backed up to $BACKUP"

COMMITTED=0
rollback() {
  [ "$COMMITTED" -eq 1 ] && return 0
  echo "!!! rolling back .env from $BACKUP" >&2
  cp -p "$BACKUP" .env 2>/dev/null || echo "!!! ROLLBACK FAILED -- restore $BACKUP by hand" >&2
  make tinyauth-users >/dev/null 2>&1
  # RESTART, not `up -d`. Compose compares the service CONFIG, not the contents
  # of a bind-mounted file, so `up -d` on a running container is a NO-OP -- and
  # tinyauth parses the users file only at start. The first version of this
  # rollback used `up -d`: it restored .env correctly, reported success, and
  # left the process authenticating against the password it was rolling BACK
  # from. Measured 2026-09-04. The happy path below gets away with `up -d` only
  # because it stops the container first.
  docker compose restart tinyauth >/dev/null 2>&1
  for _ in $(seq 1 20); do
    [ "$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' tinyauth 2>/dev/null)" = "healthy" ] && break
    sleep 2
  done
}

set_kv() { # $1 = key, $2 = literal value (already quoted if it needs to be)
  local key="$1" val="$2" tmp="$TMP/env.new"
  if grep -q "^${key}=" .env; then
    python3 - "$key" "$val" .env "$tmp" <<'PY'
import sys
key, val, src, dst = sys.argv[1:5]
out, seen = [], False
for line in open(src, encoding="utf-8"):
    if line.startswith(f"{key}=") and not seen:
        out.append(f"{key}={val}\n"); seen = True
    elif line.startswith(f"{key}="):
        continue                      # collapse duplicates; the last wins anyway
    else:
        out.append(line)
open(dst, "w", encoding="utf-8").writelines(out)
PY
  else
    cp .env "$tmp" && printf '%s=%s\n' "$key" "$val" >> "$tmp"
  fi
  [ -s "$tmp" ] || return 1
  cat "$tmp" > .env                   # rewrite IN PLACE: keeps mode and inode
  return 0
}

# The hash is SINGLE-QUOTED on purpose. .env is `.`-sourced by two Makefile
# recipes running under `set -u`, where a bare $2a$10$... aborts with
# `$2: unbound variable` and silently skips every check after it. ADR-0034.
set_kv TINYAUTH_USER "$USERNAME"          || { rollback; die "could not update TINYAUTH_USER"; }
set_kv TINYAUTH_PASSWORD_HASH "'$HASH'"   || { rollback; die "could not update TINYAUTH_PASSWORD_HASH"; }

echo "==> rendering secrets/tinyauth-users"
make tinyauth-users >/dev/null || { rollback; die "make tinyauth-users failed"; }

# ------------------------------------------------------ apply + revoke + wait

echo "==> restarting tinyauth"
docker compose stop tinyauth >/dev/null 2>&1

if [ "$REVOKE" -eq 1 ]; then
  DB="$(env_get CONFIG_DIRECTORY)/tinyauth/tinyauth.db"
  if [ -f "$DB" ]; then
    n="$(python3 - "$DB" <<'PY'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
total = 0
for t in ("sessions", "oidc_sessions"):
    try:
        total += c.execute(f'select count(*) from "{t}"').fetchone()[0]
        c.execute(f'delete from "{t}"')
    except sqlite3.Error:
        pass
c.commit(); c.close()
print(total)
PY
)"
    echo "    revoked ${n:-0} session(s) while the container was stopped"
  else
    echo "    no session DB at $DB yet -- nothing to revoke"
  fi
fi

docker compose up -d tinyauth >/dev/null || { rollback; die "could not start tinyauth"; }

ok=0
for _ in $(seq 1 40); do
  s="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' tinyauth 2>/dev/null)"
  [ "$s" = "healthy" ] && { ok=1; break; }
  sleep 2
done
if [ "$ok" -ne 1 ]; then
  docker compose logs --tail 30 tinyauth >&2
  rollback
  die "tinyauth did not become healthy; rolled back"
fi
echo "    tinyauth: healthy"

# --------------------------------------------------- verify through the door

echo "==> verifying through SWAG over HTTPS"
# DELIBERATELY only the CORRECT password here, and no wrong-password probe.
#
# tinyauth's brute-force protection is LOGINMAXRETRIES=3 within LOGINTIMEOUT
# (300s), counted per identifier and held IN MEMORY. A wrong-password check
# against the live door consumes one of those three slots -- so two runs of this
# script plus one human typo locks the account, and the lockout then refuses the
# CORRECT password too. That happened on 2026-09-04: an ad-hoc wrong-password
# probe was the fourth failure, tinyauth logged `Account locked due to too many
# failed login attempts failedAttempts=4 identifier=tom`, and the owner was
# locked out while certain the password was right.
#
# Nothing is lost by dropping it: the throwaway probe above already proved that
# THIS EXACT HASH returns 401 for a wrong password, on the same binary. Repeating
# it against the live door tested nothing new and cost a retry slot.
#
# If a lockout does happen: `docker compose restart tinyauth` clears it
# immediately (the counter is in memory; sessions live in SQLite and survive),
# or wait out LOGINTIMEOUT.
GOOD="$(login_code "https://auth.$DOMAIN" "$PW")"
GATE="$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://sonarr.$DOMAIN/")"

# Test hook. The rollback path is the whole reason this script is safe to run on
# a door that opens 13 routes, and an unproven rollback is worse than none --
# so there has to be a way to make the live verification fail on purpose.
# Deliberately an env var and not a flag: a flag invites accidental use.
if [ -n "${TINYAUTH_ROTATE_FORCE_VERIFY_FAIL:-}" ]; then
  echo "    (TINYAUTH_ROTATE_FORCE_VERIFY_FAIL set -- forcing the rollback path)"
  GOOD="000"
fi
echo "    new password  -> $GOOD   (want 200)"
echo "    sonarr anon   -> $GATE   (want 302: the door is still shut)"

if [ "$GOOD" != "200" ]; then
  rollback
  die "the live door did not accept the new password; rolled back to $BACKUP"
fi
case "$GATE" in
  30[12378]) ;;
  *) rollback; die "a protected route answered $GATE instead of a redirect; rolled back" ;;
esac

COMMITTED=1
unset PW

# -------------------------------------------------------------- final gate

echo "==> make check"
rc=0
scripts/check-invariants.sh | tail -3 || rc=1

echo
echo "Done. One credential, rotated and proven end to end."
echo "  .env backup: $BACKUP  (delete it once you are happy -- it holds the OLD hash)"
[ "$REVOKE" -eq 1 ] && echo "  every browser must log in again at https://auth.$DOMAIN"
exit "$rc"
