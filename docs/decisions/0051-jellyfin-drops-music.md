# ADR-0051 — Jellyfin drops music, and a deleted library does not delete its items

**Date:** 2026-09-16
**Status:** accepted
**Extends:** ADR-0044, ADR-0049, ADR-0050 (Navidrome), ADR-0016 (Jellyfin paths), ADR-0047 (backup size)

## Context

Navidrome now owns music end to end: it serves it over Subsonic to every phone
client (ADR-0044), Lidarr triggers its scans on import (ADR-0049), and the
playlists live there (ADR-0050). Jellyfin's music library had become a second
indexer over the same 237,560-file tree, earning its keep nowhere.

The cost was not subtle. Jellyfin's item counts on the morning of 2026-09-16:

| Type          |   Count |
| ------------- | ------: |
| `Audio`       | 168,128 |
| `MusicAlbum`  |  17,555 |
| `MusicArtist` |   3,510 |
| `Episode`     |   1,789 |
| `Series`      |      43 |
| `Movie`       |      41 |

**Music was 99% of everything Jellyfin knew about.** Every query, every index,
every library validation and every nightly backup carried it.

## Decisions

### 1. Jellyfin serves movies and series, and one day books. Nothing else.

The music library is removed. `${SHARE_DIRECTORY}:/data/movies` stays mounted
exactly as ADR-0016 requires — movies and series depend on that path being what
it is — but nothing indexes `/data/movies/music` any more.

### 2. Removing a library does NOT remove its items, and a scan will not reap them

This is the part that cost the time, and it is the reason the first attempt
appeared to do nothing.

Deleting the library removed it from `/Library/VirtualFolders` immediately.
**168,128 audio items kept answering `/Items`.** A full `Validating media
library` pass then ran for six minutes at 1133% CPU and logged **eight**
`Removing item` lines in total — because validation walks libraries, and these
items no longer sat under one. They were unreachable by the very mechanism that
would have cleaned them.

What actually cleared them was Jellyfin's own `CleanDatabaseScheduledTask`,
which runs as post-scan task 8/8. The orphaned `MusicGenre` items (1,427) and
the 37 playlists survived even that and had to be deleted by id.

The tell, if this recurs: `/Library/VirtualFolders` shows no music library while
`/Items?IncludeItemTypes=Audio` still returns a count. The library being gone
does not imply the items are.

### 3. SQLite does not shrink on delete — the database must be VACUUMed

With every music item gone, `jellyfin.db` was still **1.426 GB, of which 94.2%
was free pages**. 348,041 pages holding 20,180 pages of live data. SQLite keeps
a file at its high-water mark forever unless told otherwise, so the cost of the
music outlived the music.

```
VACUUM + ANALYZE, jellyfin stopped:
  1.426 GB / 348,041 pages / 94.2% free  ->  0.069 GB / 16,748 pages   [0.8 s]
  reclaimed 1.357 GB (95.2%)
```

This is a **performance** decision as much as a size one: before the VACUUM
every index scan walked a file that was mostly holes.

### 4. The whole config tree, measured

| Stage                                 | Size        |
| ------------------------------------- | ----------- |
| Before (with music metadata)          | **8.40 GB** |
| After Jellyfin's own metadata cleanup | 4.96 GB     |
| After VACUUM + orphan prune           | **2.63 GB** |

What the prune removed, each measured rather than estimated:

- `jellyfin.db` 1.426 GB → 69 MB (VACUUM)
- `jellyfin.db-wal` 134 MB → 0 (checkpointed by a clean stop — CAP_KILL, ADR-0041)
- 1,720 orphaned `metadata/library` item directories (144 MB)
- 3,795 orphaned `metadata/People` directories (7 MB)
- `metadata/artists` (21 MB) and `metadata/MusicGenre` (528 KB)
- `cache/images` (660 MB — regenerable), `cache/audiodb-artist`,
  `cache/audiodb-album`, `cache/fanart-music` (16 MB, music-only providers)

Orphans were identified by joining the directory names against `BaseItems.Id`
and `Peoples.Name` in the live database, not by pattern-matching names.

### 5. Backups get smaller for free, and two Jellyfin-specific trees are gone

ADR-0047 already excludes `jellyfin/data/metadata/**` from the config archive,
so the artwork was never in a backup. `jellyfin.db` **was**, at 1.426 GB per
archive. Every future archive is therefore ~1.36 GB smaller with no change to
`config_backup.py`.

Deleted: `/mnt/drive/backups/jellyfin-pre-12` (477 MB, the ADR-0035 rollback for
a version two majors behind) and `/mnt/drive/backups/jellyfin-heap-dumps`
(99 MB, artifacts of a closed OOM investigation).

### 6. Everything that pointed at the music library is retired, not left to rot

Left installed, none of these would have errored. Jellyfin answers `204` to a
library refresh for a path under no library (ADR-0033's favourite failure shape),
so they would simply have done nothing, forever, while reporting success.

Cron, 34 jobs → 30: `lidarr-jellyfin-bridge` (every 5 min), `jellyfin-scan-music`,
`jellyfin-image-backfill`, `music-library-sweep`.

Scripts deleted (2,786 lines): `lidarr_jellyfin_bridge.py`,
`music_library_sweep.py`, `jellyfin_image_backfill.py`, `jellyfin_nfo_dates.py`,
`check-lidarr-bridge-root.py`, `check-lidarr-jellyfin-notification.py`, and
their tests. Recover from git history if music ever returns.

Lidarr's `MediaBrowser` connection is gone; its `Subsonic` one (ADR-0049) is the
only media-server connection it has now.

`make verify-runtime` loses the two bridge assertions and gains
`scripts/check-jellyfin-no-music.py`.

### 7. The new assertion is an inverse one

`check-jellyfin-no-music.py` fails when music comes **back**. Adding a library is
two clicks in Jellyfin's UI, `make check` cannot see runtime library config, and
the only symptom is a database quietly growing again. It asserts three things:
no library with `CollectionType == "music"`; no `Audio`/`MusicAlbum`/`MusicArtist`
items (not redundant — see decision 2); and that `jellyfin.db` is not more than
50% free pages above 200 MB, which catches the next large delete that nobody
reclaimed.

All four of its failure paths were driven red and restored: no API key (`2`),
Jellyfin unreachable (`2`), a synthetic 287 MB database at 100% free (`1`), and
a real empty music library added and removed again (`1`).

## Consequences

- Jellyfin holds 16,883 items instead of 190,000, in a 69 MB database.
- Music playback moves entirely to Navidrome/Subsonic. Jellyfin clients lose it.
- `docs/jellyfin-playback-audit.md` and `docs/music-pipeline-integration.md`
  describe a Lidarr→Jellyfin path that no longer exists; both are historical.
- The `nas-music-pipeline` skill's "three namespaces" is now two: Lidarr's
  `/data/music` and slskd's `/music`. Jellyfin's `/data/movies/music` is retired.
- If books arrive, they are a new library under the same mount, and nothing here
  needs revisiting.
