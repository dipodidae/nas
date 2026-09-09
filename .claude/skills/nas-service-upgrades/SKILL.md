---
name: nas-service-upgrades
description: Use when updating or upgrading any service in this NAS stack - a `docker compose pull`, a pinned tag bump, a Postgres or app major version, a locally-built webapp, or acting on a diun `nas-updates` notification. Nothing here auto-applies, a tag is not a rollback because several of these migrate their database one way, and a crash-looping dependency can take SWAG down with it.
---

# Upgrading a service here

Nothing in this stack applies an update. Watchtower was retired (ADR-0025) after its
non-atomic stop→remove→create left **no container at all** for 13h; `diun` only
notifies, at 04:10 on `nas-updates` at priority 1 (ADR-0024). Every update is a
deliberate human act, and the two shapes are different work:

| Shape               | What it is                                                | How to apply                                                     |
| ------------------- | --------------------------------------------------------- | ---------------------------------------------------------------- |
| **Same-tag drift**  | `:latest`/`:nightly` moved; no repo change needed         | `docker compose pull <svc> && docker compose up -d <svc>`        |
| **Pinned tag bump** | jellyfin, qbittorrent, tinyauth, beszel, diun, …          | edit the tag, `make diun-manifest`, `make check`, then pull + up |
| **Locally built**   | 4eva-rootpage, lidarr-bulk, ongehoord, playlist-generator | `up -d --build` — a `pull` does nothing                          |

## Confirm the target version exists before planning anything

A plan built on a version that was never released wastes the whole window. Two
commands, and they disagree with wishful thinking:

```bash
curl -s "https://api.github.com/repos/<owner>/<repo>/releases?per_page=5" | grep tag_name
docker buildx imagetools inspect --format '{{.Manifest.Digest}}' <image>:<tag>
```

Do **not** trust a digest comparison to tell you whether the running container is
behind. `docker buildx imagetools inspect` and Docker's stored `RepoDigests`
disagree intermittently (OCI index vs manifest-list media types), which produced a
confident false "STILL BEHIND" on prowlarr and lidarr. **A `docker compose pull`
that downloads nothing is the authority.** `lidarr:nightly` rebuilds every night, so
it is always "behind" within a day — that is not drift worth chasing.

## A tag is not a rollback — back the state up first

Three services in this stack migrate their store **one way** on first start of the
new version. Reverting the tag then leaves the old binary unable to open its own
data:

| Service      | What happens                                                                 |
| ------------ | ---------------------------------------------------------------------------- |
| **jellyfin** | 30 migrations rewrite a 1.1 GB SQLite DB on first boot (ADR-0035)            |
| **tinyauth** | schema 11, and v5.1.3 has **no down-migration** — it crash-loops (ADR-0036)  |
| **postgres** | a major refuses a `PG_VERSION` mismatch outright; needs dump **and** restore |

How you back it up depends on the store, and **using the wrong one gives you a
green tick over an empty archive**:

**SQLite-backed (jellyfin, tinyauth, beszel, the \*arr apps).** Copy
`${CONFIG_DIRECTORY}/<service>`, but **never a plain `cp` of the `.db` alone** —
these are WAL-mode. Jellyfin left a 4.2 MB dirty WAL on every stop before it got
`CAP_KILL`; beszel's `data.db-wal` is 4.1 MB right now. Either stop the service
first and confirm `-wal`/`-shm` are gone, or archive all three files.
`scripts/config_backup.py --services <svc>` tars the whole tree — check the service
is in its `DEFAULT_SERVICES` list first, and verify the archive lists the `.db` at
full size rather than trusting the `✅` line.

**Postgres-backed (`playlist-generator-db`, `streamystats-db`) — the backup IS a
dump, and a filesystem tar is not a substitute.** Neither is in
`DEFAULT_SERVICES`, and PGDATA is `drwx------ 999:tom`, so the host user cannot
read a byte of it: `du -sh` on the host reports **4.0K** against **1.3 GB** the
container sees. A tar would "succeed" and be empty. Use the **newer** client
against the older server:

```bash
docker run --rm --network nas-network -v /mnt/drive/backups:/out \
  pgvector/pgvector:pg17 pg_dump -h playlist-generator-db -U <user> -d <db> \
  -Fc -f /out/<name>.dump
```

Then prove it by reading it — `pg_restore -l <file>` must list the real tables and
extensions — not by its exit code. Rename the old PGDATA aside rather than deleting
it (a same-filesystem `mv`, no sudo needed on the tom-owned parent); that rename is
what makes a Postgres major revertible at all.

Put backups on `/mnt/drive` (terabytes free), not the root LV — which is 78% used
and is also where `${CONFIG_DIRECTORY}` lives, so a Postgres major pays for **both**
clusters there while you cut over.

## Know the blast radius before you break the thing you are fixing

`swag` declares `depends_on: tinyauth: condition: service_healthy`. A crash-looping
tinyauth therefore means **swag does not start at all** — turning a
protected-routes outage into a total one, including jellyfin/nextcloud/ntfy/the apex
which are deliberately never gated. That is how a rollback made an incident strictly
worse on 2026-09-10.

Check `depends_on` before touching anything, and prefer going **forward** to a
working version over reverting into a store the old binary cannot read. Most
services have no such dependant — read the graph rather than assuming this warning
applies.

## During the window: `stop`, hold the lock, and mind who restores the schema

- **`stop`, never `down`/`rm`.** `stack_watchdog.py` (cron `*/5`) and
  `make verify-runtime` both page `nas-critical` when a compose service has **no
  container**. A stopped container still exists; a removed one is an alert.
- **Something on host cron may be writing the service while you migrate it.** Three
  jobs write `playlist-generator-db` (`*/30`, `:12`, `:42`), all gated on
  `/tmp/nas-playlist-stage.lock`. Hold that lock for the window
  (`flock -x <lock> -c 'sleep …'`) rather than editing the crontab — nothing to
  forget to restore, and the jobs skip cleanly instead of failing and alerting.
  Their `--max-age-min` freshness budgets are the real ceiling on how long you have.
- **Restore before the app starts.** `playlist-generator` initialises its schema on
  startup, so bringing it up before `pg_restore` finishes lands you in
  `relation already exists` halfway through. Sequencing is load-bearing.

## Green healthchecks lie during an upgrade

Every failure this session was invisible to health:

- tinyauth reported **healthy**, its login page served **200**, `nginx -t` passed — and every protected route was 500 because only the auth subrequest failed.
- Jellyfin's `encoding.xml` was rejected **whole** for one invalid value, so it booted on defaults and then **persisted them over the real settings**. Hardware transcoding silently off; the only trace was one `[ERR]` line at startup.

After any upgrade, diff the app's own config against the pre-upgrade backup rather
than assuming it survived. **REQUIRED:** verify by effect — see `verifying-by-effect`.

## Things a version bump silently breaks beyond the service itself

- **Version-string parsers.** Jellyfin dropped its leading `10.`, so `12.0` has two numeric components and the generated diun filter demanded three — it matched no 12.x tag and would have reported no jellyfin updates forever. Grep for the old version shape before assuming nothing parses it.
- **Consumers' API contracts.** Jellyfin 12.0's migration turned `EnableLegacyAuthorization` off, 401-ing `X-Emby-Token`, `X-MediaBrowser-Token` and `?api_key=` — which is what Sonarr/Radarr's library-update connections and Jellyseerr use. Check what talks to the service, not just the service.
- **Pinning too precisely.** An immutable fully-qualified tag (`0.8.6-pg17` rather than `pg17`) never moves its digest, so diun reports updates for it **forever silently**. That needs a `Policy` in `scripts/emit_diun_manifest.py` in the same commit.
- **Paired services must move together.** `beszel` and `beszel-agent` — a hub/agent version skew is the documented failure mode (ADR-0028).

## Never update the whole stack at once

`pnpm update` or a bare `up -d` trips on **ongehoord**, which is `pull_policy: build`
and is not buildable by plain compose (it needs buildx `--network=host`). Update per
service. And remember the four locally-built webapps need `--build`: a source fix
that is committed but not rebuilt is still running the old code — that happened to
`lidarr-bulk` this session.

## Finish the job

1. `make diun-manifest` after any pinned-tag edit, or `make check` fails on the stale manifest (ADR-0024).
2. `make check` and `make lint`.
3. `make verify-runtime` — it re-checks the live host, including settings that live outside the repo where a config restore can undo them (see `nas-runtime-vs-repo`).
4. **Add an assertion for whatever just bit you.** Every ADR here ends that way; a fix with no guard is a fix that gets reintroduced, and `make check`'s job is to make that impossible. Prove the new assertion **fails** with the fault reintroduced, not just that it passes clean.
5. Commit the compose edit and `diun/manifest.yml` together, so a revert is one commit.
