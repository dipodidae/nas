# ADR-0018 — Known gap: playlist-generator and its db do not drop capabilities

**Date:** 2026-09-02
**Status:** **closed 2026-09-02** (both sets measured, not guessed)

## The gap

ADR-0001 says every service gets `cap_drop: ALL`. Two do not:

- `playlist-generator`
- `playlist-generator-db`

Both have `security_opt: no-new-privileges:true`, `restart: unless-stopped` and
capped logging — they extend the `svc-base` fragment — but neither drops
capabilities. They are the only two services in the stack that don't.

## Why it was not fixed here

Adding `cap_drop: ALL` is a semantic change, and it would have broken the
compose refactor's no-op contract. More importantly it is **not obviously
safe**: the playlist-generator image runs nginx (basic auth on :80) plus
FastAPI, and nginx binding :80 needs `NET_BIND_SERVICE`; the pgvector image
runs an entrypoint that chowns `PGDATA` and drops to `postgres`, which needs
`CHOWN`/`SETUID`/`SETGID`/`DAC_OVERRIDE` (and possibly `FOWNER`). Guessing the
set and finding out at the next restart is how a database fails to come back up.

## How it was closed

Both sets were measured on the running containers, one service at a time, with a
`pg_dump` taken first. The starting points guessed above were both wrong in a
way that mattered, which is the argument for measuring rather than reasoning.

### `playlist-generator-db` — `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETUID`, `SETGID`

`pid 1` already ran at `CapEff: 0000000000000000`, because the postgres
entrypoint `gosu`s down to uid 999 before the server starts. **The running
database needs nothing.** Every capability in the set is for the entrypoint,
before that hand-off.

`FOWNER` was missing from the guess and `DAC_OVERRIDE` was in it for the wrong
reason. `PGDATA` is `drwx------ 999:1000` and the entrypoint walks it with
`find` **as root** — and root's ability to ignore file modes *is*
`CAP_DAC_OVERRIDE`. With it dropped the container looped on `find:
'/var/lib/postgresql/data': Permission denied` and never started. That failure
was reached deliberately, with the dump in hand, and is the reason the
capability is in the list.

No `KILL`: every postgres process runs as the same uid 999, verified by listing
each pid's uid, so signalling crosses no uid boundary.

### `playlist-generator` — `CHOWN`, `SETUID`, `SETGID`, `KILL`, `NET_BIND_SERVICE`

nginx binds `0.0.0.0:80` (`NET_BIND_SERVICE`), its master is uid 0 and spawns
`user www-data;` workers as uid 33 (`SETUID`/`SETGID`), and `CHOWN` covers
nginx's cache and log directories at startup. uvicorn stays on
`127.0.0.1:8000` as root and needs nothing.

`KILL` was missing from the guess and is the interesting one. The master→worker
uid boundary is the same shape as SWAG's, and `kill()` across it needs
`CAP_KILL` regardless of being root — see ADR-0021 for the mechanism and the
`EPERM` probe. Without it this container starts **healthy**, serves traffic, and
passes every functional check, then silently fails to reload or stop gracefully.
It was verified by timing a stop (1 s, not a grace-period SIGKILL) and counting
workers across a reload (16 before, 16 after, no `kill() failed`), because no
healthcheck can see it.

`DAC_OVERRIDE` is deliberately **not** granted here: everything this container
touches is root-owned (`/etc/nginx/.htpasswd` is `0:0`), so there are no
permission checks to bypass. The contrast with the database above is the point —
the same capability was necessary in one container and unnecessary in the other,
for a reason visible only on the box.

## Enforcement

`CAP_DROP_WAIVER` in `scripts/check-invariants.sh` is now **empty** — kept in
place rather than deleted, so the next gap has an obvious home and stays a
warning rather than becoming the convention. The generic `cap-drop-all` check
enforces every service in the stack, and `nginx-cap-kill` (ADR-0021) covers both
nginx services so this cannot regress silently.
