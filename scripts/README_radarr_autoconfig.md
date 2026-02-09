# Radarr AutoConfig Script - Naming Protection

## The Culprit Identified

**AutoConfig Script**: `/mnt/docker-usb/radarr/custom-services.d/AutoConfig`

This script runs at Radarr startup and updates naming configuration from:

1. **Custom file** (if exists): `/config/extended/naming.json` ✅ SAFE
2. **OR TRaSH Guides** (if custom doesn't exist): Downloads from GitHub ⚠️ DANGEROUS

## Current State - PROTECTED ✅

Your custom `naming.json` exists and is being used:

```json
{
  "folder": {
    "default": "{Movie Collection}{Movie Collection: - }{Movie CleanTitleThe} ({Release Year})"
  }
}
```

**This is the CORRECT format** that:

- Shows collection name: `Star Wars Collection`
- Adds separator ONLY if collection exists: `-`
- Shows movie title: `The Force Awakens`
- Shows year: `(2015)`

Result: `Star Wars Collection - Star Wars The Force Awakens (2015)`

## The ACTUAL Root Cause - AutoConfig's autoRenameFolders

**UPDATE Feb 6, 2026**: The REAL culprit was found!

### The Two-Part Problem

1. **Naming Pattern** (CORRECT since Feb 3):

   ```
   {Movie Collection}{Movie Collection: - }{Movie CleanTitleThe} ({Release Year})
   ```

2. **Auto-Rename Folders** (WAS FALSE until Feb 6):
   ```json
   "autoRenameFolders": false
   ```

### What `autoRenameFolders: false` Does

When FALSE, Radarr takes the naming pattern LITERALLY and doesn't process conditional tags:

- Pattern: `{Movie Collection: - }`
- Expected: Shows " - " only if collection exists
- With autoRenameFolders=false: Creates folder with literal text `{Movie Collection: - }`

Result: `Indiana Jones Collection{Movie Collection: - }Last Crusade (1989)`

### The Fix

Changed in `/mnt/docker-usb/radarr/custom-services.d/AutoConfig` line 50:

```bash
# OLD (broken):
"autoRenameFolders":false

# NEW (correct):
"autoRenameFolders":true
```

Now when movies are imported, Radarr:

1. Takes the naming pattern
2. PROCESSES conditional tags (like `{Movie Collection: - }`)
3. Creates folders with proper names

### Enforcement Script

`scripts/enforce_radarr_settings.py` now monitors BOTH:

- Correct naming pattern in naming.json
- `autoRenameFolders: true` in media management

Run daily via cron to catch if AutoConfig or manual changes break it again.

From `/mnt/docker-usb/radarr/extended.conf`:

```bash
configureNaming="true"  # Enables naming updates at startup
```

From AutoConfig script (lines 21-30):

```bash
if [ -f /config/extended/naming.json ]; then
    log "Using custom Naming (/config/extended/naming.json)..."
    namingJson=$(cat /config/extended/naming.json)
else
    log "Getting Trash Guide Recommended Naming..."
    namingJson=$(curl -s "https://raw.githubusercontent.com/TRaSH-/Guides/master/docs/json/radarr/naming/radarr-naming.json")
fi
```

**THE PROBLEM**: If `naming.json` ever gets deleted, AutoConfig will download TRaSH guide format which is:

```
{Movie CleanTitle} ({Release Year})
```

This format has NO collection support - all movies would lose collection prefixes!

## Protection Measures Implemented

### 1. Read-Only Backup

```bash
/mnt/docker-usb/radarr/extended/naming.json.locked (read-only)
```

If `naming.json` gets corrupted, restore from `.locked` file.

### 2. Validation Script

```bash
python3 scripts/validate_radarr_autoconfig_naming.py
```

Checks that Radarr's active naming matches `naming.json`.
Run this daily via cron to catch drift.

### 3. Extended Config Settings

File: `/mnt/docker-usb/radarr/extended.conf`

Current settings:

- `enableAutoConfig="true"` ✅ Keep enabled (manages other good settings)
- `configureNaming="true"` ✅ Keep enabled (uses custom naming.json)

**DO NOT** set `configureNaming="false"` - this would prevent AutoConfig from fixing naming if it drifts.

## Recovery Procedures

### If naming.json Gets Deleted

```bash
# Restore from locked backup
cp /mnt/docker-usb/radarr/extended/naming.json.locked \
   /mnt/docker-usb/radarr/extended/naming.json

# Restart Radarr to apply
docker restart radarr
```

### If AutoConfig Changes Naming

```bash
# 1. Check what happened
docker logs radarr 2>&1 | grep -i "autoconfig.*naming"

# 2. Restore naming.json
cp /mnt/docker-usb/radarr/extended/naming.json.locked \
   /mnt/docker-usb/radarr/extended/naming.json

# 3. Fix Radarr config via API
curl -s http://localhost:7878/api/v3/config/naming \
  -X PUT -H "X-Api-Key: $API_KEY_RADARR" \
  -H "Content-Type: application/json" \
  --data-raw '{
    "renameMovies":true,
    "replaceIllegalCharacters":true,
    "colonReplacementFormat":"delete",
    "standardMovieFormat":"{Movie CleanTitle} {(Release Year)} {imdb-{ImdbId}} {edition-{Edition Tags}} {[Custom Formats]}{[Quality Full]}{[MediaInfo 3D]}{[MediaInfo VideoDynamicRangeType]}{[Mediainfo AudioCodec}{ Mediainfo AudioChannels}]{MediaInfo AudioLanguagesAll}[{MediaInfo VideoBitDepth}bit][{Mediainfo VideoCodec}]{MediaInfo SubtitleLanguagesAll}{-Release Group}",
    "movieFolderFormat":"{Movie Collection}{Movie Collection: - }{Movie CleanTitleThe} ({Release Year})",
    "id":1
  }'

# 4. Fix any broken folder names
python3 scripts/fix_radarr_folders.py
python3 scripts/fix_radarr_db_paths.py
docker restart radarr
```

## Prevention - Daily Monitoring

Add to crontab:

```bash
# Daily validation at 5 AM
0 5 * * * cd /home/tom/nas && \
  export $(grep -v '^#' .env | xargs) && \
  python3 scripts/validate_radarr_autoconfig_naming.py || \
  echo "ALERT: Radarr naming drift detected" | mail -s "Radarr Naming Alert" dpdd@squat.net
```

## Files to NEVER Delete

1. `/mnt/docker-usb/radarr/extended/naming.json` - Active naming config
2. `/mnt/docker-usb/radarr/extended/naming.json.locked` - Protected backup
3. `/mnt/docker-usb/radarr/extended/naming.json.backup` - Previous version

## Understanding the Format

```
{Movie Collection}{Movie Collection: - }{Movie CleanTitleThe} ({Release Year})
```

Breakdown:

- `{Movie Collection}` - Shows "Star Wars Collection" if movie is in a collection, otherwise empty
- `{Movie Collection: - }` - CONDITIONAL: Shows " - " ONLY if {Movie Collection} has content
- `{Movie CleanTitleThe}` - Movie title with "The" moved to end (e.g., "Force Awakens, The" becomes "The Force Awakens")
- `({Release Year})` - Year in parentheses

**Why This Format?**

- Collections grouped together in file browser
- Easy to identify collection movies vs standalone
- Jellyfin can parse collection info
- Clean, consistent naming

## Verification Commands

```bash
# Check if naming.json exists
ls -la /mnt/docker-usb/radarr/extended/naming.json*

# Check current Radarr naming
curl -s -H "X-Api-Key: $API_KEY_RADARR" \
  http://localhost:7878/api/v3/config/naming | \
  jq -r '.movieFolderFormat'

# Run validation
python3 scripts/validate_radarr_autoconfig_naming.py

# Check for broken folder names
ls /mnt/drive-next/Movies/ | grep "{"
```

## Summary

✅ **CURRENT STATUS**: Protected and working correctly
✅ **AUTOCONFIG**: Using custom naming.json (safe)
✅ **BACKUPS**: Multiple copies of correct config
✅ **MONITORING**: Validation script ready for cron

**The AutoConfig script is NOT the enemy** - it's actually helpful for maintaining config.
The issue was understanding HOW it works and ensuring the custom naming.json exists.
As long as that file exists, AutoConfig uses it instead of downloading external formats.

## Setting Up Daily Validation

Add to crontab to run every day at 6 AM:

```bash
crontab -e
```

Add this line:

```bash
# Validate Radarr naming settings daily
0 6 * * * cd /home/tom/nas && export $(grep -v '^#' .env | xargs) && python3 scripts/enforce_radarr_settings.py || echo "ALERT: Radarr settings drift" | mail -s "Radarr Alert" dpdd@squat.net
```

This will:

- Check naming pattern matches naming.json
- Ensure autoRenameFolders is TRUE
- Auto-fix if drift detected
- Email you if it finds/fixes issues
