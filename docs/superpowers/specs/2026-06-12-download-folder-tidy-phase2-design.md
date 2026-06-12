# Download-folder tidy — Phase 2 (incomplete-orphan sweep + residue cleanup) — design

**Date:** 2026-06-12
**Status:** Approved (design), pending implementation
**Scope:** Phase 2 of the download-topology tidy. Phase 1 (qBittorrent Auto TMM
+ slskd incomplete split) is done and merged. See
`docs/superpowers/specs/2026-06-12-download-folder-tidy-phase1-design.md`.

## Problem

After Phase 1, three pieces of residue remain (all verified live on 2026-06-12):

1. **148 orphaned flat dirs in `incomplete/` root** — legacy Soulseek
   in-progress album folders from before slskd was given its own
   `incomplete/slskd` dir. 0 are referenced by any live qBittorrent torrent
   (qBit's temp fully moved into `incomplete/qbittorrent/`), and all slskd
   transfers were nuked — so they are dead leftovers. They will also keep
   accumulating in `incomplete/slskd/` whenever a Soulseek transfer is cancelled
   or dies mid-download.
2. **3 unused cruft qBittorrent categories** — `movies-radarr`, `music-lidarr`,
   `tv-sonarr`, each with empty save paths and **0 torrents**.
3. **Stale duplicate config tree `/mnt/drive/.docker-config`** — 2.5 GB, frozen
   since 2026-05-23, NOT mounted by any container (the live tree is
   `${CONFIG_DIRECTORY}=/home/tom/nas/.docker-config`, 52 GB).

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| incomplete cleanup | **Reusable gated sweeper** (not a one-time script) — cleans the 148 legacy flat dirs now and future `incomplete/slskd/` orphans. |
| Residue items folded in | **Both** — delete the 3 cruft categories AND the stale config tree. |
| Safety default | Acts by default; `--dry-run` previews (repo convention). |
| qBittorrent's incomplete zone | **Never touched** — qBit owns `incomplete/qbittorrent/`. |

## Architecture

### Component 1 — `scripts/slskd_incomplete_sweep.py`

Reusable gated sweeper; mirrors `slskd_complete_sweep.py` (pure planner + side
effects in `main()`, stdlib `urllib`, dotenv fallback, exit 0/1/2, acts by
default with `--dry-run`).

**Sweep zones** (the two slskd-owned regions of `incomplete/`):
- Direct children of `INCOMPLETE_DIR` **except** the managed `qbittorrent` and
  `slskd` subdirs (the legacy flat dirs).
- Direct children of `INCOMPLETE_DIR/slskd` (current + future slskd orphans).

`INCOMPLETE_DIR/qbittorrent` is **never entered** — qBittorrent owns its temp;
deleting it would corrupt live torrents.

**Three gates** — a candidate dir is deleted only if it clears ALL three:
1. **Not referenced by an active slskd transfer.** `GET
   /api/v0/transfers/downloads`; protect any dir whose basename matches a
   transfer's directory trailing segment (same matching as `slskd_cleanup.py`).
2. **Not referenced by a live qBittorrent torrent.** Authenticate to the qBit
   WebUI API v2 (cookie jar; accept HTTP 200 **or 204** on login — qBit v5.2.1
   returns 204); `GET /api/v2/torrents/info`; protect any dir whose basename
   matches a torrent's `save_path` / `content_path` / `name` basename. This
   honors the CLAUDE.md rule that qBittorrent historically shared `incomplete/`.
3. **Age gate.** Only dirs whose `mtime` is older than `--min-age-hours`
   (default 24) — never reap a just-started download.

**Containment guard:** every delete target must resolve to a path inside the
resolved `INCOMPLETE_DIR` tree; the managed `qbittorrent` / `slskd` subdir roots
themselves are never deletable. Refuse (exit 2) on any escape.

**Degraded-safety rule:** if either the slskd or qBittorrent reference fetch
fails, the sweep **aborts (exit 2)** rather than deleting with an incomplete
protection set — deleting on a partial picture is the dangerous failure mode.

**CLI / behavior:**
- Acts by default; `--dry-run` prints the plan (dirs + GB, protected counts) and
  exits 0 without deleting.
- `--min-age-hours` (default 24), `--limit` (cap deletions per run, 0 =
  unlimited).
- Exit codes: `0` success / dry-run / nothing to do; `1` partial (some rmtrees
  failed); `2` fatal (config missing, slskd/qBit unreachable, containment
  violation).
- **Env:** `API_KEY_SLSKD`, `SLSKD_HOST` (default `http://localhost:5030`),
  `QBITTORRENT_USER`, `QBITTORRENT_PASS`, `QBITTORRENT_HOST` (default
  `http://localhost:8080`), `INCOMPLETE_DIR` (default
  `/mnt/drive/downloads/incomplete`).

**Pure function (unit-tested, no network):**
- `plan_incomplete_sweep(candidates, slskd_refs, qbt_refs, *, now, min_age_hours)
  -> list[Path]` — `candidates` is a list of `(Path, mtime)` for the sweep-zone
  dirs; `slskd_refs` / `qbt_refs` are `set[str]` of protected basenames. Returns
  dirs that are unprotected by both ref sets AND older than the age gate. Order
  preserved.
- Supporting pure helper `trailing_segment(path) -> str` (basename extraction
  handling `\\` and `/`), reused from the `slskd_cleanup.py` idiom.

The directory walk, ref-set building, and `shutil.rmtree` live in `main()` /
thin helpers (side effects), exactly like `slskd_complete_sweep.py`.

### Component 2 — one-time residue ops (controller-run live actions)

Documented as explicit plan steps; executed once by the controller, each
verified immediately before acting:

1. **Delete 3 cruft qBittorrent categories.** Re-confirm at runtime that
   `movies-radarr` / `music-lidarr` / `tv-sonarr` each have 0 torrents, then
   `POST /api/v2/torrents/removeCategories` with the three names. Idempotent
   (removing an absent category is a no-op).
2. **Delete stale `/mnt/drive/.docker-config`.** First assert via `docker
   inspect` across all running containers that **none** mount any path under
   `/mnt/drive/.docker-config`; only then `rm -rf` the 2.5 GB tree. Abort if any
   mount is found.

## Testing

`scripts/tests/test_slskd_incomplete_sweep.py` (pytest, offline):
- `plan_incomplete_sweep`: dir protected by slskd ref skipped; dir protected by
  qbt ref skipped; too-recent dir skipped; unprotected old dir selected; empty
  candidates → `[]`.
- `trailing_segment`: handles `/`, `\\`, trailing separators, bare name.

Plus standard gates: `ruff check scripts`, `pytest -q scripts/tests`,
`docker compose config`.

## Order of operations (runbook for the plan)

1. Build + test `slskd_incomplete_sweep.py`.
2. `slskd_incomplete_sweep.py --dry-run` → eyeball (expect ~148 legacy flat dirs
   eligible, qbittorrent/ & slskd/ subdir roots protected).
3. Run it for real → flat-root orphans cleared.
4. Delete the 3 cruft categories (after runtime 0-torrent re-check).
5. Verify no container mounts `/mnt/drive/.docker-config`, then `rm -rf` it.
6. Docs + pnpm wrappers; final CI-parity gate.

## Docs to update on implementation

- `scripts/README.md` — add `slskd_incomplete_sweep.py` entry.
- `AGENTS.md` — add to operational-scripts list (reuses existing env vars).
- `package.json` — `sweep:incomplete` / `sweep:incomplete:dry` wrappers.

## Out of scope

- Anything under `complete/` (Phase 1 done) or `incomplete/qbittorrent/`
  (qBit-owned).
- Compose volume-mount changes.
- A cron schedule for the sweeper (manual/on-demand for now; can be wired later).
