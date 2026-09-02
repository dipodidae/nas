# ADR-0018 — Known gap: playlist-generator and its db do not drop capabilities

**Date:** 2026-09-02
**Status:** **open — documented, not fixed**

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

## How to close it

Determine the minimum set empirically, one service at a time, not by
copy-pasting another service's list:

```sh
# find out what it actually uses, without breaking it
docker exec playlist-generator sh -c 'capsh --print' 2>/dev/null
# then try the narrow set on ONE service and watch it come back healthy
docker compose up -d playlist-generator-db
docker compose logs -f playlist-generator-db
```

Expected starting points, to be verified rather than trusted:

- `playlist-generator-db`: `CHOWN`, `SETUID`, `SETGID`, `DAC_OVERRIDE`
- `playlist-generator`: `CHOWN`, `SETUID`, `SETGID`, `NET_BIND_SERVICE`

Do the database **second**, after the app has proven the pattern, and take a
`pg_dump` first.

## Until then

`scripts/check-invariants.sh` keeps asserting `cap_drop: ALL` stack-wide and
lists these two on an explicit, commented waiver list. It prints a **warning
naming both services on every run**, so the gap cannot quietly become the
convention. Remove them from the waiver as they are fixed; the check then
enforces it.
