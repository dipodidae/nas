# 0037 — The nightly config archive discovers its services and snapshots databases

**Status:** accepted · **Date:** 2026-09-13 · **Supersedes nothing**

## Context

`scripts/config_backup.py` has run nightly at 01:00 since the stack was built,
exiting 0-or-1 and writing a ~437 MB `configs-*.tar.gz` to
`/mnt/drive/backups/nas-configs`, keeping five. `stack_watchdog` confirmed it
ran. Nobody had opened one.

Opening the 2026-09-12 archive found three independent faults, each of which
alone would have made the backup useless:

**1. It covered eight of twenty-eight service directories.** The set came from a
hard-coded `DEFAULT_SERVICES` list of nine names. It still contained
`lazylibrarian`, retired long ago — which is why the job printed
`Services processed: 8/9` and exited **1** every single night. Exit 1 is inside
`cron_job.py`'s default `--ok-codes 0,1`, so it never alerted, and the failure
was in effect load-bearing camouflage: a genuinely partial backup would have
looked identical.

Absent from every archive: **lidarr** (the whole music pipeline — artists,
MBIDs, indexer 4's fallback flags per ADR-0003), **tinyauth** (ADR-0036 says to
copy `${CONFIG_DIRECTORY}/tinyauth` before a tag bump _because its migration is
one-way_; nothing was copying it), **slskd**, **cleanuparr** (an armed deletion
engine with `dryRun: false`, ADR-0017), **qui**, **ntfy**, **jellyseerr**,
**diun**, **scrutiny**, **recyclarr**, **beszel**, **subcleaner**, **lingarr**.

**2. `--fast`'s 25 MB per-file cap was dropping the databases.** The cron line
passes `--fast`, which sets `max_file_size = 25.0` MB. `sonarr.db` is 41 MB,
`prowlarr.db` 32 MB, `jellyfin.db` larger still — all three silently skipped.
The summary line said `Skipped (size): 9`, a count with no names. `radarr.db`
survived only by being 7.5 MB. So even for the eight services it did cover, the
archive contained no database for most of them.

What it _did_ contain: 437 MB of Nextcloud's PHP tree, Jellyfin `.sup` subtitle
extracts, `*arr` MediaCover poster caches, and a 25 MB yt-dlp temp file — all of
it regenerable, none of it state.

**3. Databases were tarred raw.** Every `*arr`, Jellyfin, slskd, qBittorrent,
tinyauth and cleanuparr database here is SQLite in **WAL** mode, where the
newest committed rows live in `<db>-wal` until a checkpoint. `slskd/data` had
`events.db-wal` at 4 MB and `transfers.db-wal` at 5.2 MB at the time of writing.
Copying `x.db` alone reads back stale, possibly torn — the same trap CLAUDE.md
already documents for reading a just-saved `*arr` setting.

## Decision

**The service set is discovered, not listed.** `discover_services()` returns
every directory under `CONFIG_DIRECTORY` minus `SKIP_SERVICES`, a dict that
carries the reason for each exclusion (`whisper`: re-downloadable model weights;
`*-db`: live Postgres data dirs, which need `pg_dump` — a file tar of a running
cluster does not restore; `beszel-agent`: no state), plus `SKIP_SERVICE_GLOBS`
for `*.bak.*` manual snapshots. A new service is protected the day it is
created, not the day someone remembers the list.

**The size cap never applies to a SQLite file**, and files it does skip are
**named** in the summary rather than counted.

**Databases are copied through `sqlite3.Connection.backup()`**, the online
backup API, which folds the WAL into one consistent file. `-wal`/`-shm`/
`-journal` sidecars are then excluded as redundant. A database that cannot be
snapshotted (locked, root-owned, not really SQLite) falls back to a raw copy and
is listed under "Databases copied RAW" — visible, never silent.

**Excludes are defaults, not cron flags**, and understand a gitignore-style
`!pattern` re-include so `nextcloud/www/**` can be dropped while
`nextcloud/www/nextcloud/config/**` — the instanceid, secret and DB password —
is kept. Directory pruning respects re-includes; pruning `nextcloud/www/` would
otherwise discard the re-include before the walk reached it.

**The cron line passes `--ok-codes 0`.** With discovery there is no benign exit
1 left, so any 1 is worth a human.

## Consequences

Measured on the first run of the new code:

|                           | before     | after                                  |
| ------------------------- | ---------- | -------------------------------------- |
| service directories       | 8          | 23                                     |
| files                     | 20,207     | 2,299                                  |
| raw bytes archived        | 948 MB     | 4,207 MB                               |
| compressed                | 437 MB     | 1,027 MB                               |
| databases, WAL-consistent | 0          | 27                                     |
| exit code                 | 1, nightly | 0                                      |
| files skipped for size    | 9, unnamed | 3, named (two binaries and a jobs log) |

Fewer files, four times the state. Retention stays at 5, so roughly 5 GB on a
9.1 TB disk. The run took 491 s against 59 s before — snapshotting 27 databases,
`lidarr.db` at 2.3 GB and `jellyfin.db` at 1.3 GB among them. At 01:00 that
collides with nothing.

## Verification is by effect, not by exit code

A backup that runs and exits 0 is exactly what this stack had for months. So
`scripts/check_backup_contents.py` opens the newest archive in
`make verify-runtime` and asserts thirteen required members are present **and
non-empty** — every `*arr` database, `jellyfin.db`, `tinyauth.db`,
`cleanuparr.db`, `qui`'s database, `qBittorrent.conf`, `slskd.yml`, the
proxy-confs, and Nextcloud's `config.php`. A stale archive (>36 h) fails too.

`make check` holds the two halves it can see: `config_backup.py` must not regain
a hard-coded list, and the cron line must not pin `--services` or drop
`--ok-codes 0`.

## What this does not cover

`playlist-generator-db` and `streamystats-db` are skipped on purpose. They are
Postgres, and a correct backup is a `pg_dump`, not a tar. They hold derived data
(embeddings, playback stats) that can be rebuilt, so this is a known gap rather
than an oversight — if either ever holds something original, it needs its own
dump job, not an entry in this archive.

Off-box replication is still `scripts/offsite_backup.sh`, still
`PENDING-DESTINATION` until `RESTIC_REPOSITORY` is set. Five copies on one disk
in the same box is not a backup strategy; it is a restore convenience.

## See also

- ADR-0017 — Cleanuparr is armed; its rules are in `cleanuparr.db`
- ADR-0036 — tinyauth's DB migration is one-way; this archive _is_ the rollback
- `.claude/skills/verifying-by-effect` — why the contents check exists
- `.claude/skills/hunting-silent-failure` — exit 1 inside `--ok-codes 0,1`
