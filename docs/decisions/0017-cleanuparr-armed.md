# ADR-0017 — Cleanuparr is armed; three of its modules must stay off

**Date:** 2026-09-01
**Status:** accepted
**Full reasoning:** `docs/cleanuparr-configuration.md`

## What it is

Cleanuparr is a **deletion engine** wired to qBittorrent and to Sonarr/Radarr.
`dryRun` is **`false`**. Treat every change to its config as a change to
something that deletes media.

## Armed

- **Malware Blocker** — local blacklist at
  `${CONFIG_DIRECTORY}/cleanuparr/blacklist.txt`, deliberately a **file**, not a
  URL.
- **Queue Cleaner** — failed-import 3 strikes, metadata 6, stall 12, slow 24,
  all bounded to 0–99% completion so a _completed_ torrent can never be struck
  as "slow".
- **Download Cleaner** — hourly, one seeding rule matching qBittorrent's own
  goals (0.6 ratio / 34 h).

## Off on purpose — do not enable casually

- **Unlinked Downloads** — looks safe now that hardlinks work (ADR-0002), but
  the pre-restructure backlog is still `nlink=1` (**425 files vs 1
  hardlinked**), so it would flag the whole download tree.
- **Dead Torrents** — tried in dry-run and **rejected**. "No seeders" means _no
  other seeder_, which is true of **25 of 59** completed torrents here. It
  struck _Friends_, _Fargo_ and _Planet Earth III_ — all healthy and mid-goal.
- **Orphaned Files**, **Seeker**, **Blacklist Sync** — off.

## Two hard rules

**Lidarr must never be enabled in any Cleanuparr module.** Lidarr's only
download client is slskd, which Cleanuparr cannot see.

**`failedImport.skipIfNotFoundInClient` must stay `true`.** That flag is what
stops Cleanuparr striking a queue it has no view of.

## Why nothing tags torrents from the \*arr side

Sonarr and Radarr **cannot tag torrents at all** — no tag field exists on their
qBittorrent client config. The post-import category is empty **on purpose**:
with `auto_tmm=true` a category change physically relocates the data.

## Compose-level note

`cleanuparr` waits on `qbittorrent: service_healthy` (not `service_started`) —
a deletion engine must not start acting against a client that is still coming
up. That edge crosses module files, which is one of the reasons the stack stays
a single Compose project (ADR-0000).
