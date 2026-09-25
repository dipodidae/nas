#!/usr/bin/env bash
#
# navidrome_plugins.sh -- install, configure and enable the pinned Navidrome
# plugins from navidrome/plugins/ (ADR-0053). `make navidrome-plugins`.
#
# What lives where, because most of it is NOT in git:
#   - the .ndp binaries    ${CONFIG_DIRECTORY}/navidrome/plugins/<id>.ndp
#                          (fetched here, sha256-pinned in plugins.lock)
#   - enabled flag, config,
#     grants               the `plugin` table in navidrome.db
#   - which agents run     ND_AGENTS / ND_LYRICSPRIORITY in compose -- tracked
#
# So a restored or fresh navidrome.db comes back with every plugin DISABLED
# and unconfigured, and Navidrome says nothing: Instant Mix quietly falls
# back to Last.fm, lyrics quietly stop. That is why this is a script and why
# `make verify-runtime` runs check-navidrome-plugins.py.
#
# Idempotent: a file whose sha256 already matches is not re-downloaded, and
# re-applying a config is a no-op. `${VAR}` in a <id>.json is expanded from
# .env (only the audiomuseai token uses it), so no secret is tracked.
#
# Ends with a `docker compose restart navidrome` -- see the note at the bottom.
#
# Exit: 0 all plugins enabled with their config; 2 anything failed.

set -euo pipefail
IFS=$'\n\t'

cd "$(dirname "$0")/.."
LOCK=navidrome/plugins/plugins.lock
CONFIG_DIRECTORY=$(sed -n 's/^CONFIG_DIRECTORY=//p' .env)
DEST="${CONFIG_DIRECTORY:?CONFIG_DIRECTORY unset in .env}/navidrome/plugins"
AUDIOMUSE_API_TOKEN=$(sed -n 's/^AUDIOMUSE_API_TOKEN=//p' .env)
export AUDIOMUSE_API_TOKEN

# Only `ndi` attaches stdin. A plain `docker exec -i` inside the read loops
# below swallows the rest of plugins.lock and silently ends the loop after
# the first plugin -- measured: one plugin enabled, exit 0.
nd() { docker exec navidrome navidrome --nobanner plugin "$@" </dev/null; }
ndi() { docker exec -i navidrome navidrome --nobanner plugin "$@"; }

[ -d "$DEST" ] || { echo "!!! $DEST missing -- is navidrome deployed?" >&2; exit 2; }

while IFS=' ' read -r id version sha url; do
  case "$id" in ''|'#'*) continue ;; esac
  f="$DEST/$id.ndp"
  if [ -f "$f" ] && [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sha" ]; then
    echo "==> $id $version: present"
  else
    echo "==> $id $version: fetching"
    tmp=$(mktemp "$DEST/.$id.XXXXXX")
    curl -fsSL -o "$tmp" "$url"
    got=$(sha256sum "$tmp" | cut -d' ' -f1)
    if [ "$got" != "$sha" ]; then
      rm -f "$tmp"
      echo "!!! $id: sha256 $got != pinned $sha -- NOT installed" >&2
      exit 2
    fi
    # rename, not overwrite-in-place: Navidrome may have the old file open.
    mv -f "$tmp" "$f"
  fi
done < <(tr -s ' ' < "$LOCK")

nd rescan >/dev/null 2>&1

while IFS=' ' read -r id _rest; do
  case "$id" in ''|'#'*) continue ;; esac
  # No <id>.json means the plugin has no config schema at all (coverartarchive);
  # Navidrome rejects even `{}` for those, so skip straight to enable.
  if [ -f "navidrome/plugins/$id.json" ]; then
    cfg=$(python3 -c 'import os,sys,json; print(json.dumps(json.loads(os.path.expandvars(open(sys.argv[1]).read()))))' \
          "navidrome/plugins/$id.json")
    case "$cfg" in *'${'*) echo "!!! $id: unexpanded \${VAR} in its config -- set it in .env" >&2; exit 2 ;; esac
    # A plugin that declares the `library` permission will not enable without
    # a library grant. Grant read, NEVER write: nd-lyrics asks for filesystem
    # access only to write .lrc sidecars, /music is mounted :ro anyway, and
    # writeLyrics is off in its config.
    grant=()
    if nd info "$id" -f json 2>/dev/null | python3 -c 'import json,sys; sys.exit(0 if "library" in json.loads(json.load(sys.stdin)["manifest"]).get("permissions", {}) else 1)'; then
      grant=(--all-libraries)
    fi
    printf '%s' "$cfg" | ndi edit "$id" --config-file - "${grant[@]}" --no-write-access >/dev/null 2>&1 \
      || { echo "!!! $id: config rejected" >&2; { ndi edit "$id" --config-file - <<<"$cfg" 2>&1 || true; } | grep -o 'error=.*' >&2; exit 2; }
  fi
  { nd enable "$id" 2>&1 || true; } | grep -o 'error=.*' >&2 && { echo "!!! $id: enable failed" >&2; exit 2; }
  echo "==> $id: configured + enabled"
done < <(tr -s ' ' < "$LOCK")

# The CLI writes navidrome.db directly; the RUNNING server does not re-read a
# plugin's config or enabled flag from it. Restart so what `plugin list` says
# is also what the server is doing. A scan in flight resumes on its own.
echo "==> restarting navidrome to load the plugin table"
docker compose restart navidrome >/dev/null 2>&1
for _ in $(seq 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' navidrome)" = healthy ] && break
  sleep 2
done
nd list 2>/dev/null | grep -v -e '^time=' -e '^$'
docker logs --since 1m navidrome 2>&1 | grep -o 'msg="Loaded plugin".*' || true
