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
  cd /home/tom/nas
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
  cd /home/tom/nas
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

### Priority Configuration

Both scripts use the same priority mapping defined in `INDEXER_PRIORITIES`:

```python
INDEXER_PRIORITIES = {
    "YTS": 1,                    # Highest priority
    "SubsPlease": 5,
    "showRSS": 10,
    "The Pirate Bay": 15,
    "TorrentGalaxyClone": 21,
    "Solid Torrents": 25,
    "Torrent Downloads": 28,
    "Torrent9": 30,
    "TorrentDownload": 33,
    "OxTorrent": 35,
    # ... more indexers
    "VSTorrent": 50,             # Lowest priority
}
```

**Priority Scale**: 1-50 (1 = highest priority, 50 = lowest priority)

## 🔧 Setup Instructions

### Prerequisites

1. **Python Environment**:

   ```bash
   cd /home/tom/nas
   python -m venv .venv
   source .venv/bin/activate  # Linux/Mac
   # or .venv\Scripts\activate  # Windows
   ```

2. **Install Dependencies**:

   ```bash
   pip install -r scripts/requirements.txt
   # or manually: pip install requests python-dotenv
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

For additional debugging information, modify the scripts to include:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```
