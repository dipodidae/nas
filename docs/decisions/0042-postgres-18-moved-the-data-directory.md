# ADR-0042 — Postgres 18 moved the data directory, and "applied" is not "running"

**Date:** 2026-09-15
**Status:** accepted
**Same shape as:** ADR-0035, ADR-0039, ADR-0041 — a green healthcheck over a change
that never landed

## Two findings, discovered together

The second is what caught the first, which is the only reason this is one ADR.

## 1. Postgres 18's images refuse the old bind mount

`postgres:18` and everything built on it (here: `pgvector/pgvector`,
`tensorchord/vchord-postgres`) store the cluster under a **major-versioned
subdirectory** — `/var/lib/postgresql/18/docker` — so that `pg_upgrade --link` never
has to cross a mount boundary (docker-library/postgres#1259).

The images therefore **hard-error when anything is mounted at
`/var/lib/postgresql/data`** — empty or not:

```
Error: in 18+, these Docker images are configured to store database data in a
       format which is compatible with "pg_ctlcluster" …
       Counter to that, there appears to be PostgreSQL data in:
         /var/lib/postgresql/data (unused mount/volume)
```

So **a pg17 → pg18 bump is a compose _volume_ change as well as a tag change**:

```yaml
- ${CONFIG_DIRECTORY}/streamystats-db:/var/lib/postgresql # NOT .../data
```

Measured here 2026-09-15: with the tag bumped and the mount untouched, the container
crash-looped. Nothing said why except `docker logs` — the plan was green, the
compose model was valid, and `make check` passed.

## 2. `applied` is not `running` — a pinned bump can be a silent no-op

`scripts/stack_update.py` pulled the target image and ran `docker compose up -d`, and
**never edited the pinned tag in the compose file**. `up -d` reads the tag from the
file, so compose recreated the container from the tag that was still written there.

The result was a clean, confident lie. On the first streamystats-db run it reported:

```
[6/7] verify: 32 tables before -> 32 after (pg_restore exit 0)
ok -- pg18 live, 32 tables restored
==> applied=1 skipped=0 failed=0 -> exit 0
```

and the server answered `show server_version` → **17.4**. Every step had genuinely
succeeded — the dump was real, the cluster was rebuilt, the restore was complete and
the data was intact. The only thing that had not happened was the upgrade.

Nothing in the run could have caught it, because every check asked the _plan_ what
had happened. The fix is one line of a different kind: **ask the container.**

```python
live = running_tag(service)          # docker inspect .Config.Image
if kind is Kind.PINNED and live != target:
    fail("reports healthy but is running {live}, not {target}")
```

and for a database, ask the server itself — `show server_version` — _before_ pouring
a restore into it, since a healthy container on the old major will accept the data
happily and leave the upgrade silently un-done.

## Why it matters beyond this script

This is the ADR-0022 lesson in a new place: **the label is not the mechanism.** There,
`swag=enable` documented a route that only a proxy-conf could create. Here, the plan
documented an upgrade that only the compose file could create. Both drifted silently
because the thing asserting success and the thing doing the work were different
objects, and only the first was ever consulted.

The general rule this repo keeps rediscovering: _verify the noun you changed, not the
verb you ran._ `pull` succeeding says an image is on disk. `up -d` succeeding says a
container exists. Neither says the container is running the image you meant.

## The guards

- `stack_update.py` edits the pinned tag in the compose file (and regenerates
  `diun/manifest.yml` in the same step, per ADR-0024), then **re-reads the running
  container** and fails if the tag is not the one it asked for.
- It moves the PGDATA bind mount to the parent for pg18+ automatically
  (`needs_parent_mount`), before starting the new major.
- `wait_healthy` gives up after three `restarting` observations instead of waiting out
  its 900 s timeout — a restart loop never becomes healthy, and the 15 minutes spent
  learning that were 15 minutes of downtime.
- Tests pin all of it, including the regex bug found while writing them: `\s*$` is
  greedy **across newlines**, so a match on a file's last line eats its terminator and
  welds the rewritten line to the next one. It is `[ \t]*$`.

## What is deliberately NOT automated

`pg_upgrade --link` is the fast path and this does not use it. Dump-and-restore is
slower and needs the disk for both clusters at once, but it rebuilds indexes and
extensions on the new major rather than carrying old on-disk structures forward, and
the old cluster is left **renamed, not deleted** — `<service>.pre-pg18-<timestamp>` —
which is the entire rollback. Delete it by hand once the new one has proved itself.
