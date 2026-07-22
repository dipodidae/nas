# Album-art overwrite-once — design

**Date:** 2026-07-22
**Status:** Approved, pending implementation
**Touches:** `scripts/album_art.py`, the weekly `album_art.py` cron, `scripts/tests/`, `scripts/README.md`

## Problem

`scripts/album_art.py` (weekly cron, Sun 04:45) only *fills gaps* — it downloads
`folder.jpg` for album folders that have none. A lot of the art that Lidarr /
embedded tags already wrote is low quality. We want to **overwrite** that art
with a better sacad-sourced cover, but only **once per folder**: after a folder
has had its overwrite, consecutive cron runs must leave it alone.

The existing `--ignore-existing` flag (`sacad_r -i`) overwrites *every* album on
*every* run — churning the whole library weekly — so it cannot be used as the
cron default. `sacad_r`'s only native skip logic is "does the cover file
exist," which is useless once we start overwriting (the file always exists
afterward). We need our own per-folder "already done" memory.

## Decisions

1. **Scope of "once": per-folder, ongoing.** Each album folder gets exactly one
   sacad overwrite pass, ever. New albums Lidarr adds later arrive un-processed
   and get their one pass on the next cron run. Nothing is churned repeatedly.
2. **Marker: hidden sidecar file** `.album_art_done` written into each album
   folder once sacad has blessed its cover. Travels with the folder on
   rename/move; independent of any central state file. Its presence means
   "leave this folder alone forever."
3. **No-source safety: never blank a cover.** Overwrite is driven per-folder
   with `sacad_r <dir> <size> <filename> -i`. `-i` overwrites when a source is
   found; sacad leaves the existing file untouched when no source has art. A
   folder that currently has (bad) art can never end up with none.
4. **Per-run limit** (`--limit N`, default 300) so the first pass over the whole
   library drains across several weekly runs instead of one multi-hour marathon.

## Behaviour

A new opt-in flag **`--overwrite-once`** selects the new logic. Without it,
`--apply` keeps its current cheap tree-wide gap-fill behaviour, and `--dry-run`
(the default) still works. The cron switches to `--apply --overwrite-once`.

### Per-run logic (`--apply --overwrite-once`)

1. Discover album dirs (existing `discover_album_dirs`).
2. Partition into **marked** (contains `.album_art_done`) → skip entirely, and
   **unmarked**.
3. Take the first `--limit` unmarked dirs (sorted deterministically). For each:
   run `sacad_r <dir> <size> <filename> -i`.
4. **After the attempt, write `.album_art_done` into the folder iff a cover file
   now exists there.**

Rule 4 unifies overwrite and gap-fill:

| Folder before        | sacad result | After                                        |
| -------------------- | ------------ | -------------------------------------------- |
| bad art              | found new    | overwritten → **marked** (never retried)     |
| bad art              | nothing      | old art kept → **marked** (attempt spent)    |
| no art (gap)         | found        | filled → **marked**                          |
| no art (gap)         | nothing      | still empty → **unmarked** → retried next run |

Genuine unfillable gaps stay unmarked and keep getting retried exactly as
today. Everything else gets one overwrite then is frozen.

### Dry-run reporting

`--dry-run --overwrite-once` reports, without shelling out:

- total album dirs found
- marked (will skip)
- unmarked *with* an existing cover (will be overwritten, capped at `--limit`)
- unmarked *without* a cover (gap fill)
- how many exceed `--limit` and defer to a later run
- a sample of folders that would be overwritten

## Implementation notes

- New pure helpers (keep `main()` thin, testable):
  - `dir_is_marked(dir, marker_name) -> bool`
  - `partition_by_marker(dirs, marker_name) -> (marked, unmarked)`
  - `select_batch(unmarked, limit) -> (batch, deferred)`
  - a summary builder for the overwrite-once dry-run (parallel to
    `summarize_plan`).
- Overwrite execution: a `main()`-level loop calling `subprocess.run` per folder
  with a per-folder `build_sacad_cmd`-style command (reuse/extend
  `build_sacad_cmd` to take a target dir and force `-i`). After each call, check
  `(dir / cover_filename).exists()` and write the marker if true.
- Marker constant: `DEFAULT_MARKER_FILENAME = ".album_art_done"`, with a
  `--marker` override for symmetry with `--filename`. Marker file content: a
  single line (ISO date + `size`/`filename` used) for forensics — but presence
  is all that matters.
- Exit codes unchanged: 0 success / 1 partial (any per-folder `sacad_r`
  non-zero) / 2 fatal. A per-folder non-zero does **not** write the marker only
  if it also left no cover; if a cover exists we still mark (attempt spent).
  Track whether any folder failed to downgrade the overall exit to 1.
- `discover_album_dirs` must not treat the marker as audio (it isn't) — no
  change needed, but confirm marker files never get counted as album content.

## Testing

Extend `scripts/tests/test_album_art.py` (or create if absent):

- `partition_by_marker` splits correctly given a fake tree.
- `select_batch` respects the limit and returns the deferred remainder.
- overwrite-once dry-run summary counts marked / overwrite / gap / deferred.
- marker is written when a cover exists post-attempt; **not** written when the
  folder still has no cover (gap that sacad couldn't fill).
- marked folders are skipped (no `sacad_r` invoked for them) — assert via a
  mocked `subprocess.run`.
- `--limit` bounds the number of `subprocess.run` calls.
- backward compat: plain `--apply` (no `--overwrite-once`) still builds the
  tree-wide command and ignores markers.

Run: `. .venv/bin/activate && pytest -q scripts/tests/test_album_art.py`

## Cron change

Current:

```
45 4 * * 0 /usr/bin/flock -n /tmp/nas-album-art.lock /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/album_art.py --apply >> logs/album_art.log 2>&1"
```

New (add `--overwrite-once`; `--limit 300` is the default so it need not be
explicit, but state it for clarity):

```
45 4 * * 0 /usr/bin/flock -n /tmp/nas-album-art.lock /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/album_art.py --apply --overwrite-once --limit 300 >> logs/album_art.log 2>&1"
```

flock `-n` already prevents overlapping runs, so a long batch can't collide with
the next week's trigger.

## Docs

- Update the `album_art.py` module docstring (usage block + behaviour).
- Update `scripts/README.md` album-art section with the new flag, the marker
  file, and the drip semantics.
- Update `AGENTS.md` only if a new env var were introduced (none is).

## Out of scope (YAGNI)

- Judging art *quality* — we assume all pre-existing non-sacad art is worth
  replacing once; we don't score covers.
- Re-overwriting when a *better* source appears later (marker is terminal).
- Central manifest / DB of processed folders (sidecar chosen instead).
