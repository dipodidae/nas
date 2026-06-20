# Album-art backfill via sacad — design

**Date:** 2026-06-20
**Status:** approved
**Author:** brainstorming session

## Problem

The music library at `$SHARE_DIRECTORY/music` (~2000 artists, ~7200 album
folders) is missing external album covers for a few hundred albums. Lidarr's
metadata consumer writes `folder.jpg` and 6625/7200 album folders already have
one, but the remainder (and every future Lidarr import that Lidarr fails to
art) have no cover for Jellyfin to display.

We want a hands-off, cronned job that fills the gaps using
[sacad](https://github.com/desbma/sacad) (Smart Automatic Cover Art
Downloader), which fetches covers from multiple online sources and writes one
image file per album folder.

## Scope

- **In scope:** download a missing external cover image (`folder.jpg`) into each
  album folder that lacks one, on a weekly schedule.
- **Out of scope:** embedding artwork into audio tags (sacad does not do this);
  replacing/upgrading existing covers; any change to Lidarr's own metadata
  behaviour.

## Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Where art lands | External `folder.jpg` per album folder | Matches the 6625 existing covers; what Jellyfin reads. sacad only writes external files. |
| Cover filename | `folder.jpg` | Existing library convention. |
| Target size | `1000` px | Good quality for Jellyfin without bloating the library. |
| Install method | `pip install` into the existing `.venv`, pinned in `scripts/requirements.txt` | Matches the repo's host-side wrapper pattern (cf. `replaygain.py`). sacad 2.8.3 is verified to resolve cleanly on Python 3.14 with prebuilt cp314 wheels — no source builds. |
| Frequency | Weekly, Sunday 04:45 | sacad_r is incremental (skips folders that already have the target file), so real work is only the few hundred gaps + new Lidarr imports. Gentle on cover-art APIs. |

## Architecture

Host-side Python wrapper around the `sacad_r` CLI, structured identically to
`scripts/replaygain.py` (the established pattern for "wrap an external media CLI
with a dry-run-default, env-driven, exit-code-contracted script").

### Component: `scripts/album_art.py`

- **Music root resolution:** `$SHARE_DIRECTORY/music` (default
  `/mnt/drive/music`), overridable with `--music-dir`.
- **Dry-run is the default.** A bare `python scripts/album_art.py` walks the tree
  and prints a plan — total album dirs, how many already have `folder.jpg`, how
  many gaps remain, a sample of gap paths — and invokes nothing. `--apply` is
  required to actually fetch.
- **Apply mode:** shells out to `sacad_r <music-dir> <size> <filename>`. sacad_r
  walks the library recursively and, for every album directory that lacks
  `<filename>`, downloads the best-matching cover at the closest available size.
  It natively skips folders that already have the target file, so re-runs are
  cheap and idempotent.
- **Flags:**
  - `--music-dir PATH` — override the music root.
  - `--size N` — target cover size in px (default `1000`).
  - `--filename NAME` — cover filename (default `folder.jpg`).
  - `--apply` — actually run sacad_r (otherwise dry-run).
- **Prerequisite check:** if `sacad_r` is not importable/on PATH, exit `2` with an
  actionable message (`pip install sacad` / `pnpm py:deps`).
- **Logging:** plain stdout; the cron redirects to `logs/album_art.log`.

### Exit codes (repo contract)

| Code | Meaning |
|---|---|
| 0 | success, or dry-run, or nothing to do |
| 1 | partial — sacad_r reported failure on one or more dirs |
| 2 | fatal — sacad missing, music dir missing, unexpected error |

Side effects (subprocess invocation, filesystem) are centralized in `main()`;
plan computation and arg parsing are pure/testable.

## Scheduling

One crontab entry, shaped like the existing weekly jobs, with its own flock so a
long run can't overlap the next week's tick:

```cron
# Sunday 04:45 — backfill missing folder.jpg album covers (sacad, incremental:
# skips albums that already have one). Sits after the 04:30 post-update verifier,
# clear of the heavy hourly Tubifarry/slskd hygiene jobs.
45 4 * * 0 /usr/bin/flock -n /tmp/nas-album-art.lock /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/album_art.py --apply >> logs/album_art.log 2>&1"
```

## Testing

`scripts/tests/test_album_art.py`, following existing pytest patterns, with
`sacad_r` mocked (no network):

- music-dir resolution from `$SHARE_DIRECTORY` and from `--music-dir` override;
- dry-run (default) computes a plan and never invokes sacad_r;
- missing-sacad path exits `2`;
- exit-code mapping: sacad_r returns 0 → 0, non-zero → 1;
- `--apply` builds the correct `sacad_r` argv (music-dir, size, filename).

Keeps CI green (`ruff check scripts` + `pytest -q scripts/tests`).

## Docs

- `scripts/README.md` — add an `album_art.py` entry (purpose, flags, exit codes).
- `AGENTS.md` — mention in the scripts section; no new env var (reuses
  `SHARE_DIRECTORY`).

## Known limitation (intentional)

sacad has no negative cache: albums for which *no* source has art will be
re-queried every weekly run. For a few hundred gaps this is negligible, so no
"tried-and-failed" skiplist is built unless API hammering is later observed.
