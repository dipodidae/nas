# Radarr Movie Naming Fix

## Problem

Movie folder names had literal pattern text like `{Movie Collection: - }` instead of being properly formatted. This happened after pattern changes in Radarr's naming configuration.

## Root Cause

When Radarr's folder naming pattern is changed, existing folders are NOT automatically renamed unless explicitly triggered. The database retained old broken folder names.

## Solution Implemented

### 1. Fixed Folder Names

Script: `fix_radarr_folders.py`

- Removes literal pattern text from folder names
- Adds proper " - " separator between collection and movie title
- Safe: checks for conflicts before renaming

### 2. Validation Tool

Script: `validate_radarr_naming.py`

- Verifies Radarr naming configuration matches expected format
- Scans for any folders with literal pattern text
- Run after any Radarr config changes

## Expected Configuration

**Folder Format:**

```
{Movie Collection}{Movie Collection: - }{Movie CleanTitleThe} ({Release Year})
```

**How it works:**

- `{Movie Collection}` - Shows "James Bond Collection" if movie is in a collection
- `{Movie Collection: - }` - Shows " - " separator ONLY if collection exists
- `{Movie CleanTitleThe}` - Movie title with "The" moved to end
- `({Release Year})` - Year in parentheses

**Example Results:**

- Collection movie: `James Bond Collection - Casino Royale (2006)`
- Standalone movie: `Lords of Chaos (2018)`

## Usage

### Fix Broken Folders

```bash
python3 scripts/fix_radarr_folders.py
```

### Validate Configuration

```bash
export $(grep -v '^#' .env | xargs)
python3 scripts/validate_radarr_naming.py
```

### After Config Changes in Radarr UI

1. Validate config: `python3 scripts/validate_radarr_naming.py`
2. If folders are broken: `python3 scripts/fix_radarr_folders.py`
3. Trigger Radarr rescan via UI or API:
   ```bash
   curl -H "X-Api-Key: $API_KEY_RADARR" \
     http://localhost:7878/api/v3/command \
     -X POST -H "Content-Type: application/json" \
     -d '{"name":"RescanMovie","movieIds":[]}'
   ```

## Prevention

- Don't change naming patterns in Radarr UI unless necessary
- When changing, immediately run validation and fix scripts
- Use the validation script in monitoring/CI to catch issues early
