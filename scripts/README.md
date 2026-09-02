# NAS Automation Scripts

This directory contains utility scripts for automating various tasks in the media server stack.

## 📋 Available Scripts

### 1. **Check Current Status**:

```bash
python scripts/prowlarr_priority_checker.py
```

2. **Review Recommendations**:r Priority Management

#### `prowlarr_priority_setter.py`

**Purpose**: Automatically updates indexer priorities in Prowlarr via API
**Status**: ⚠️ _Has API issues - use checker instead_

- **Features**:
  - Clean code architecture following SOLID principles
  - Fuzzy name matching for indexer identification
  - Intelligent error classification and handling
  - Comprehensive logging and reporting
  - Environment variable configuration via `.env`
  - Dry-run mode for testing

- **Usage**:

  ```bash
  cd /home/<username>/nas
  python scripts/prowlarr_priority_setter.py --dry-run  # Preview changes
  python scripts/prowlarr_priority_setter.py           # Apply changes (⚠️ may hang)
  ```

- **Known Issues**:
  - PUT requests to Prowlarr API may hang indefinitely
  - Appears to be a Prowlarr API bug with complex object serialization

#### `prowlarr_priority_checker.py` ✅ **Recommended**

**Purpose**: Analyzes indexer priorities and provides manual update instructions
**Status**: ✅ _Fully functional - recommended approach_

- **Features**:
  - Fast and reliable priority analysis
  - Fuzzy matching with confidence scores
  - Clear categorization of indexers (needs update / already correct / not in list)
  - Manual update instructions for Prowlarr UI
  - No API update issues

- **Usage**:

  ```bash
  cd /home/<username>/nas
  python scripts/prowlarr_priority_checker.py
  ```

- **Sample Output**:

  ```
  🔄 UPDATES NEEDED (3):
    • iTorrent (ID: 12): Current: 50 → New: 30
    • Solid Torrents (ID: 32): Current: 50 → New: 25
    • Torrentz2nz (ID: 26): Current: 50 → New: 44

  📋 MANUAL UPDATE INSTRUCTIONS:
  1. Open http://localhost:9696/settings/indexers
  2. Update the listed indexers with new priorities
  ```

## ⚙️ Configuration

### Environment Variables (`.env`)

Both scripts require the following variables in your `.env` file:

```bash
# Required
API_KEY_PROWLARR=your_prowlarr_api_key

# Optional (defaults shown)
PROWLARR_HOST=http://localhost
PROWLARR_PORT=9696
```

### Finding Your Prowlarr API Key

1. Open Prowlarr web interface
2. Go to Settings → General
3. Copy the API Key value
4. Add to your `.env` file as `API_KEY_PROWLARR=your_key_here`

### Priority Configuration (`prowlarr-config.yml`)

Both scripts now load indexer priorities from a YAML configuration file for easier management:

```yaml
indexer_priorities:
  # Premium/High Quality Indexers (1-10)
  YTS: 1 # High quality movie releases
  SubsPlease: 5 # Reliable anime releases
  showRSS: 10 # TV show RSS feeds

  # Mid-tier Indexers (11-30)
  The Pirate Bay: 15 # Popular public tracker
  TorrentGalaxyClone: 21 # General purpose tracker
  Solid Torrents: 25 # Decent general tracker
  Torrent9: 30 # French tracker
  TorrentDownload: 33 # Standard tracker

  # ... more indexers (see prowlarr-config.yml for complete list)

# Configuration settings
settings:
  fuzzy_match_threshold: 0.8 # Minimum confidence for fuzzy matching
  strict_fuzzy_threshold: 0.9 # Higher threshold for setter script
```

**Priority Scale**: 1-50 (1 = highest priority, 50 = lowest priority)
**Configuration**: Edit `prowlarr-config.yml` to customize indexer priorities and matching settings

## 🔧 Setup Instructions

### Prerequisites

1. **Python Environment**:

   ```bash
   cd /home/<username>/nas
   python -m venv .venv
   source .venv/bin/activate  # Linux/Mac
   # or .venv\Scripts\activate  # Windows
   ```

2. **Install Dependencies**:

   ```bash
   pip install -r scripts/requirements.txt
   # or manually: pip install requests python-dotenv PyYAML
   ```

3. **Environment Configuration**:
   - Ensure `.env` file exists in the project root
   - Add `API_KEY_PROWLARR=your_api_key` to `.env`

### Quick Test

```bash
# Run comprehensive test suite
python scripts/test_scripts.py

# Test connectivity and view current status
python scripts/prowlarr_priority_checker.py
```

## 🚀 Recommended Workflow

1. **Analyze Current State**:

   ```bash
   python scripts/prowlarr_priority_checker.py
   ```

2. **Review Recommendations**:
   - Check fuzzy matches for accuracy
   - Verify priority assignments match your preferences
   - Note indexers that need manual updates

3. **Apply Updates Manually**:
   - Open Prowlarr UI at `http://localhost:9696/settings/indexers`
   - Update priorities as recommended by the checker script
   - Verify changes in the UI

4. **Re-run Checker** (optional):
   ```bash
   python scripts/prowlarr_priority_checker.py
   ```
   Should show "All indexers already have correct priorities!"

## 🛠️ Development & Customization

### Adding New Indexers

Edit the `INDEXER_PRIORITIES` dictionary in either script:

```python
INDEXER_PRIORITIES = {
    # ... existing entries
    "New Indexer Name": 25,  # Add your new indexer with desired priority
}
```

### Adjusting Fuzzy Matching

Change the `FUZZY_MATCH_THRESHOLD` value:

- Higher values (0.9+): More strict matching
- Lower values (0.7-): More lenient matching

### Custom Priority Schemes

You can modify the priority values to match your preferences:

- **Performance-based**: Assign lower numbers to faster indexers
- **Quality-based**: Prioritize indexers with better quality content
- **Reliability-based**: Higher priority for more stable indexers

## 📊 Architecture Overview

### `prowlarr_priority_setter.py` (Advanced)

**Clean Code Architecture**:

- `ProwlarrConfiguration`: Configuration management
- `IndexerMatcher`: Fuzzy name matching logic
- `IndexerValidator`: Update validation rules
- `ErrorClassifier`: Intelligent error categorization
- `ProwlarrApiClient`: API communication handling
- `IndexerPriorityUpdater`: Main orchestration class
- `ProcessingSummary`: Results tracking and reporting

**Design Principles**:

- Single Responsibility Principle (SRP)
- Dependency Injection
- Type Safety with hints
- Comprehensive error handling
- Structured logging

### `prowlarr_priority_checker.py` (Simple & Reliable)

**Streamlined Design**:

- Single-file architecture for simplicity
- Direct API calls without complex state management
- Focus on analysis and reporting
- Minimal dependencies for maximum reliability

## 🐛 Troubleshooting

## 🧰 Maintenance & Operations Scripts

These additional scripts help keep the stack healthy and tidy.

### `config_backup.py`

Creates timestamped `tar.gz` archives of service configuration directories (from `CONFIG_DIRECTORY`). Supports pruning old archives, exclusions, fast mode, and restoring.

Key features:

- Curated list of default services (override with `--services`)
- Retention pruning (`--retain`, or `BACKUP_RETAIN` env)
- Exclude patterns: `--exclude PATTERN`, `--exclude-from file`, `--default-excludes`
- Fast mode (`--fast`): applies default excludes + adds log directory exclusion + size cap
- Size-based skipping: `--max-file-size MB`
- Optional checksum skipping: `--no-checksum`
- Progress feedback (auto when interactive; force with `--progress` / disable with `--no-progress`)
- Graceful interrupt handling (Ctrl+C cleans up unless `--keep-partial`)

Usage examples:

```
python scripts/config_backup.py                       # create backup
python scripts/config_backup.py --list                # list archives
python scripts/config_backup.py --restore configs-20250101-000000.tar.gz
python scripts/config_backup.py --retain 14           # keep 14 most recent
python scripts/config_backup.py --exclude jellyfin/cache/** --exclude-from excludes.txt
python scripts/config_backup.py --fast --no-checksum  # quick lightweight backup
```

Fast mode defaults: excludes heavy cache/transcode/data/temp paths and `**/logs/**`, applies a 25MB file size cap (can override with `--max-file-size`).

Environment: `CONFIG_DIRECTORY` (required), `BACKUP_DIR` (override destination – default is `CONFIG_DIRECTORY/backups`), `BACKUP_RETAIN`.

### `permissions_auditor.py`

Audits ownership (PUID/PGID) and basic permissions. Optionally fixes them.

```
python scripts/permissions_auditor.py                 # report
python scripts/permissions_auditor.py --fix           # fix (may need sudo)
python scripts/permissions_auditor.py --fix --dry-run # show planned changes
```

Environment: `PUID`, `PGID`, `CONFIG_DIRECTORY`, optional `SHARE_DIRECTORY` (use `--include-share`).

### `post_update_verifier.py`

Verifies that core services are healthy after updates (e.g. Watchtower run). Checks Docker container state & HTTP endpoints.

```
python scripts/post_update_verifier.py
VERIFY_SERVICES="prowlarr,sonarr,radarr" python scripts/post_update_verifier.py
```

Exit codes: 0 all healthy, 1 degraded, 2 fatal. Environment keys: `API_KEY_PROWLARR`, `API_KEY_SONARR`, `API_KEY_RADARR` (optional), `DOCKER_BIN`.

### `log_pruner.py`

Compresses or truncates oversized, older log files inside `CONFIG_DIRECTORY` (or specified roots).

```
python scripts/log_pruner.py --max-mb 10 --min-age 0
python scripts/log_pruner.py --roots /custom/logs --dry-run
```

Environment: `LOG_PRUNE_MAX_MB` (default 25), `LOG_PRUNE_MIN_AGE_DAYS` (1), `LOG_PRUNE_COMPRESS` (true/false).

### `slskd_rescan.py`

Forces slskd to rescan `SLSKD_SHARED_DIR` (`/music`). slskd scans only at startup; if the share is empty or unmounted at that moment, the stack silently advertises zero files to Soulseek peers, which triggers heavy peer-side throttling (`Transfer rejected: Overwhelmed with requests`). A periodic rescan ensures newly imported albums become available to peers and keeps the share advertised as non-empty.

```bash
python scripts/slskd_rescan.py              # fire and forget
python scripts/slskd_rescan.py --wait       # block until scan finishes, report counts
python scripts/slskd_rescan.py --dry-run    # print intended action
```

Exit codes: `0` success, `1` scan completed with empty share (mount/perms issue), `2` fatal (config / network / HTTP error).

Environment: `API_KEY_SLSKD` (required), `SLSKD_HOST` (default `http://localhost:5030`).

### `slskd_cleanup.py`

Clears stale slskd `Completed, *` transfer rows that Tubifarry (Lidarr's slskd plugin) leaves behind after Lidarr imports a download. Tubifarry only removes a transfer record while the item is still in Lidarr's queue; once Lidarr drops the queue entry, the slskd row sits in `Completed, Succeeded` / `Errored` / `Rejected` state forever and slowly clogs slskd's transfer manager + peer connection state. Run hourly to keep the channel clean.

Safety design — does **not** conflict with the Lidarr/Tubifarry flow:

1. **Lidarr-quiet gate (per-transfer)** — build the set of dir basenames referenced by _active_ Lidarr queue items (`downloading` / `importPending` / `importing` / `importBlocked` / `importFailed`). A `Completed, Succeeded` slskd record is deferred only if its trailing-segment basename appears in that set. Succeeded records that no Lidarr item references are cleaned in the same run, so the slskd transfer manager doesn't accumulate indefinitely just because _some other_ import is in flight. Terminal-failure states (`Completed, Errored / Rejected / Cancelled / TimedOut`) are always cleaned — they will never trigger a Tubifarry callback.
2. **Per-record age gate** — only deletes records whose `endedAt` is older than `--min-age-hours` (default `1`). Records without `endedAt` are skipped conservatively.
3. **Per-dir age gate** — only removes `/downloads/incomplete/<name>` whose mtime is older than the same threshold.
4. **Name allowlist on disk** — only ever deletes incomplete dirs that slskd itself listed in the transfer it just removed. qBittorrent shares `/downloads/incomplete` with slskd (as its `Session\TempPath`), so this is critical: indiscriminate sweeps would destroy in-progress torrents.

```bash
python scripts/slskd_cleanup.py                       # clean now
python scripts/slskd_cleanup.py --dry-run             # report only
python scripts/slskd_cleanup.py --min-age-hours 6     # extra safety buffer
python scripts/slskd_cleanup.py --keep-dirs           # clear API rows only, leave fs
python scripts/slskd_cleanup.py --skip-lidarr-check   # bypass the quiet gate (manual one-offs)
```

Exit codes: `0` success or nothing to do, `1` partial (some API deletes / fs ops failed), `2` fatal (config / network).

Environment: `API_KEY_SLSKD` (required), `API_KEY_LIDARR` (required for the quiet-gate), `SLSKD_HOST` (default `http://localhost:5030`), `LIDARR_HOST` (default `http://localhost:8686`), `INCOMPLETE_DIR` (default `/mnt/drive/downloads/incomplete`).

### `lidarr_queue_unstick.py`

Clears Lidarr queue items wedged in `completed / importFailed` state. These accumulate when slskd peers deliver a download that Lidarr can't accept. Lidarr has no built-in "remove failed import after N hours" setting; `autoRedownloadFailed` only fires for download-side failures, so these rows live forever, block Tubifarry from clearing the matching slskd transfer, and gum up the whole pipeline. Run hourly to keep throughput high.

The script splits failures by **why** they failed, because they need opposite handling:

**Reclaim pass (default on).** `Album release not requested` is _not_ a bad download — the peer sent a complete, valid album that maps to a different MusicBrainz release than the one Lidarr monitors. Lidarr's automatic import pipeline deliberately disables release switching (so a random peer can't flip your monitored edition) and there is **no global toggle** for it, so these sit wedged forever. For each such row the script re-imports the download via the manual-import API with `disableReleaseSwitching: false`: Lidarr re-points the monitored release to the edition on disk and imports the files already there. Success is verified against the album's `trackFile` count (a ManualImport that imports nothing still reports `completed`), and if the primary import is a no-op — files were already copied into the library by a prior `albumImportIncomplete` but never registered — it re-scans the artist folder and registers those orphans in place. Only when track files actually appear is the now-satisfied row dropped with `blocklist=false&skipRedownload=true` (no re-download, no blocklist). Disable with `--no-reclaim`.

**Destructive pass.** Genuine bad matches (`Album match is not close enough: X% vs 80%`, `Couldn't find similar album`) and any reclaim that failed get `DELETE /api/v1/queue/{id}?removeFromClient=true&blocklist=true&skipRedownload=true`: drops the entry, kills the slskd transfer via Tubifarry, and blocklists the specific release. `skipRedownload` is **true by default** — an immediate per-row replacement search piles onto the Soulseek search burst that earns flood bans, so re-finding is left to the paced `lidarr_backlog_drip`. Pass `--redownload` to search immediately.

Safety design:

1. **State gate** — only rows with `trackedDownloadState == importFailed` are touched. Downloading / importing rows are left strictly alone.
2. **Age gate** — only acts on rows whose `added` timestamp is older than `--min-age-hours` (default `1`). Rows missing `added` are skipped conservatively.
3. **Reclaim is conservative** — a row is only reclaimed when `Album release not requested` is present _and_ no hard blocker (`not close enough`, `couldn't find similar`, `destination already exists`) is, so fuzzy matches never get force-imported.
4. **Effect-verified** — reclaim never clears a queue row unless the album's track-file count actually increased.

```bash
python scripts/lidarr_queue_unstick.py                       # reclaim + clean now (1h age gate)
python scripts/lidarr_queue_unstick.py --dry-run             # report the reclaim/remove split only
python scripts/lidarr_queue_unstick.py --min-age-hours 0     # immediate (manual one-off)
python scripts/lidarr_queue_unstick.py --no-reclaim          # legacy: delete+blocklist+redownload everything
python scripts/lidarr_queue_unstick.py --import-mode move    # reclaim with move instead of copy
python scripts/lidarr_queue_unstick.py --redownload          # blocklist AND immediately search a replacement (default is skip — avoids flood bans)
python scripts/lidarr_queue_unstick.py --no-blocklist        # remove only (not recommended — Tubifarry will re-grab the same junk)
```

Exit codes: `0` success or nothing to do, `1` partial (some deletes failed), `2` fatal (config / network / HTTP error).

Environment: `API_KEY_LIDARR` (required), `LIDARR_HOST` (default `http://localhost:8686`).

### `lidarr_stuck_download_reaper.py`

Closes the gap `lidarr_queue_unstick.py` leaves: grabs wedged **forever at 0 bytes** in slskd. When a remote peer queues us (`Queued, Remotely`) but never starts uploading, the files never transfer, the matching Lidarr row stays in `downloading` indefinitely (it never reaches `importFailed`, so unstick ignores it), and slskd's transfer manager fills with dead entries that pin the in-flight count — permanently parking `lidarr_backlog_drip`. slskd has no timeout for remotely-queued downloads, so without this they accumulate without bound.

The reaper reads slskd `/transfers/downloads`, flags every `Queued,*` file with `bytesTransferred == 0` whose `enqueuedAt` is older than `--stuck-hours` (default `12`; a started-then-paused transfer with bytes > 0 is treated as alive), maps each to its Lidarr queue row by dir basename, and `DELETE /api/v1/queue/{id}?removeFromClient=true&blocklist=true&skipRedownload=true` — Tubifarry cancels the slskd transfer, the dead release is blocklisted, and re-sourcing from a live peer is left to the paced `lidarr_monitor_sweep` / `lidarr_backlog_drip` (no flood-ban burst). Stuck transfers with no Lidarr match (orphaned grabs) are cancelled directly via slskd. `--max-actions` (default `40`) caps the blast radius per run so a backlog drains over several hourly runs. Stall detection uses slskd's own `enqueuedAt`/`bytesTransferred` — Lidarr's `size`/`sizeleft` is unreliable for slskd grabs.

```bash
python scripts/lidarr_stuck_download_reaper.py --dry-run                       # report the plan only
python scripts/lidarr_stuck_download_reaper.py --stuck-hours 12 --max-actions 40   # default cron invocation
python scripts/lidarr_stuck_download_reaper.py --stuck-hours 6                 # more aggressive turnover
python scripts/lidarr_stuck_download_reaper.py --no-blocklist                  # remove without blocklisting (not recommended)
```

Exit codes: `0` success / nothing to do, `1` partial (some deletes failed), `2` fatal (config / slskd or Lidarr unreachable / HTTP error).

Environment: `API_KEY_SLSKD` + `API_KEY_LIDARR` (required), `SLSKD_HOST` (default `http://localhost:5030`), `LIDARR_HOST` (default `http://localhost:8686`). Cron `:52`, shares `/tmp/nas-tubifarry-cleanup.lock` with the other hourly hygiene jobs. Kept in lock-step with `lidarr_backlog_drip --stale-queued-hours` (both 12h) so the drip's capacity gate ignores exactly the entries this reaper removes.

### `slskd_lidarr_nuke.py`

**Clean-slate button** for the slskd↔Lidarr pipeline — aggressive, on-demand,
idempotent. The opposite of the gated reapers: it resets everything to zero.

1. **Lidarr queue teardown** — deletes every queue row with
   `removeFromClient=true&blocklist=true&skipRedownload=true`. Lidarr cancels
   the slskd transfer via Tubifarry (nothing orphaned), blocklists the dead
   release (album stays monitored/missing), and does **not** auto re-search —
   re-kick searches yourself (e.g. lidarr-bulk) to re-grab fresh copies.
2. **slskd full wipe** — cancels every active/queued transfer and clears all
   terminal records (`DELETE .../downloads/all/completed`, per-transfer
   fallback).
3. **Completed-folder sweep** — `rmtree`s every dir under
   `SLSKD_COMPLETE_DIR` except those an active Lidarr import references.

Acts by default; `--dry-run` previews. Phase toggles: `--skip-lidarr`,
`--skip-slskd`, `--skip-folder-sweep`.

```bash
python scripts/slskd_lidarr_nuke.py --dry-run   # preview
python scripts/slskd_lidarr_nuke.py             # ACT: full clean slate
```

Env: `API_KEY_LIDARR`, `API_KEY_SLSKD`, `LIDARR_HOST`, `SLSKD_HOST`,
`SLSKD_COMPLETE_DIR`. Exit: `0` ok/dry-run/noop, `1` partial, `2` fatal.

### `qbittorrent_settings_enforce.py`

Enforces qBittorrent **Auto Torrent Management** so category tags actually drive
save paths. Sets `auto_tmm_enabled` / `category_changed_tmm_enabled` /
`save_path_changed_tmm_enabled` and points the temp path at
`/downloads/incomplete/qbittorrent`, then flips existing torrents to
auto-managed so qBittorrent relocates them from `complete/manual/` into their
category folders (`complete/sonarr`, `complete/radarr`, …). Same-filesystem
rename — instant, hardlinks preserved, seeding uninterrupted. Idempotent.

Acts by default; `--dry-run` previews the pref diff and relocate plan.

```bash
python scripts/qbittorrent_settings_enforce.py --dry-run
python scripts/qbittorrent_settings_enforce.py
```

Env: `QBITTORRENT_USER`, `QBITTORRENT_PASS`, `QBITTORRENT_HOST`
(default `http://localhost:8080`). Exit: `0` ok/dry-run/no-op, `1` partial,
`2` fatal.

### `slskd_complete_sweep.py`

Reaps `/downloads/complete/slskd/<dir>` subtrees that Lidarr has already imported into `/music/`. With Lidarr configured to use hardlinks (`copyUsingHardlinks=true`), the slskd download copy is never reaped by the import path itself; over weeks this accumulates GBs of duplicates of files that already live in the music library.

Match strategy is size-based against a one-shot walk of `/music/`. By default `--threshold 1.0` requires that _every_ audio file in the slskd dir has a matching size in the library — false-positive risk is essentially zero. Dirs the Lidarr queue still references are skipped (mid-flight protection), and an age gate keeps freshly downloaded folders alone for the first hour.

```bash
python scripts/slskd_complete_sweep.py                    # delete confirmed dups
python scripts/slskd_complete_sweep.py --dry-run          # report only
python scripts/slskd_complete_sweep.py --threshold 0.9    # require ≥90% of files match
python scripts/slskd_complete_sweep.py --limit 20         # cap deletions per run
```

Exit codes: `0` success / nothing to do, `1` partial (some rmtree failed), `2` fatal (config / Lidarr unreachable / no /music).

Environment: `API_KEY_LIDARR` (required), `LIDARR_HOST` (default `http://localhost:8686`), `MUSIC_DIR` (default `/mnt/drive/music`), `SLSKD_COMPLETE_DIR` (default `/mnt/drive/downloads/complete/slskd`).

Note: this script reaps confirmed duplicates only. Folders Lidarr _rejected_ (peer mismatch, fingerprint below 80%) never get hardlinked into `/music/` and so look like orphans here — they are real orphans, but not duplicates, and need a separate decision (re-import via `process_soulseek_imports.py`, or manual triage). The sweeper deliberately leaves them alone.

### `slskd_incomplete_sweep.py`

Reusable **gated sweeper** for orphaned dirs in the slskd-owned zones of
`/downloads/incomplete` — the legacy flat dirs at the root and orphans under
`incomplete/slskd`. Never enters `incomplete/qbittorrent` (qBit-owned). A dir is
deleted only if it clears all three gates: not referenced by an active slskd
transfer, not referenced by a live qBittorrent torrent, and older than
`--min-age-hours` (default 24). Aborts (exit 2) if either reference fetch fails.

Acts by default; `--dry-run` previews.

```bash
python scripts/slskd_incomplete_sweep.py --dry-run
python scripts/slskd_incomplete_sweep.py --min-age-hours 24 --limit 50
```

Env: `API_KEY_SLSKD`, `SLSKD_HOST`, `QBITTORRENT_USER`, `QBITTORRENT_PASS`,
`QBITTORRENT_HOST`, `INCOMPLETE_DIR`. Exit: `0` ok/dry-run/no-op, `1` partial,
`2` fatal.

### `slskd_login_watch.py`

Alerts when slskd has been logged out of Soulseek longer than a grace period — and **never restarts it**. slskd's web server stays up while the Soulseek login is dead, and restarting on a login drop is harmful: a fast restart re-collides with slsknet's stale session for the username and perpetuates the 5000ms login-timeout spiral. The only cure is to leave slskd down 15–30 min, then cold-start. So this is the alert-only counterpart to that rule — slskd's container healthcheck is deliberately Soulseek-independent (it must not drive autoheal off login state).

Tracks how long slskd has been logged out across runs in a small JSON state file, so it can distinguish a brief reconnect blip from a genuine stuck session. Optionally POSTs to `SLSKD_ALERT_WEBHOOK` (e.g. an ntfy topic) so a drop reaches your phone.

```bash
python scripts/slskd_login_watch.py                  # check + print status
python scripts/slskd_login_watch.py --grace-min 10 --state logs/slskd_login_watch.json
```

Exit codes: `0` logged in (or out but within grace), `1` logged out past the grace period (alert raised), `2` fatal (config missing / slskd API unreachable).

Environment: `API_KEY_SLSKD` (required), `SLSKD_HOST` (default `http://localhost:5030`), `SLSKD_ALERT_WEBHOOK` (optional).

### `lidarr_monitor_sweep.py`

Self-heals artists that landed **monitored but with zero monitored albums** — the dead state bulk adds (lidarr-bulk) leave behind when Lidarr ignores `addOptions.monitor` or its post-add `RefreshArtist` clobbers the album monitoring. Such an artist never gets searched, so "nothing happens". The sweep monitors the whole discography of each broken artist and triggers an `ArtistSearch`.

Deliberately conservative: it only touches **monitored** artists (an unmonitored artist is a deliberate "don't want this") that have albums but **none** monitored — so it never disturbs artists where you intentionally monitored just one album, and never resurrects something you unmonitored. A fixed artist gains monitored albums and is skipped on the next run, so the sweep is self-limiting and a no-op in steady state. `--limit` caps artists per run so a large backlog drains gently instead of storming slskd.

```bash
python scripts/lidarr_monitor_sweep.py            # fix + search
python scripts/lidarr_monitor_sweep.py --dry-run  # report only
python scripts/lidarr_monitor_sweep.py --limit 5  # cap artists per run (cron uses this)
python scripts/lidarr_monitor_sweep.py --no-search # monitor only, don't search
```

Exit codes: `0` success / nothing to do, `1` partial (some calls failed), `2` fatal (config / Lidarr unreachable).

Environment: `API_KEY_LIDARR` (required), `LIDARR_HOST` (default `http://localhost:8686`).

### `lidarr_backlog_drip.py`

Drip-feeds Lidarr's **missing-album backlog** (thousands of monitored-but-missing albums) into Soulseek without flooding it. Lidarr's built-in `MissingAlbumSearch` searches _everything_ at once — hundreds of grabs hit slskd, peers queue them remotely, and they wedge at 0 bytes holding slots forever (the classic clog). This script is the controlled alternative: it searches a small batch **only when slskd has spare capacity**.

The gate is slskd's _live in-flight_ download count: any file not in a `Completed` state, **except** a zero-byte `Queued, Remotely` grab older than `--stale-queued-hours` (default `12`) — those are dead (the peer never started) and would otherwise pin the count high forever and permanently park the drip. (Recently-queued grabs still count, so the drip keeps self-throttling and never re-floods.) Keep `--stale-queued-hours` in step with `lidarr_stuck_download_reaper --stuck-hours`, which actually removes those dead entries. When in-flight is below `--threshold`, it searches the next `--batch` missing albums it hasn't touched within `--cooldown-hours`; otherwise it does nothing and the drip pauses itself. A rolling `--state` JSON records each album's last-searched epoch so successive runs **walk the whole backlog** instead of re-firing the same first page, and only retry an album after the cooldown. Self-throttling by design: when downloads back up, the drip stops; as they complete, it resumes.

**Pacing (`--search-delay`, default 20s).** The batch is dispatched as one `AlbumSearch` _per album_, spaced `--search-delay` seconds apart, rather than a single command with all the IDs. A bulk command makes slskd fire the whole batch onto the Soulseek network at once, which the central server treats as flooding/"quickly repeating a search" and answers with a **30-minute account ban** (`server` chat: _"banned for 30 minutes… too many operations at once"_). Those bans were the true upstream cause of the importFailed clog: every ban is 30 min where no grab can complete. Pacing 20 searches over ~7 min keeps the rate well under the threshold while staying inside the 15-min cron window. A failed search leaves its album un-stamped so the next run retries it. `--search-delay 0` restores the legacy single-command burst.

```bash
python scripts/lidarr_backlog_drip.py                          # gated, paced drip (cron uses this)
python scripts/lidarr_backlog_drip.py --dry-run                # report what it would search
python scripts/lidarr_backlog_drip.py --threshold 40 --batch 20 --search-delay 20
python scripts/lidarr_backlog_drip.py --state logs/lidarr_backlog_drip.json
```

Exit codes: `0` success / intentionally idle (queue busy or all of this page within cooldown), `1` partial (the `AlbumSearch` POST failed), `2` fatal (config / Lidarr or slskd unreachable).

Environment: `API_KEY_LIDARR` + `API_KEY_SLSKD` (both required), `LIDARR_HOST` (default `http://localhost:8686`), `SLSKD_HOST` (default `http://localhost:5030`).

### `jellyfin_subtitle_css.py`

Fixes the "large subtitles wrap onto a second line constantly" problem. jellyfin-web renders each cue into `.videoSubtitlesInner`, which ships with a hardcoded **`max-width: 70%`** — the subtitle _size_ setting scales the font but never the box, so comfortable text immediately outgrows its container. Jellyfin exposes no API field for the box geometry; the only server-side lever is **Branding → Custom CSS**, which every jellyfin-web client (browser, webOS, Tizen, Fire TV) fetches from `/Branding/Css` on load. This script merges a sentinel-delimited managed block into `BrandingOptions.CustomCss` over `POST /System/Configuration/branding`.

Idempotent by construction: the `/* >>> nas-managed: subtitle-layout >>> */` sentinels mean a re-run **replaces** the block in place instead of appending, and any CSS you added by hand outside the markers is preserved byte-for-byte. `--remove` strips the block cleanly. Writes the full `BrandingOptions` object back (not just `CustomCss`) — a partial POST blanks `LoginDisclaimer`/`SplashscreenEnabled`.

**Caveats:** text subtitles only (SRT/ASS) — image subs (PGS/VOBSUB) are drawn to a canvas and ignore CSS. Native apps (Android/iOS/Android TV) ignore server Custom CSS entirely. Clients cache it: hard-refresh the browser, restart the TV app.

```bash
python scripts/jellyfin_subtitle_css.py                                  # dry-run preview (default)
python scripts/jellyfin_subtitle_css.py --apply                          # write it
python scripts/jellyfin_subtitle_css.py --apply --max-width 96 --line-height 1.2
python scripts/jellyfin_subtitle_css.py --apply --remove                 # strip the block
```

Exit codes: `0` success / dry-run / already up to date, `1` partial (read fine, write rejected), `2` fatal (API key missing, Jellyfin unreachable, bad `--max-width`).

Environment: `API_KEY_JELLYFIN` (required), `JELLYFIN_HOST` (default `http://localhost:8096`).

### Integration

All new scripts are included in `test_scripts.py` for import validation. The full live crontab on this host:

```
# --- daily ---
# 01:00 — config archive (fast mode keeps it small, prune to 14 most recent)
0 1 * * * /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/config_backup.py --fast --no-checksum --retain 14 >> logs/config_backup.log 2>&1"
# 03:30 — force slskd to re-advertise the shared library to Soulseek peers
30 3 * * * /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/slskd_rescan.py --wait >> logs/slskd_rescan.log 2>&1"
# 04:30 — post-Watchtower health check (Watchtower fires at 04:00)
30 4 * * * /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/post_update_verifier.py >> logs/post_update_verifier.log 2>&1"
# 05:30 — re-import orphaned slskd download folders that lost their Lidarr queue row (the 4th hygiene job); shares the cleanup flock, 50min budget, rolling --state so only new folders are fingerprinted, stub guard at 0.5, --purge-not-upgrade deletes folders Lidarr can only reject as "not an upgrade"
30 5 * * * /usr/bin/flock -w 600 /tmp/nas-tubifarry-cleanup.lock /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/process_soulseek_imports.py --execute --skip-queue-tracked --accept-min-match 70 --min-track-fraction 0.5 --purge-not-upgrade --state logs/slsk_import_state.tsv --max-seconds 3000 >> logs/process_soulseek_imports.log 2>&1"

# --- weekly ---
# Sunday 02:00 — compress / truncate oversize log files inside CONFIG_DIRECTORY
0 2 * * 0 /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/log_pruner.py >> logs/log_pruner.log 2>&1"

# --- hourly Tubifarry/slskd hygiene (mutex-locked, must run in order) ---
# :07 — drain Lidarr's importFailed queue; Tubifarry clears matching slskd records as it goes
07 * * * * /usr/bin/flock -n /tmp/nas-tubifarry-cleanup.lock /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/lidarr_queue_unstick.py >> logs/lidarr_queue_unstick.log 2>&1"
# :22 — reap /downloads/complete/slskd/<dir> whose audio is fully represented under /music/
22 * * * * /usr/bin/flock -n /tmp/nas-tubifarry-cleanup.lock /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/slskd_complete_sweep.py >> logs/slskd_complete_sweep.log 2>&1"
# :37 — direct slskd sweep picks up anything Lidarr never tracked (errored/rejected/cancelled transfers)
37 * * * * /usr/bin/flock -n /tmp/nas-tubifarry-cleanup.lock /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/slskd_cleanup.py >> logs/slskd_cleanup.log 2>&1"
# :52 — reap grabs wedged >12h at 0 bytes in slskd (peer never started): blocklist the dead release + cancel the slskd transfer so monitor-sweep/drip re-source from a live peer. Frees the in-flight count that otherwise parks the backlog drip; --max-actions caps blast radius so a backlog drains over several runs.
52 * * * * /usr/bin/flock -w 120 /tmp/nas-tubifarry-cleanup.lock /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/lidarr_stuck_download_reaper.py --stuck-hours 12 --max-actions 40 >> logs/lidarr_stuck_download_reaper.log 2>&1"

# --- slskd login watchdog (alert-only, NOT on the cleanup flock) ---
# */15 — alert if slskd has been logged out of Soulseek >10min. Never restarts:
# the ghost-session cure is to stop slskd 15-30min then cold-start, and a restart
# only re-collides with the stale session. Set SLSKD_ALERT_WEBHOOK (e.g. an ntfy
# topic) in .env to get a push instead of just a log line.
*/15 * * * * /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/slskd_login_watch.py --grace-min 10 --state logs/slskd_login_watch.json >> logs/slskd_login_watch.log 2>&1"

# --- lidarr monitor sweep (self-heals bulk-added artists left unmonitored) ---
# 5,20,35,50 — gentle drain: fix at most 5 artists/run so a large backlog (the
# bulk-add monitoring bug can leave hundreds of artists with 0 monitored albums)
# trickles into searches instead of storming slskd. Offset from the :00/:15
# watchdog so they don't run together. Self-limiting: a fixed artist gains
# monitored albums and is skipped next run.
5,20,35,50 * * * * /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/lidarr_monitor_sweep.py --limit 5 >> logs/lidarr_monitor_sweep.log 2>&1"

# --- lidarr backlog drip (self-throttling missing-album search) ---
# 12,27,42,57 — search 20 missing albums per run, but ONLY when slskd has spare
# *live* capacity (<40 in-flight; dead remotely-queued grabs >12h are excluded
# from the count by --stale-queued-hours, in step with the :52 reaper, so they
# no longer park the drip). Drains the missing backlog steadily and
# can never re-clog: when downloads back up the drip pauses itself. The rolling
# --state file walks the whole backlog (12h per-album cooldown) instead of
# re-firing the same first page. Own flock so it never overlaps itself; offset
# from the monitor sweep (:05/:20/...) and hourly hygiene (:07/:22/:37).
12,27,42,57 * * * * /usr/bin/flock -n /tmp/nas-lidarr-backlog-drip.lock /usr/bin/env bash -c "cd /home/<username>/nas && . .venv/bin/activate && python scripts/lidarr_backlog_drip.py --threshold 40 --batch 20 --cooldown-hours 12 --search-delay 20 --state logs/lidarr_backlog_drip.json >> logs/lidarr_backlog_drip.log 2>&1"
```

Pi-era host-tuning shell scripts and their docs live under `scripts/legacy/` — they are reference-only and not safe to run on the MS01 host (see `scripts/legacy/README.md`).

#### Concurrency and race-safety

The two cleanup scripts target overlapping state (a Lidarr `importFailed` queue item is backed by a `Completed, Succeeded` slskd transfer) and could in principle fight each other or fight Tubifarry's own import flow. They are designed not to:

1. **Mutex via `flock`** — both cron entries share `/tmp/nas-tubifarry-cleanup.lock` with `flock -n`, so a second invocation while the first is running exits immediately (next hour will pick it up). This covers cron overrun _and_ manual one-offs that fire during a scheduled run.
2. **Schedule ordering** — `lidarr_queue_unstick` runs first (`:07`), `slskd_cleanup` second (`:37`). Within each hour the Lidarr→Tubifarry→slskd path drains first, then the direct slskd sweep mops up what was never tied to Lidarr (errored peer transfers, rejected handshakes, cancelled grabs).
3. **Per-transfer deferrals in `slskd_cleanup`** — a `Completed, Succeeded` row is deferred only if its dir basename matches one referenced by an active Lidarr queue item (`downloading / importPending / importing / importBlocked / importFailed`). Tubifarry's own import flow owns the first four; `lidarr_queue_unstick` owns the fifth. Either way `slskd_cleanup` declines to race a Lidarr-side actor for _that specific_ transfer's path. Unmatched Succeeded rows (Lidarr already imported and dropped them) are cleaned in the same run. Terminal-failure slskd states (`Completed, Errored / Rejected / Cancelled`) have no Lidarr-side actor and are always safe to clean.
4. **Strict state filters in `lidarr_queue_unstick`** — only `trackedDownloadState == 'importFailed'` rows are touched. `importPending` / `importing` rows (mid-flight in Tubifarry's import pipeline) are never targeted, so the script cannot pull a queue item out from under an active import.
5. **Age gates on both sides** — `slskd_cleanup` requires `endedAt` older than `--min-age-hours` (default 1h); `lidarr_queue_unstick` requires `added` older than the same threshold. Rows without timestamps are conservatively skipped. This means in-progress workflows (which are sub-hour) are structurally excluded from cleanup.

### Common Issues

**Connection Errors**:

```bash
❌ Error: Failed to connect to Prowlarr API: 401
```

- **Solution**: Check `API_KEY_PROWLARR` in `.env` file

**Import Errors**:

```bash
ModuleNotFoundError: No module named 'requests'
```

- **Solution**: `pip install requests python-dotenv`

**No Indexers Found**:

```bash
✅ All indexers already have correct priorities!
```

- **Cause**: All indexers already match desired priorities
- **Action**: No changes needed, system is optimized

### Debug Mode

### `qbittorrent_stalled_kickstart.py`

Identifies stalled torrents (using qBittorrent Web API filters: `stalled`, `stalled_uploading`, `stalled_downloading`) and performs a gentle "kick" sequence: resume (if paused), reannounce, optional recheck.

Environment (from `.env`): `QBITTORRENT_USER`, `QBITTORRENT_PASS`, optional `QBITTORRENT_HOST` (default `http://localhost`), `QBITTORRENT_PORT` (default `8080`).

Usage examples:

```bash
python scripts/qbittorrent_stalled_kickstart.py                # standard kick
python scripts/qbittorrent_stalled_kickstart.py --dry-run      # inspect only
python scripts/qbittorrent_stalled_kickstart.py --recheck --max 5
python scripts/qbittorrent_stalled_kickstart.py --filters stalled stalled_downloading
python scripts/qbittorrent_stalled_kickstart.py --min-age 30   # ignore very recent
python scripts/qbittorrent_stalled_kickstart.py --no-reannounce
```

Exit codes: 0 success/no work; 1 partial failures; 2 fatal (auth/network/config).

Flags:

- `--recheck` optionally triggers hash recheck for stalled torrents (I/O heavy)
- `--min-age` (minutes) avoids acting on freshly added torrents (default 10)
- `--max` limit number of targeted torrents (safeguard)
- `--dry-run` report planned actions without executing
- `--no-reannounce` skip tracker reannounce

Safe by design: no deletions, no forceful state resets. One reannounce per batch.

### `album_art.py`

Backfills **missing external album covers** (`folder.jpg`) across the music library by delegating to [sacad](https://github.com/desbma/sacad)'s recursive `sacad_r` CLI, which searches Deezer/Discogs/iTunes/Last.fm and writes one image per album folder. Lidarr already writes `folder.jpg` for most albums (the convention this matches); this fills the few hundred gaps — and any future imports Lidarr fails to art — so Jellyfin always has a cover. `sacad_r` natively skips folders that already contain the cover file, so runs are incremental and idempotent (only the gaps trigger network calls). Albums with no cover on any source are simply left untouched and retried next run (sacad has no negative cache).

**Overwrite-once (`--overwrite-once`)** — much Lidarr/embedded art is low quality. This mode overwrites each album's cover _once_ with a fresh sacad image (`sacad_r -i` per folder), then drops a hidden `.album_art_done` marker so consecutive runs skip that folder forever. New albums arrive unmarked and get their one pass on the next run. `--limit N` (default 300) caps folders per run so the first big pass drains across several runs. **A cover is never blanked** — sacad leaves existing art in place when no source has a replacement. A folder that still has no cover after its attempt stays unmarked and is retried next run (identical to plain gap-fill). `--marker` overrides the sidecar filename. This is the mode the weekly cron uses.

**Dry-run is the default** — a bare invocation walks the tree and prints a plan (album dirs found / already have the cover / missing, plus a sample of missing paths) and downloads nothing. `--apply` is required to fetch.

```bash
python scripts/album_art.py                       # dry-run: plan only, downloads nothing
python scripts/album_art.py --music-dir /mnt/drive/music
python scripts/album_art.py --apply               # fill only MISSING folder.jpg at 1000px
python scripts/album_art.py --apply --size 600    # smaller covers
python scripts/album_art.py --apply --filename cover.jpg   # different cover filename
python scripts/album_art.py --apply --ignore-existing      # force re-download ALL, every run
python scripts/album_art.py --apply --overwrite-once --limit 300  # overwrite once then freeze (cron uses this)
```

Exit codes: `0` success / dry-run / nothing to do, `1` partial (`sacad_r` exited non-zero), `2` fatal (`sacad_r` not installed, music dir missing, unexpected error).

Environment: `SHARE_DIRECTORY` (default `/mnt/drive`; music root resolves to `$SHARE_DIRECTORY/music` unless `--music-dir` given). Requires `sacad` installed in the venv (`pnpm py:deps`). Cron: Sunday 04:45, flock-guarded, `--apply --overwrite-once --limit 300`.

### `check-smart-freshness.py`

Asserts that `scrutiny`'s SMART collector is still reporting, for `make verify-runtime` (daily cron, 06:15). Deliberately **not** in `check-invariants.sh`: a stale collector is a runtime fact, and the compose model can be perfectly correct while the collector has silently failed for a week — a monitoring tool that has stopped monitoring is worse than none, because the dashboard stays green.

```bash
scripts/check-smart-freshness.py                    # default: <24h, http://127.0.0.1:8086
scripts/check-smart-freshness.py --max-age-hours 6
```

Exit codes: `0` every known device reported inside the window, `1` a device is stale **or scrutiny knows about no devices at all**, `2` the API is unreachable.

**One device is the correct answer on this host.** The 9.1 TB USB media disk answers no SMART, so scrutiny covers the NVMe only — see ADR-0023 before "fixing" the device count. The zero-devices case is called out separately because it is what a wrong device passthrough looks like: hand scrutiny `/dev/nvme0n1` instead of `/dev/nvme0` and `smartctl --scan` returns empty, so it monitors nothing while the UI looks like a fresh install.

### `jellyfin_mem_sample.py`

Per-minute memory sampler for the ADR-0008 investigation. Cron `* * * * *`, self-rotating log at `logs/jellyfin-mem.log`, read by `stack_watchdog.py`'s anon-RSS threshold check.

**v4 (2026-09-02)** added what tells two different diseases apart. Upstream reports (jellyfin/jellyfin#16048, #16549) pin 10.11.x memory blowups on **ffprobe fan-out during library scans** rather than on a slow leak in the server process — two diseases, two cures, and v3 could not distinguish them, because a leak and a fan-out both look like "anon went up". So each sample now also records:

| field                  | why                                                                                                                                                                                                                                                                                       |
| ---------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ffprobe=` / `ffmpeg=` | live child processes. Scan-time extraction and transcodes both live here                                                                                                                                                                                                                  |
| `children_rss=`        | their summed RSS. These are charged to the **same cgroup** as the server, so they inflate `mem_current` while leaving the server's own `anon` flat — which is precisely how a fan-out is distinguishable from a leak, and precisely why it is invisible if you don't count them           |
| `scanning=yes\|no\|NA` | whether a library-scan task is actually running, from the Jellyfin API's `ScheduledTasks`. The cron line says when a scan is _triggered_, not whether it is still going, and an overrunning scan is exactly the case worth catching. `NA` rather than a guess when the API is unreachable |

Reads `API_KEY_JELLYFIN_ARR` (falls back to `API_KEY_JELLYFIN`); with neither, `scanning=NA` and everything else still samples.

### `stack_watchdog.py`

The thing that shouts. Nothing on this box reported failure before it existed: Jellyfin was OOM-killed five times in 48 hours and only surfaced because an episode stuttered, qBittorrent sat dead for fourteen hours twice and was found by accident, `autoheal` was absent from the stack for over a month, and the `media_ops_status.py` cron had been silently writing nothing since June (a missing `cd` in the crontab line). Four checks, one push notification.

1. **Every compose service** exists, is running, and is not `unhealthy` — compared against `docker compose config --services`, so a service defined but _never created_ is caught too. "Not unhealthy" and "not there at all" look identical to anything that only inspects running containers.
2. **Restart churn** — a climbing `RestartCount` between runs is flapping even if the container is "up" at the moment the check fires. First run never alerts (no baseline).
3. **Jellyfin anon-RSS** from `logs/jellyfin-mem.log` (see `jellyfin_mem_sample.py`). `anon` is the OOM-relevant number; `memory.current`/`mem_peak` include page cache and read high for benign reasons. A stale (>15 min) or `SAMPLE_FAILED` sampler is itself an alert — a monitor that quietly stops is worse than none.
4. **Kernel OOM kills** from `journalctl -k` (readable unprivileged here; `dmesg` is not, under `kernel.dmesg_restrict`). Catches kills of _any_ process, including ones that leave no trace in a container's log.
5. **The media drive**, because nothing else can watch it — `scrutiny` included. All ~4.7 TB lives on a single USB external disk with no redundancy, and its bridge refuses SMART under every `smartctl -d` type (re-verified 2026-09-02 from inside the scrutiny container too, with the capability granted and the device passed in). So this watches the only signals that do exist: the mount vanishing, ext4 remounting **read-only** (its default on error — at which point every \*arr import fails while the stack still looks healthy), free space, kernel I/O / USB-reset errors, and **ext4's own superblock error counter** via `tune2fs -l`. That last one is the durable signal: the kernel-log sweep covers 6 h and this host's journal retains only ~3 days, so an older error is invisible to both — while ext4 still reports it, because it lives in the superblock. Two traps, both unit-tested: `Filesystem state` is compared for **equality** with `clean` (a substring test passes during `clean with errors`, i.e. during the failure it guards against), and a **missing** `FS Error count` is the healthy state, because `tune2fs` omits the field when it is zero. ADR-0023.
6. **`autoheal` itself** — running, actually supervising something, and its restarts succeeding. A supervisor going quiet is invisible by construction: everything it watches stays healthy, so the only symptom is an absence.
7. **That an off-box heartbeat is configured at all.** Nothing running on this host can report that this host is down.
8. **The crontab, textually** — a line using a relative path without a `cd` into the repo cannot work from cron's `$HOME`, and a line naming a script that does not exist never will. Both are invisible in the job's output because there is none.
9. **Every wrapped cron job** has succeeded within the window its own cron line declares (see `cron_job.py`).

Delivery is **ntfy** — a plain `POST <topic-url>` with a text body is the whole publish contract, it needs no server-side application/token setup before the first alert can land, and this repo already spoke it (`SLSKD_ALERT_WEBHOOK`). Gotify would need a server plus per-application tokens first. The instance is self-hosted (compose service `ntfy`, `ntfy.<PUBLIC_DOMAIN>` via SWAG) so alert contents never leave the box; only the phone's subscription egresses. **Coverage limit:** the watchdog and ntfy run on the same host, so neither can tell you the host itself is down — that needs an off-box heartbeat, deliberately not built.

State in `logs/stack_watchdog.json` de-duplicates: a new problem notifies immediately, a continuing one re-notifies every `--repeat-min` (default 60), and a recovery sends a `RESOLVED:` note.

```bash
python scripts/stack_watchdog.py                       # check and notify
python scripts/stack_watchdog.py --dry-run             # print alerts, send nothing
python scripts/stack_watchdog.py --self-test           # prove delivery works
python scripts/stack_watchdog.py --jellyfin-anon-mb 2048 --repeat-min 30
python scripts/stack_watchdog.py --ignore recyclarr    # skip a service entirely
```

Exit codes: `0` all healthy, `1` at least one alert active, `2` fatal (docker unreachable). Cron runs it `*/5` — the push is the delivery path, `logs/stack_watchdog.log` is the record. Environment: `NAS_ALERT_WEBHOOK` (falls back to `SLSKD_ALERT_WEBHOOK`), `NAS_ALERT_USER`, `NAS_ALERT_PASSWORD`.

**The ntfy instance runs three accounts, not one.** `watchdog` (what these scripts use) is **write-only** — it cannot read the topic, so a leak of this box's `.env` exposes no alert history. `phone` is **read-only** — the credential typed into an Android app and backed up to Google cannot inject fake alerts. `admin` is for the web UI. Anonymous access is denied for both reading and writing. Web Push is enabled so a browser notifies with no tab open; `NTFY_UPSTREAM_BASE_URL` is deliberately unset, because its only purpose is waking iOS devices through ntfy.sh's APNs relay, which would send a hash of every topic off the box.

### `jellyfin_library_scan.py`

Scans **one** Jellyfin library. Jellyfin's built-in "Scan Media Library" (`RefreshLibrary`) task is global-only — there is no way to schedule TV Shows weekly while keeping Music daily — and a library scan is the largest memory event on this box (~1.56 GB peak `anon`-RSS). This drives the per-library endpoint instead, so cron can give each library its own cadence and the global task's trigger list can be emptied.

The call is `POST /Items/{virtualFolderItemId}/Refresh?metadataRefreshMode=Default&imageRefreshMode=Default&replaceAllMetadata=false&replaceAllImages=false&recursive=true` — the same one Jellyfin's own per-library "Scan library" button makes. `Default` mode fetches metadata for _new_ items only, so it is safe on a schedule. Verified end-to-end: a file dropped straight onto disk was invisible to Jellyfin, and one run of this script made it appear.

**There is deliberately no `--wait`.** On 10.11.11 a per-item refresh does _not_ drive the `RefreshLibrary` scheduled task's state (it stays `Idle` throughout) and `BaseItemDto` exposes no refresh-progress field, so there is no honest REST signal to poll. A zero exit means _accepted_, not _finished_.

```bash
python scripts/jellyfin_library_scan.py --list
python scripts/jellyfin_library_scan.py --library "TV Shows"
python scripts/jellyfin_library_scan.py --library Movies --library Music
python scripts/jellyfin_library_scan.py --all --dry-run
```

Exit codes: `0` all accepted, `1` partial, `2` fatal. Environment: `API_KEY_JELLYFIN`, `JELLYFIN_HOST`. Cron: Movies Fri 05:05, TV Shows Sat 05:05, Music Sun 05:05 (Music deliberately after the 04:45 `album_art.py` pass, because sacad writes `folder.jpg` straight to disk where no \*arr ever reports it).

### `lidarr_jellyfin_bridge.py`

Makes Lidarr's imports actually reach Jellyfin. Lidarr's own "Update Library" connection _does_ fire and Jellyfin _does_ answer `204` — and it is still a silent no-op, which is exactly what the 204 hides. Lidarr reports `/music/...` paths; Jellyfin's Music library is `/data/movies/music/...`; `Library/Media/Updated` drops a path that resolves to no library without logging anything. Proven by A/B against the live server:

```text
POST {"Updates":[{"Path":"/music/Bathory/1988 - Blood Fire Death"}]}
  -> 204, no LibraryMonitor line, Jellyfin keeps the stale metadata
POST {"Updates":[{"Path":"/data/movies/music/Bathory/1988 - Blood Fire Death"}]}
  -> 204, 'LibraryMonitor: "Blood Fire Death" ... will be refreshed', updated
```

Sonarr and Radarr have `mapFrom`/`mapTo` fields on their MediaBrowser connection for this and they are now set. **Lidarr's does not expose them**, so this script is the mapping, applied outside Lidarr: poll Lidarr's history for `trackFileImported|Renamed|Retagged|Deleted`, take the album folder of each path, translate the prefix, and report it to Jellyfin. It reports the _album folder_, not the library root, so Jellyfin does a targeted refresh rather than the 1.56 GB whole-library walk. History is paged backwards to the cursor rather than trusting one page — one album alone produces ~25 records, so a single 200-record page does not reliably cover five minutes during a backlog drain.

```bash
python scripts/lidarr_jellyfin_bridge.py --dry-run --since-min 120
python scripts/lidarr_jellyfin_bridge.py
python scripts/lidarr_jellyfin_bridge.py --map-from /music --map-to /data/movies/music
```

Exit codes: `0` nothing to do or all reported, `1` partial (some folders outside the mapped root), `2` fatal. Environment: `API_KEY_LIDARR`, `API_KEY_JELLYFIN`, `LIDARR_HOST`, `JELLYFIN_HOST`. Cron: `2-59/5` (offset from the watchdog's `*/5`), flock-guarded.

### `aac_fallback_track.py`

Adds a **default** stereo AAC track to DTS-only media so browsers Direct Play it. Browsers cannot decode DTS and cannot play MKV, so a DTS-only file makes Jellyfin remux the container _and_ transcode the audio for every web client — the exact path the Fargo stutter came down.

The non-obvious half, and the reason a naive "just add an AAC track" pass changes nothing: **Jellyfin's StreamBuilder evaluates the default audio stream.** With DTS still flagged default, `PlaybackInfo` returns `SupportsDirectPlay: false` even with a good AAC track present. Measured on Fargo S01E01 against the live server with a Chrome device profile:

```text
before: DirectPlay=False DirectStream=False   audio: [dts 6ch DEFAULT]
after:  DirectPlay=True  DirectStream=True    audio: [dts 6ch, aac 2ch DEFAULT]
```

So the script always does both: append the track _and_ move the default disposition. **Trade-off:** a client that could handle DTS 5.1 — the living-room TV app — now gets stereo unless the viewer picks the surround track. The DTS stream is still there, byte-identical and first in the file, just no longer default.

Nothing is destroyed: the untouched original is moved to `<SHARE>/backups/aac-remux-originals/` with its path under the media root preserved, so a revert is a plain `mv` back. Conversion is staged outside the library (a half-written `*.mkv` in the media folder is something Jellyfin or Sonarr could pick up mid-conversion) and the staged file is re-probed before the original is touched. Files whose default audio is already browser-safe are skipped, so re-running is idempotent. There is no host ffmpeg, so by default it runs Jellyfin's own binary out of the Jellyfin image (`--ffmpeg`/`--ffprobe` override that).

```bash
python scripts/aac_fallback_track.py --root "$SHARE_DIRECTORY/series/Fargo/Season 1"   # dry run
python scripts/aac_fallback_track.py --root ... --limit 10 --apply
python scripts/aac_fallback_track.py --file /mnt/drive/series/X/Y.mkv --apply --limit 1
```

Exit codes: `0` all processed, `1` partial, `2` fatal. Environment: `SHARE_DIRECTORY`, `PUID`, `PGID`. Not cron-driven — run it deliberately, in batches, and check the result.

### `cron_job.py`

Wraps a cron job so that **both** halves of failure are loud. A job that runs and exits fatal pushes an ntfy alert naming itself, its exit code and the tail of its stderr. A job that _stops running at all_ produces no output to notice, so each run also writes `logs/cron-state/<name>.json` and `stack_watchdog.py` alerts when a job has not succeeded within the window its own cron line declares. That second half is what would have caught `media_ops_status.py`, dead since 2026-06-10 because its line ran `.venv/bin/python` with no `cd` — it failed at launch, produced nothing, and the ops dashboard served June data for three months.

`--register` writes the state file without running anything, so a job that has never run once is still watched (staleness is measured from registration until the first success replaces it).

**Exit codes are not "0 good, non-zero bad" here.** This repo's contract is 0 success / 1 partial / 2 fatal, and several scripts report a real finding as 1 — `slskd_login_watch.py` (logged out), `stack_watchdog.py` (alert active), `media_ops_status.py` (DEGRADED). So `--ok-codes` defaults to `0,1`; plain shell commands pass `--ok-codes 0`. The wrapper exits 0 for an acceptable code and re-raises the job's own code otherwise, which keeps cron's mail quiet for expected partials without swallowing real failures.

**Put the wrapper inside `flock`, not outside.** `flock -n` exits 1 without running anything when the lock is held; outside the wrapper that would be recorded as a successful run and would refresh the freshness clock for a job that never executed.

```bash
python scripts/cron_job.py --name media-ops-status --max-age-min 30 -- \
    .venv/bin/python scripts/media_ops_status.py --json-out /path/out.json
python scripts/cron_job.py --name docker-prune --max-age-min 10380 --ok-codes 0 -- /bin/sh -c '...'
python scripts/cron_job.py --name album-art --max-age-min 10380 --register
```

All 23 scheduled jobs are wrapped. `jellyfin_mem_sample.py` deliberately is not — `stack_watchdog.py` already alerts on a stale or failing sampler, and double-alerting one job is noise.

### `heartbeat.py`

The off-box half of the alerting. `stack_watchdog.py` and the ntfy instance both run **on** the host they watch, so a powered-off box, a wedged kernel, a dead NIC or a stopped cron daemon all produce the same thing: silence, indistinguishable from "all fine". This inverts it — the box reports that it is alive to an external dead-man's switch (healthchecks.io free tier), and that service raises the alarm when the reports stop.

**A missed heartbeat cannot tell you what broke** — host, network, cron, or this script. That is fine and it is the point: the external service notices silence; everything attributable is already attributed from the inside.

It does one thing more than a bare `curl`: before reporting "alive" it checks that `stack_watchdog` has succeeded recently, and pings `/fail` instead if not. A box that is up and networked with dead monitoring would otherwise keep the heartbeat green forever — the same circular gap that hid `autoheal` for a month.

Ping failure is loud on purpose: a wrong URL (HTTP 400 from hc-ping.com, verified) or a DNS failure exits 2, which `cron_job.py` pushes. A silently-failing heartbeat is worse than none, because the dashboard elsewhere stays green.

```bash
python scripts/heartbeat.py --dry-run
python scripts/heartbeat.py
```

Exit codes: `0` pinged alive; `1` a reported condition — no URL configured yet, or the local check failed and `/fail` was pinged; `2` the ping could not be delivered. Environment: `NAS_HEARTBEAT_URL`. Cron: `*/10`, wrapped. Until the URL is set, `stack_watchdog.py` raises a standing `heartbeat:unconfigured` warning so it cannot be quietly forgotten.

## 🧪 Testing & Linting

Python unit tests live in `scripts/tests/` and use `pytest` for structure plus the existing `test_scripts.py` smoke harness.

Run locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r scripts/requirements.txt
python scripts/test_scripts.py      # legacy environment/import checks
pytest -q scripts/tests             # unit tests (fast, isolated)
```

Ruff lint (static analysis & style):

```bash
ruff check scripts
```

## 📦 Dependency Management (Python)

The project intentionally uses minimal production dependencies. Current policy:

- `scripts/requirements.txt` lists minimum versions (`>=`) to allow security patch upgrades.
- CI installs the latest compatible versions each run (early warning of breaking changes).
- `pytest` and `ruff` are included as dev/test tools.

Recommended monthly (or after CVE notifications):

```bash
source .venv/bin/activate
pip install -U -r scripts/requirements.txt
pip list --outdated         # review major version jumps
python scripts/test_scripts.py && pytest -q scripts/tests
```

Optional: capture a point-in-time lock snapshot for rollback:

```bash
pip freeze > scripts/requirements.lock
git add scripts/requirements.lock
```

When updating dependencies, ensure all tests & lint pass locally before committing.

For additional debugging information, modify the scripts to include:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```
