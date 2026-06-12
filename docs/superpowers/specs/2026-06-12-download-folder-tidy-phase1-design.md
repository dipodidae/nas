# Download-folder tidy — Phase 1 (qBittorrent TMM + slskd incomplete split) — design

**Date:** 2026-06-12
**Status:** Approved (design), pending implementation
**Scope:** Phase 1 of a larger "tidy the download topology" effort. Residue
cleanup of `complete/manual/` and orphaned `incomplete/` dirs is **Phase 2** (a
separate design, not covered here).

## Problem (evidence-backed)

Every service mounts the whole `${SHARE_DIRECTORY}/downloads` as `/downloads`,
so all download mess lives in one shared tree. Three root causes, all verified
against the **live** services (not config files — there is a stale duplicate
config tree; see Side-findings):

1. **qBittorrent Auto TMM is OFF.** Live prefs: `auto_tmm_enabled=False`,
   `category_changed_tmm_enabled=False`, `save_path_changed_tmm_enabled=False`.
   The *arr apps tag categories correctly (Radarr→`arr-radarr`,
   Sonarr→`arr-sonarr`) and those categories have correct save paths
   (`/downloads/complete/{radarr,sonarr}`), but with TMM off the category never
   drives the save path. All **107 live torrents** (95 `arr-sonarr`, 12
   `arr-radarr`) save to the global default `/downloads/complete/manual`
   (~1.4 TB) while `complete/{sonarr,radarr,lidarr}` sit empty.
2. **slskd and qBittorrent share one flat `incomplete/` dir.** Live
   `slskd.yml: directories.incomplete = /downloads/incomplete` is identical to
   qBittorrent's live `temp_path = /downloads/incomplete`. Soulseek in-progress
   album folders and torrent temp dirs intermingle (~9.9 GB), which is why
   `slskd_cleanup.py` is forbidden from ever sweeping `incomplete/`.
3. **Residue** accumulates in `manual/` and `incomplete/` because nothing
   categorizes or safely owns those folders. (Phase 2.)

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Existing 107 torrents | **Relocate only, keep seeding** — flip to Auto TMM so qBit moves them into category folders; never stop/remove. |
| Effort scope | **Phase 1 config fixes only.** Residue cleanup is a later design. |
| Incomplete layout | **Per-client subfolders:** `incomplete/qbittorrent` + `incomplete/slskd`. |
| Compose mounts | **Leave as-is** — *arr need broad `/downloads` visibility for imports. |
| Apply method | **Idempotent enforcement script + reviewed config edits** (matches `enforce_radarr_settings.py`). |
| Script default | **Acts by default; `--dry-run` previews** (repo convention). |

## Why relocate is safe

`SHARE_DIRECTORY=/mnt/drive` is a single ext4 mount. qBittorrent relocates a
torrent via `moveStorage`, which on the same filesystem is a rename — the inode
is unchanged, so the existing hardlinks from `series/` and `movies/` into
`complete/manual/` keep pointing at the same data. Seeding is uninterrupted and
no bytes are copied. Moving ~1.4 TB is therefore effectively instant.

## Architecture

### Component 1 — `scripts/qbittorrent_settings_enforce.py`

An idempotent enforcement script (mirrors `enforce_radarr_settings.py` style;
stdlib `urllib`, dotenv fallback, pure planning + side effects in `main()`).

Steps (all via the qBittorrent WebUI API v2, **no container restart**):

1. Authenticate: `POST /api/v2/auth/login` (`QBITTORRENT_USER` /
   `QBITTORRENT_PASS`), cookie-jar session.
2. `GET /api/v2/app/preferences`; compute the diff against the desired set:
   - `auto_tmm_enabled = true` (new torrents default to TMM)
   - `category_changed_tmm_enabled = true`
   - `save_path_changed_tmm_enabled = true`
   - `temp_path = /downloads/incomplete/qbittorrent`
   - `temp_path_enabled = true`
   Apply only if changed via `POST /api/v2/app/setPreferences` (form field
   `json=<changed-subset>`).
3. `GET /api/v2/torrents/info`; collect hashes where `auto_tmm` is false.
   Flip them with `POST /api/v2/torrents/setAutoManagement`
   (`hashes=<a|b|...>&enable=true`) — qBit relocates each into its category
   save path. Batch the hashes (chunked) to keep the request sane.
4. Report: prefs changed, torrents flipped, and a per-category count of where
   torrents will land (from the live category save paths).

**Safety / behavior:**
- Acts by default; `--dry-run` prints the pref diff and the
  hash→category→target-path plan and exits 0 without calling any setter.
- Idempotent: a second run with TMM already on and all torrents auto-managed is
  a no-op ("nothing to change").
- Does **not** create or modify categories (their paths are already correct);
  does not touch the empty-path cruft categories.
- Exit codes: `0` success / no-op / dry-run, `1` partial (some API calls
  failed), `2` fatal (config missing, qBittorrent unreachable, auth failed).

**Pure functions (unit-tested, no network):**
- `plan_pref_changes(current: dict, desired: dict) -> dict` — subset of desired
  that differs from current.
- `collect_unmanaged_hashes(torrents: list[dict]) -> list[str]` — hashes with
  `auto_tmm` falsey.
- `summarize_targets(torrents, categories) -> dict[str,int]` — category→count
  for the relocate preview.

**Env:** `QBITTORRENT_USER`, `QBITTORRENT_PASS` (both already in
`.env`/`.env.example`); `QBITTORRENT_HOST` (default `http://localhost:8080`).

### Component 2 — slskd incomplete dir (config edits, one restart)

- **`docker-compose.yml`** (slskd `environment:`, after the existing
  `SLSKD_DOWNLOADS_DIR` line): add
  `- SLSKD_INCOMPLETE_DIR=/downloads/incomplete/slskd`. slskd env overrides
  `slskd.yml`, and this keeps compose the source of truth (mirrors
  `SLSKD_DOWNLOADS_DIR`).
- **`${CONFIG_DIRECTORY}/slskd/slskd.yml`** (the LIVE file at
  `/home/tom/nas/.docker-config/slskd/slskd.yml`): change
  `directories.incomplete` to `/downloads/incomplete/slskd` so the file does not
  contradict the env var.
- **Pre-create host dirs** owned `${PUID}:${PGID}`:
  `${SHARE_DIRECTORY}/downloads/incomplete/{qbittorrent,slskd}`.
- **Recreate slskd:** `docker compose up -d slskd`. **This is the safest moment**
  — slskd was just nuked to a clean slate, so it has no active transfers.
  Single controlled cold start; watch the Soulseek login per the ghost-session
  rule in CLAUDE.md (if login hangs at 5 s, leave it DOWN 15–30 min, then
  cold-start — do NOT restart-spiral).

### Target layout after Phase 1

```
downloads/
  complete/
    sonarr/      ← 95 torrents relocate here
    radarr/      ← 12 torrents relocate here
    lidarr/      ← (future torrent music, if any)
    slskd/       ← soulseek completed (managed by existing tools)
    manual/      ← only genuinely-manual adds remain (Phase 2 cleans residue)
  incomplete/
    qbittorrent/ ← torrent temp (qBit temp_path)
    slskd/       ← soulseek temp (slskd incomplete)
```

## Order of operations (runbook for the plan)

1. Pre-create `incomplete/{qbittorrent,slskd}` dirs (`${PUID}:${PGID}`).
2. Run `qbittorrent_settings_enforce.py --dry-run`; eyeball the plan.
3. Run it for real → prefs set, 107 torrents relocate out of `manual/`.
4. Verify: `complete/{sonarr,radarr}` populate, `manual/` drops to genuinely
   manual content, qBit shows torrents healthy/seeding at new paths.
5. Edit `docker-compose.yml` + `slskd.yml`; `docker compose config` check.
6. `docker compose up -d slskd`; watch login; confirm new slskd downloads use
   `incomplete/slskd`.

## Testing

`scripts/tests/test_qbittorrent_settings_enforce.py` (pytest, offline):
- `plan_pref_changes`: returns only differing keys; empty when already correct.
- `collect_unmanaged_hashes`: picks `auto_tmm=false`, skips already-managed,
  handles empty list.
- `summarize_targets`: maps each torrent's category to its save-path count;
  handles uncategorized / empty-path categories.

Plus the standard gates: `ruff check scripts`, `docker compose config`,
`pytest -q scripts/tests`.

## Side-findings (flagged, out of scope)

- **Stale duplicate config tree** at `/mnt/drive/.docker-config` — NOT what the
  containers mount (they use `${CONFIG_DIRECTORY}=/home/tom/nas/.docker-config`).
  It diverges from the live tree (e.g. its `slskd.yml`). Harmless but confusing;
  consider deleting it in a later cleanup. Do not edit it.
- **Empty-path qBittorrent categories** `movies-radarr`, `music-lidarr`,
  `tv-sonarr` — unused cruft (no torrents reference them). Candidate for removal
  later; left alone in Phase 1.

## Out of scope (Phase 2 / later)

- Sweeping residue from `complete/manual/` and orphaned `incomplete/` dirs.
- Removing the stale `/mnt/drive/.docker-config` tree.
- Deleting empty cruft categories.
- Any compose volume-mount changes.

## Docs to update on implementation

- `scripts/README.md` — add `qbittorrent_settings_enforce.py` entry.
- `AGENTS.md` — add the script to the operational-scripts list (reuses existing
  `QBITTORRENT_*` env vars; document `QBITTORRENT_HOST` if added).
- `package.json` — optional `qbt:tidy` / `qbt:tidy:dry` scripts (consistent with
  existing wrappers).
