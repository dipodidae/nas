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

1. **Lidarr-quiet gate (per-transfer)** — build the set of dir basenames referenced by *active* Lidarr queue items (`downloading` / `importPending` / `importing` / `importBlocked` / `importFailed`). A `Completed, Succeeded` slskd record is deferred only if its trailing-segment basename appears in that set. Succeeded records that no Lidarr item references are cleaned in the same run, so the slskd transfer manager doesn't accumulate indefinitely just because *some other* import is in flight. Terminal-failure states (`Completed, Errored / Rejected / Cancelled / TimedOut`) are always cleaned — they will never trigger a Tubifarry callback.
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

Removes Lidarr queue items wedged in `completed / importFailed` state. These accumulate when slskd peers deliver a download that Lidarr can't accept — typically `Album release not requested` (peer sent a different MusicBrainz release than the one Lidarr asked for) or `Album match is not close enough: X% vs 80%`. Lidarr has no built-in "remove failed import after N hours" setting; `autoRedownloadFailed` only fires for download-side failures, so these rows live forever, block Tubifarry from clearing the matching slskd transfer, and gum up the whole pipeline. Run hourly to keep throughput high.

For each eligible row the script issues `DELETE /api/v1/queue/{id}?removeFromClient=true&blocklist=true&skipRedownload=false`. That drops the queue entry, kills the slskd transfer record via Tubifarry, blocklists the specific release so it isn't re-grabbed, and asks Lidarr to search for a different release.

Safety design:

1. **State gate** — only rows with `trackedDownloadState == importFailed` are touched. Downloading / importing rows are left strictly alone.
2. **Age gate** — only deletes rows whose `added` timestamp is older than `--min-age-hours` (default `1`). Rows missing `added` are skipped conservatively.

```bash
python scripts/lidarr_queue_unstick.py                       # clean now (1h age gate)
python scripts/lidarr_queue_unstick.py --dry-run             # report only
python scripts/lidarr_queue_unstick.py --min-age-hours 0     # immediate (manual one-off)
python scripts/lidarr_queue_unstick.py --skip-redownload     # blocklist without auto re-search
python scripts/lidarr_queue_unstick.py --no-blocklist        # remove only (not recommended — Tubifarry will re-grab the same junk)
```

Exit codes: `0` success or nothing to do, `1` partial (some deletes failed), `2` fatal (config / network / HTTP error).

Environment: `API_KEY_LIDARR` (required), `LIDARR_HOST` (default `http://localhost:8686`).

### `slskd_complete_sweep.py`

Reaps `/downloads/complete/slskd/<dir>` subtrees that Lidarr has already imported into `/music/`. With Lidarr configured to use hardlinks (`copyUsingHardlinks=true`), the slskd download copy is never reaped by the import path itself; over weeks this accumulates GBs of duplicates of files that already live in the music library.

Match strategy is size-based against a one-shot walk of `/music/`. By default `--threshold 1.0` requires that *every* audio file in the slskd dir has a matching size in the library — false-positive risk is essentially zero. Dirs the Lidarr queue still references are skipped (mid-flight protection), and an age gate keeps freshly downloaded folders alone for the first hour.

```bash
python scripts/slskd_complete_sweep.py                    # delete confirmed dups
python scripts/slskd_complete_sweep.py --dry-run          # report only
python scripts/slskd_complete_sweep.py --threshold 0.9    # require ≥90% of files match
python scripts/slskd_complete_sweep.py --limit 20         # cap deletions per run
```

Exit codes: `0` success / nothing to do, `1` partial (some rmtree failed), `2` fatal (config / Lidarr unreachable / no /music).

Environment: `API_KEY_LIDARR` (required), `LIDARR_HOST` (default `http://localhost:8686`), `MUSIC_DIR` (default `/mnt/drive/music`), `SLSKD_COMPLETE_DIR` (default `/mnt/drive/downloads/complete/slskd`).

Note: this script reaps confirmed duplicates only. Folders Lidarr *rejected* (peer mismatch, fingerprint below 80%) never get hardlinked into `/music/` and so look like orphans here — they are real orphans, but not duplicates, and need a separate decision (re-import via `process_soulseek_imports.py`, or manual triage). The sweeper deliberately leaves them alone.

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
```

Pi-era host-tuning shell scripts and their docs live under `scripts/legacy/` — they are reference-only and not safe to run on the MS01 host (see `scripts/legacy/README.md`).

#### Concurrency and race-safety

The two cleanup scripts target overlapping state (a Lidarr `importFailed` queue item is backed by a `Completed, Succeeded` slskd transfer) and could in principle fight each other or fight Tubifarry's own import flow. They are designed not to:

1. **Mutex via `flock`** — both cron entries share `/tmp/nas-tubifarry-cleanup.lock` with `flock -n`, so a second invocation while the first is running exits immediately (next hour will pick it up). This covers cron overrun *and* manual one-offs that fire during a scheduled run.
2. **Schedule ordering** — `lidarr_queue_unstick` runs first (`:07`), `slskd_cleanup` second (`:37`). Within each hour the Lidarr→Tubifarry→slskd path drains first, then the direct slskd sweep mops up what was never tied to Lidarr (errored peer transfers, rejected handshakes, cancelled grabs).
3. **Per-transfer deferrals in `slskd_cleanup`** — a `Completed, Succeeded` row is deferred only if its dir basename matches one referenced by an active Lidarr queue item (`downloading / importPending / importing / importBlocked / importFailed`). Tubifarry's own import flow owns the first four; `lidarr_queue_unstick` owns the fifth. Either way `slskd_cleanup` declines to race a Lidarr-side actor for *that specific* transfer's path. Unmatched Succeeded rows (Lidarr already imported and dropped them) are cleaned in the same run. Terminal-failure slskd states (`Completed, Errored / Rejected / Cancelled`) have no Lidarr-side actor and are always safe to clean.
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
