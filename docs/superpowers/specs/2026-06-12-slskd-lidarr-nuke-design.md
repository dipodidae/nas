# slskd↔Lidarr clean-slate "nuke" button — design

**Date:** 2026-06-12
**Status:** Approved (design), pending implementation
**Script:** `scripts/slskd_lidarr_nuke.py`
**Tests:** `scripts/tests/test_slskd_lidarr_nuke.py`

## Problem

The slskd↔Lidarr music pipeline accumulates stale state: wedged Lidarr queue
rows, dead/queued slskd transfers, terminal slskd records that never clear, and
leftover dirs in the slskd completed-downloads folder. The existing reapers
(`lidarr_stuck_download_reaper.py`, `slskd_cleanup.py`, `slskd_complete_sweep.py`,
`lidarr_queue_unstick.py`) are all deliberately **gated and throttled** — age
gates, `--max-actions`, size-match safety — so they drain slowly and safely on
cron.

This tool is the deliberate opposite: a **single aggressive "clean slate"
button** the operator fires on demand to reset the whole pipeline to zero. It is
idempotent — if nothing is in flight it is a no-op.

Lidarr on this host is configured with **slskd (via Tubifarry) as its only
download client** — no qBittorrent integration — so the entire Lidarr queue is
slskd-sourced and can be wiped wholesale without a per-client filter.

## Goal

One command that:

1. Gracefully tears down the **entire Lidarr queue** so Lidarr stays internally
   consistent (no orphaned grabs) and every album stays monitored/missing for
   later re-grab.
2. **Fully wipes** slskd's transfer manager — cancels all active/queued
   downloads and clears all terminal records.
3. **Sweeps** the slskd completed-downloads folder of everything except dirs a
   live Lidarr import is touching right now.

…all GRACEFULLY: Lidarr-aware teardown first, slskd mop-up second, disk last.

## Design decisions (from brainstorming)

| Decision                  | Choice                                                                                                                                                                                                                         |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Lidarr fate of nuked rows | `removeFromClient=true&blocklist=true&skipRedownload=true` — graceful client-side cancel, blocklist only the _dead release_ (album stays monitored), **no auto re-search** (external automation / lidarr-bulk re-kicks later). |
| slskd scope               | Full wipe: cancel all active/queued transfers **and** clear all terminal records.                                                                                                                                              |
| Folder sweep              | Empty the whole completed/slskd folder except dirs an active Lidarr import references.                                                                                                                                         |
| Safety gate               | Acts by default; `--dry-run` previews. (Matches repo convention.)                                                                                                                                                              |
| Queue blast radius        | Entire Lidarr queue — moot, since slskd is Lidarr's only client.                                                                                                                                                               |

## Architecture

Three phases, ordered so each phase's side effects make the next phase's
observations accurate.

### Phase 1 — Lidarr queue teardown (graceful, first)

- `GET /api/v1/queue?pageSize=1000&includeUnknownArtistItems=true` → all rows,
  every state.
- Delete via bulk endpoint `DELETE /api/v1/queue/bulk` with body
  `{"ids": [...]}` and query
  `removeFromClient=true&blocklist=true&skipRedownload=true`.
  - Fallback: per-id `DELETE /api/v1/queue/{id}?<same params>` if bulk returns
    an error status.
- **Why graceful:** Lidarr routes the cancel through Tubifarry, which tears down
  the matching slskd transfer — so the transfer is removed _with Lidarr's
  knowledge_, nothing is orphaned. `blocklist=true` blocklists the specific dead
  release (not the album); the album returns to monitored/missing.
  `skipRedownload=true` suppresses an immediate re-search storm.

### Phase 2 — slskd full wipe (mop-up)

- `GET /api/v0/transfers/downloads`.
- For every transfer **not** in a terminal `Completed,*` state (i.e. still
  `Queued, *` / `InProgress` / `Initializing` / etc.): cancel + remove via
  `DELETE /api/v0/transfers/downloads/{user}/{id}?remove=true`. `404` =
  already gone = success (Tubifarry may have beaten us to it in Phase 1).
- Clear all terminal records via
  `DELETE /api/v0/transfers/downloads/all/completed`; per-record fallback for
  any stragglers if the bulk endpoint is unavailable / returns error.
- Result: empty transfer manager.

### Phase 3 — completed-folder sweep (disk, last)

- Re-fetch the now-drained Lidarr queue and build a **spare set** of path
  basenames any active import still references (`outputPath`,
  `downloadForcedClientPath`, `title` basenames — same extraction
  `slskd_complete_sweep.py` uses). After Phase 1 this is normally empty, but a
  row that resisted deletion or arrived mid-run is protected.
- Walk the **direct children** of `${SLSKD_COMPLETE_DIR}` (default
  `/mnt/drive/downloads/complete/slskd`). `shutil.rmtree` every child dir whose
  basename is **not** in the spare set — both already-imported dups (hardlinks,
  safe to drop) and orphaned leftovers.
- **Containment guard:** refuse (exit 2) if any resolved target is not a direct
  child of the resolved complete dir — never escape the folder.
- Report GB freed.

## Module shape (AGENTS.md contract)

Pure logic separated from side effects for testability; side effects only in
`main()`. Mirrors `lidarr_stuck_download_reaper.py` / `slskd_complete_sweep.py`.

Pure / near-pure functions:

- `plan_lidarr_nuke(records) -> list[int]` — queue ids to delete.
- `collect_slskd_transfers(downloads) -> (active: list[Transfer], terminal_count: int)`
  — partition transfers into active-to-cancel vs terminal.
- `spare_basenames(records) -> set[str]` — basenames an active import touches.
- `plan_folder_sweep(complete_dir, spare) -> list[Path]` — direct-child dirs to
  delete (with containment check).

Side-effecting helpers (thin wrappers over `urllib`, like the existing scripts):

- `fetch_lidarr_queue`, `bulk_delete_lidarr`, `delete_lidarr_item`
- `fetch_slskd_downloads`, `cancel_slskd_transfer`, `clear_slskd_completed`
- `_request` (shared, copied from the existing reaper pattern)

## CLI

```
python scripts/slskd_lidarr_nuke.py            # ACT: full clean slate
python scripts/slskd_lidarr_nuke.py --dry-run  # preview plan, exit 0
python scripts/slskd_lidarr_nuke.py --skip-folder-sweep
python scripts/slskd_lidarr_nuke.py --skip-lidarr --skip-slskd  # folder only
```

Flags: `--dry-run`, `--skip-lidarr`, `--skip-slskd`, `--skip-folder-sweep`,
`--slskd-complete-dir` (override env). A loud one-line banner prints the full
plan (queue rows, transfers, dirs + GB) before any destructive action.

## Environment

| Var                  | Required | Default                               |
| -------------------- | -------- | ------------------------------------- |
| `API_KEY_LIDARR`     | yes      | —                                     |
| `API_KEY_SLSKD`      | yes      | —                                     |
| `LIDARR_HOST`        | no       | `http://localhost:8686`               |
| `SLSKD_HOST`         | no       | `http://localhost:5030`               |
| `SLSKD_COMPLETE_DIR` | no       | `/mnt/drive/downloads/complete/slskd` |

dotenv fallback when keys aren't already in the environment (same idiom as the
existing scripts).

## Exit codes

- `0` — success, or dry-run, or nothing to do.
- `1` — partial: some deletes/cancels/rmtrees failed (details on stderr).
- `2` — fatal: config missing, slskd/Lidarr unreachable, or containment violation.

## Testing

`scripts/tests/test_slskd_lidarr_nuke.py` (pytest, pure functions — no network):

- `plan_lidarr_nuke`: empty queue → `[]`; mixed-state queue → all ids; ids
  de-duplicated / well-typed.
- `collect_slskd_transfers`: terminal `Completed,*` excluded from active list;
  active states collected; empty payload → no work.
- `spare_basenames`: extracts basenames from each path field; ignores empties.
- `plan_folder_sweep`: spares dirs in the spare set; selects the rest; raises /
  refuses on a target outside the complete dir (containment).

## Out of scope

- No cron entry — this is a manual, on-demand button. (Operator may wire one
  later, but it is not part of this work.)
- No re-search trigger — by design; external automation (lidarr-bulk) handles
  re-kicking searches.
- Does not touch qBittorrent (Lidarr has no qB client; the qB-shared
  `/downloads/incomplete` is never walked).

## Docs to update on implementation

- `scripts/README.md` — add the script with flags / exit codes / workflow.
- `AGENTS.md` — note the new script if it introduces any new env var (it reuses
  existing ones, so likely just the script list).
