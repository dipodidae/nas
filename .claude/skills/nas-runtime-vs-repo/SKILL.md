---
name: nas-runtime-vs-repo
description: Use when checking, asserting or changing configuration in this NAS stack, or deciding whether an assertion belongs in make check or make verify-runtime. Much of what governs behaviour lives outside the repo - in app SQLite DBs, in gitignored config, in bind-mounted files whose contents compose ignores, and in upstream defaults nobody wrote down - so make check cannot see it and a config restore, a git checkout or a plain up -d can undo it silently.
---

# Runtime facts vs. repo facts

This repo has two classes of invariant and they need two different gates. Putting an
assertion in the wrong one makes it useless.

## `make check` — what the compose model can see

`scripts/check-invariants.sh`, 66 assertions. Static, no containers needed, runs in CI.
Use it for anything derivable from files in the repo: compose structure, capabilities,
labels vs proxy-confs, the generated cron tables, `jellyfin/logging.json` existing.

## `make verify-runtime` — what only the running system knows

Reads live containers and live APIs. **`VERIFY_NOTIFY ?= 0`, so it does not push unless
you ask** — the 06:15 cron sets `VERIFY_NOTIFY=1`. Running it by hand is safe.

Use it for anything that lives outside the repo. Currently:

- Lidarr's root folder is one the bridge can translate (`check-lidarr-bridge-root.py`)
- Lidarr notification 6 still has **no** `mapFrom`/`mapTo`, and the delete toggles are
  still off (`check-lidarr-jellyfin-notification.py`)
- slskd's **effective** config matches its pins (`check-slskd-effective-config.py`)
- Jellyfin's live `logging.json` matches the repo copy
- ntfy ACLs, \*arr notification connectors, container health
- every tracked SWAG conf is byte-identical to the one nginx is serving
  (`check-swag-conf-drift.sh`) -- a stale one is a route that lost its auth door
- the doors are actually shut, live (`check-door-live.sh`): every `protect` route `302`s to
  the login page anonymously, no `never` route does, the apex is `200` and `/ops.html` is
  not. Both of these escalate to `nas-critical`

## The four places config hides from git

### 1. \*arr SQLite databases

Root folders, indexer settings, download clients, notifications and every `onXxx` toggle
live in `.docker-config/<app>/<app>.db`. The compose model cannot see them and **a config
restore silently reverts them**.

Two traps when editing over the API:

- `GET /notification` returns `apiKey` **masked as asterisks**. Write the real key back
  before the `PUT` or you blank it. For Sonarr/Radarr that key is `API_KEY_JELLYFIN_ARR`
  (the `arr-integrations` key) — **not** `API_KEY_JELLYFIN`, which is Jellyseerr's.
- The DB is **WAL mode**. Confirm a change by reading the DB, not by re-`GET`ting (which
  re-masks) — and copy `*.db*` including `-wal`/`-shm`, or a just-saved `1` reads back
  as `0`.

Anything in here that matters gets an assertion in `verify-runtime`, not a comment.

### 2. Gitignored service config

`.docker-config/` is gitignored, so `slskd.yml` and Jellyfin's config are not in the repo.
The pattern for something that must be reviewable: keep the canonical copy at repo top
level (`jellyfin/logging.json`, `qbittorrent/custom-cont-init.d/`), install it with a
`make` target, and assert in `verify-runtime` that the live copy matches.

Note `/config` maps to `.docker-config/jellyfin/`, **not** `.docker-config/jellyfin/config/`.

### 3. Bind-mounted files, whose CONTENTS compose ignores

`secrets/tinyauth-users`, every `swag/proxy-confs/*.conf`, `swag/tinyauth-*.conf`,
`jellyfin/logging.json`. These _are_ in the repo, so `make check` can read them -- but
whether the **running process** has them is a different fact, and two traps sit between:

- `docker compose up -d <svc>` compares the service **config**, not file **contents**, so
  it does not recreate a running container and a new credential file never reaches the
  process. Use `docker compose restart <svc>`.
- Docker binds a single file by **inode**, so replacing the host file (`git checkout`,
  `git revert`, prettier, `sed -i`) detaches the mount silently. `make swag-apply`.

A conf baked into an image from a **submodule** is a third case: `git ls-files` cannot see
inside a submodule, so `no-auth-basic` names
`webapps/jellyfin-playlist-generator/nginx/app.conf` explicitly and reads it from the
working tree.

### 4. Upstream defaults nobody configured

The worst class, because nothing anywhere records them.

`transfers.download.retry` — `attempts 3, delay 5000, maxDelay 60000, partial resume` —
is absent from `slskd.yml` and present in the running config. Every measurement of
`lidarr_stuck_download_reaper.py` assumes it. Defaults change on upgrade, and the change
would read as the reaper getting worse.

> **Read the effective config from the process, not the file on disk.** A file is not
> evidence the process loaded it, and it omits everything the process supplied itself.

```bash
curl -s -H "X-API-Key: $API_KEY_SLSKD" http://localhost:5030/api/v0/options   # slskd
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://localhost:8686/api/v1/config/host
docker inspect <svc> -f '{{json .Config.Env}}'
```

If a value you did not set turns out to matter, **pin it** — add it to
`scripts/check-slskd-effective-config.py` (or an equivalent) with a comment saying what
assumes it.

## Inverted assertions (tripwires)

Some checks must fail when something **appears**, not when it goes missing.

`check-lidarr-jellyfin-notification.py` fails when `mapFrom`/`mapTo` show up on Lidarr's
Jellyfin connection, because that is the day the bridge can be retired and the delete
toggles become safe. Without it the workaround outlives its reason by years and the next
person cannot tell whether it is still needed.

Write one whenever you build a workaround for an upstream gap: the tripwire is the thing
that tells someone the workaround is finished.

## Adding a runtime assertion

Write it as a standalone script under `scripts/` with the repo's exit contract
(`0` ok / `1` drift / `2` unreachable), a docstring saying **why it exists** and what
incident it came from, then wire it into the `verify-runtime` target with a `note` line
so the ntfy message names the drift.
