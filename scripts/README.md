# NAS Automation Scripts

This directory contains utility scripts for automating various tasks in the media server stack.

## 📋 Available Scripts

### Prowlarr Priority Management

#### `prowlarr-priority-setter.py`

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
  python scripts/prowlarr-priority-setter.py --dry-run  # Preview changes
  python scripts/prowlarr-priority-setter.py           # Apply changes (⚠️ may hang)
  ```

- **Known Issues**:
  - PUT requests to Prowlarr API may hang indefinitely
  - Appears to be a Prowlarr API bug with complex object serialization

#### `prowlarr-priority-checker.py` ✅ **Recommended**

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
  python scripts/prowlarr-priority-checker.py
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
python scripts/prowlarr-priority-checker.py
```

## 🚀 Recommended Workflow

1. **Analyze Current State**:

   ```bash
   python scripts/prowlarr-priority-checker.py
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
   python scripts/prowlarr-priority-checker.py
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

### `prowlarr-priority-setter.py` (Advanced)

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

### `prowlarr-priority-checker.py` (Simple & Reliable)

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

### Integration

All new scripts are included in `test_scripts.py` for import validation. Add cron/systemd timers as needed, for example:

```
# Daily 01:00 backup & prune
0 1 * * * /usr/bin/env bash -c 'cd /home/<username>/nas && . .venv/bin/activate && python scripts/config_backup.py >> backups/backup.log 2>&1'

# Hourly post-update verification (or triggered by Watchtower hook)
0 * * * * /usr/bin/env bash -c 'cd /home/<username>/nas && . .venv/bin/activate && python scripts/post_update_verifier.py >> logs/verify.log 2>&1'
```

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
