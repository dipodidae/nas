# ADR-0003 — Lidarr's `/data` mount is staged but NOT in use

**Date:** 2026-09-01
**Status:** accepted (unfinished work, deliberately parked)
**Background:** `docs/arr-qbittorrent-pollution.md`, section
"Lidarr — attempted, broke it, rolled back"

## What happened

The same bulk-editor call that Sonarr and Radarr handled correctly —
`PUT /api/v1/artist/editor` with `rootFolderPath` and `moveFiles: false` —
**emptied Lidarr's `TrackFiles` table: 150,187 rows → 0.**

`Artists` was repathed correctly and `Albums`/`Tracks` survived, but every
track-file registration was gone, so Lidarr reported 0.00 TiB on disk while
still claiming a `trackFileCount` from stale statistics.

**Detection.** The verification pass showed Lidarr at `size=0.00 TiB` against
1.52 TiB before. Confirmed against the pre-change DB backup rather than assumed:
backup 150,187 rows / 1.52 TiB, live 0 rows / 0.00 TiB.

**Rollback.** Lidarr stopped, the broken DB preserved at
`/mnt/drive/backups/pre-cleanup-1788287701/broken-after-repath/`, `lidarr.db`
restored from the pre-change backup (integrity check `ok` before use).
Post-restore: 3334 artists, 1.52 TiB, 147,528 track files, root folder back to
`/music`, health clean. Cost: ~45 minutes of Lidarr activity lost between the
20:35 backup and the restore.

## Decision

Keep the `${SHARE_DIRECTORY}:/data` mount in place — it is harmless and ready
for a safer retry — but **Lidarr's root folder is still `/music`, so Lidarr
still copies rather than hardlinks.** That is the status quo, not a regression,
and it is unfinished work.

## A retry MUST NOT use `PUT /api/v1/artist/editor`

This is the whole point of the ADR. Two acceptable paths instead:

1. Add the root folder, move a **single** artist, verify `TrackFiles` is intact
   for that artist, and only then proceed.
2. Skip the editor endpoint entirely: rewrite `Artists.Path` **and**
   `TrackFiles.Path` directly with Lidarr stopped. More invasive, but with
   predictable semantics.

Either way, take a DB backup first and verify the row count after, not just the
UI's reported size.

## Why Lidarr differs — a guess, not a finding

Lidarr is a much older fork of the *arr codebase and its editor endpoint likely
treats a root-folder change as a move, unlinking track files when `moveFiles` is
false rather than rewriting paths in place. **Lidarr's source was not read to
confirm this.** It should be verified before any retry.
