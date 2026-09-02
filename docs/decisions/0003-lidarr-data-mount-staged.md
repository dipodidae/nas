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

---

## Rehearsal, 2026-09-02 — option 2, and why option 1 could never have worked

Option 1 above ("move a **single** artist, verify `TrackFiles` is intact") was
planned in detail and then abandoned, because it cannot detect the failure it
exists to catch.

The plan sorted candidate artists ascending by track-file count, and **757 of
3,345 artists have zero track files**. The first artist moved was therefore
*certain* to be a zero-file artist, and its verification would have been
`0 == 0`. Not unlikely — guaranteed.

Worse, the check is backwards. `PUT /api/v1/artist/{id}?moveFiles=false`
updates `Artists.Path` and rewrites nothing else, by design. An unchanged
`trackFileCount` afterwards is evidence the file rows were **not** rewritten,
which is the failure, not the success. And the final step — deleting the
`/music` root folder while 150,300 rows still point at it — is the documented
route straight back to `150,187 → 0` by a different path.

The reason the API cannot do this at all: **`TrackFiles` has no inode and no
hash. The absolute path is the only handle Lidarr has on a file.**

```
sqlite> select sql from sqlite_master where name='TrackFiles';
CREATE TABLE "TrackFiles" (... "Path" TEXT NOT NULL UNIQUE, ...)
sqlite> select substr(Path,1,7), count(*) from TrackFiles group by 1;
/music/ | 150300
```

### The method actually used: an offline prefix rewrite

`/data` is a bind mount of `/mnt/drive` and `/music` is a bind mount of
`/mnt/drive/music`, so `/data/music/X` and `/music/X` are **literally the same
directory**. Nothing on disk has to move; this is a pure metadata change.
(That also rules out the Artist Editor's "Yes, move the files" — it would be a
rename onto itself.)

The premise was re-probed before anything else, inside the live container:

```
$ docker exec lidarr sh -c 'ln /downloads/.probe /music/.probe'
ln: failed to create hard link: Cross-device link      # today's problem
$ docker exec lidarr sh -c 'ln /data/downloads/.probe /data/music/.probe'
LINK OK                                                # the premise holds
```

`scripts/lidarr_repath_db.py` rewrites four columns in **one transaction**,
with verification inside that transaction so a failed check rolls back rather
than leaving half the rows on a dead root.

| Table.column | Rewritten | Why |
|---|---:|---|
| `RootFolders.Path` | 1 | the root itself |
| `Artists.Path` | 3,345 | artist folders |
| `TrackFiles.Path` | 150,300 | the only handle Lidarr has on a file |
| `MetadataFiles.RelativePath` | 14,958 | **absolute despite the column name** |

**`MetadataFiles` is the trap.** 14,958 of its 43,299 rows hold absolute
`/music/...` paths; 28,341 are genuinely relative (`artist.nfo`). A rewrite
covering only the obvious `Artists` and `TrackFiles` orphans every `.nfo`;
a blind rewrite corrupts the relative rows.

`History.SourceTitle` (605,171 rows), `History.Data`, `DownloadHistory.Data`
and `Commands.Body` are **deliberately left on `/music`**. They are audit text,
not state Lidarr resolves against, and rewriting 900k+ rows of history to make
old log lines read prettier is risk with no return. **Leftover `/music`
strings in History are expected and are not an incomplete migration.**

### Rehearsal result, on a full copy of the live database

A single-artist canary was replaced with a full-scale rehearsal: same 168,604
rows, same schema, same data, zero risk, because the live database was never
opened.

- 14/14 unit tests pass (path logic, idempotency, the `/musicvideos`
  shared-prefix trap, traversal rejection, the mixed absolute/relative
  `MetadataFiles` shape, row-count preservation, rollback on error).
- `apply_rewrite` changed exactly `1 / 3345 / 150300 / 14958` rows.
- Row counts preserved across all four tables: `1 / 3345 / 150300 / 43299`
  before and after.
- Zero rows left matching `/music`; the 28,341 relative `MetadataFiles` rows
  untouched; zero `/data/music/data/music` double prefixes.
- Root folder reads back `/data/music`.
- A second pass reports **0 eligible** — re-running after an interruption is a
  no-op, not a corruption.
- A random **1000/1000** sample of rewritten `TrackFiles.Path` values exists on
  disk under `/mnt/drive/music`.

The audit above was run independently of the tool that did the writing; the
script's own report was not accepted as the verification.

One incidental finding worth keeping: the backup guard refused a first attempt
because the rehearsal's dry run had opened the copy read-write, so SQLite
checkpointed and **removed** the `-wal`/`-shm` files. The backup must be a
separate directory taken before any run touches the database.
