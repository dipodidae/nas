# Ebook Library Deduplication Scripts

This directory contains scripts to safely deduplicate and clean your ebook library.

## Overview

Two complementary scripts are provided:

### 1. `deduplicate_ebooks_filesystem.py` (Recommended)
**Filesystem-based deduplication** - Scans the Books directory for duplicate files.

**Features:**
- Finds duplicate ebooks by matching normalized title + author
- Prefers non-PDF formats (EPUB > MOBI > AZW3 > PDF)
- Handles both LazyLibrarian-managed and loose files
- Cleans up Jellyfin library entries for missing files
- Removes orphaned LazyLibrarian database entries
- Safe dry-run and scan-only modes

### 2. `deduplicate_ebooks.py` (Database-based)
**LazyLibrarian database deduplication** - Only processes books tracked in LazyLibrarian's database.

**Note:** This won't catch filesystem-level duplicates that LazyLibrarian doesn't know about.

## Quick Start

### Step 1: Scan for Duplicates (Safe)
```bash
cd /home/tom/nas
python3 scripts/deduplicate_ebooks_filesystem.py --scan-only
```

This will:
- ✅ Scan for duplicates
- ✅ Show what would be kept/removed
- ✅ Estimate space savings
- ❌ NOT delete anything

### Step 2: Preview Changes (Dry-Run)
```bash
python3 scripts/deduplicate_ebooks_filesystem.py --dry-run
```

This will:
- ✅ Show exactly which files would be deleted
- ✅ Preview database updates
- ✅ Preview Jellyfin cleanup
- ❌ NOT make any actual changes

### Step 3: Execute Deduplication
```bash
python3 scripts/deduplicate_ebooks_filesystem.py
```

This will:
- ✅ Delete duplicate files (keeping best format)
- ✅ Remove associated metadata (.opf, .jpg)
- ✅ Clean up empty directories
- ✅ Update LazyLibrarian database
- ✅ Clean Jellyfin library

## Format Priority

When duplicates are found, files are ranked by format:

| Format | Priority | Notes |
|--------|----------|-------|
| EPUB   | 100      | Best for most readers |
| MOBI   | 90       | Kindle format |
| AZW3   | 85       | Enhanced Kindle |
| AZW    | 80       | Older Kindle |
| FB2    | 70       | Russian ebook format |
| CBZ    | 60       | Comic book archive |
| CBR    | 55       | Comic book archive |
| PDF    | **10**   | **Lowest priority** |
| TXT    | 5        | Plain text |

**Bonuses:**
- +5 points for files in structured directories (`Author/Title/Book.epub`)
- -10 points for temp files or weird filenames (e.g., `Book (1).epub`)

## Command Line Options

```bash
# Scan only - no changes
python3 scripts/deduplicate_ebooks_filesystem.py --scan-only

# Dry-run - preview changes
python3 scripts/deduplicate_ebooks_filesystem.py --dry-run

# Execute deduplication only (skip Jellyfin)
python3 scripts/deduplicate_ebooks_filesystem.py --no-jellyfin

# Clean Jellyfin only (skip deduplication)
python3 scripts/deduplicate_ebooks_filesystem.py --jellyfin-only

# Full execution (dedupe + Jellyfin cleanup)
python3 scripts/deduplicate_ebooks_filesystem.py
```

## What Gets Kept vs Deleted

### Example 1: EPUB vs PDF
```
Books/
  ├── Book Title - Author.pdf         [Priority: 15]  ❌ DELETED
  └── Author/
      └── Book Title/
          └── Book Title.epub         [Priority: 105] ✅ KEPT
```

### Example 2: Structured vs Loose
```
Books/
  ├── Book.epub                       [Priority: 100] ❌ DELETED (loose)
  └── Author/
      └── Book/
          └── Book.epub               [Priority: 105] ✅ KEPT (structured)
```

### Example 3: Multiple PDFs
```
Books/
  ├── Book.pdf                        [Priority: 15]  ❌ DELETED
  ├── Book (1).pdf                    [Priority: 5]   ❌ DELETED (temp name)
  └── Author/
      └── Book/
          └── Book.pdf                [Priority: 20]  ✅ KEPT (structured)
```

## Safety Features

### 1. Comprehensive Logging
Every run creates a detailed log file:
```
/home/tom/nas/logs/ebook_filesystem_dedup_execution_20260122_180000.log
```

### 2. Metadata Cleanup
When a book is deleted, associated files are also removed:
- `Book.epub` ← Main file
- `Book.opf` ← Metadata
- `Book.jpg` ← Cover image
- Empty parent directories

### 3. Database Sync
- LazyLibrarian database updated to remove orphaned entries
- Jellyfin library refreshed automatically

### 4. Idempotent
Safe to run multiple times. If you run it again, it will find 0 duplicates.

## Jellyfin Cleanup

The script also cleans the Jellyfin library:

### Removes:
- ❌ Entries pointing to deleted/missing files
- ❌ Metadata files (`.opf`, `.jpg`) incorrectly added as books
- ❌ Orphaned entries with no file path

### Keeps:
- ✅ Valid ebook files
- ✅ Books with existing files on disk

After cleanup, Jellyfin library is automatically refreshed.

## Expected Results

Based on the initial scan of your library:

```
Total files:       1,215
Duplicate groups:  556
Files to delete:   ~659
Space to free:     ~500-800 MB (estimated)
```

## Rollback/Recovery

### If you need to undo changes:

1. **Restore from LazyLibrarian backup:**
   ```bash
   # LazyLibrarian creates backups automatically
   ls -lt /mnt/docker-usb/lazylibrarian/*.tgz | head -5
   ```

2. **Check the log file** to see exactly what was deleted:
   ```bash
   grep "Deleted:" /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log
   ```

3. **Re-download from LazyLibrarian** if needed

### Prevention:
- Always run `--scan-only` first
- Then run `--dry-run` to preview
- Only then execute without flags

## Troubleshooting

### "No duplicates found" but you see duplicates
- The script normalizes titles aggressively
- Check the log for how titles are being normalized
- Files may have different enough names to not match

### "Database error"
- Ensure LazyLibrarian container is running
- Check database permissions

### "Jellyfin API error"
- Verify Jellyfin is running
- Check API key in script configuration

### Script runs but Jellyfin still shows garbage
- Run again with `--jellyfin-only`
- Manually trigger library scan in Jellyfin UI

## Configuration

Edit these values at the top of the script if your paths differ:

```python
LAZYLIBRARIAN_DB = Path("/mnt/docker-usb/lazylibrarian/lazylibrarian.db")
BOOKS_DIR = Path("/mnt/drive-next/Books")
JELLYFIN_API_URL = "http://localhost:8096"
JELLYFIN_API_KEY = "d0ba4efb1a664e2b8870363719c57939"
```

## Best Practices

1. **Always scan first:** `--scan-only`
2. **Review the scan results** in the log file
3. **Dry-run to preview:** `--dry-run`
4. **Execute in stages:**
   - First run: `--no-jellyfin` (just deduplication)
   - Verify results
   - Second run: `--jellyfin-only` (clean Jellyfin)

5. **Backup before major cleanups:**
   ```bash
   docker exec lazylibrarian tar -czf /config/manual_backup_$(date +%Y%m%d).tgz /config/*.db
   ```

## Example Workflow

```bash
# 1. Scan to see what duplicates exist
python3 scripts/deduplicate_ebooks_filesystem.py --scan-only

# 2. Review the log
less /home/tom/nas/logs/ebook_filesystem_dedup_dry_run_*.log

# 3. Dry-run to see exact changes
python3 scripts/deduplicate_ebooks_filesystem.py --dry-run

# 4. If satisfied, execute
python3 scripts/deduplicate_ebooks_filesystem.py

# 5. Verify in Jellyfin
# Open Jellyfin > Books > Check for missing/garbage entries
```

## Post-Cleanup

After running the script:

1. **Refresh LazyLibrarian:**
   - Go to LazyLibrarian web UI
   - Settings > Restart
   - Or: `docker restart lazylibrarian`

2. **Verify Jellyfin:**
   - Dashboard > Libraries > Books > Scan Library
   - Check for any remaining garbage entries

3. **Review the log:**
   ```bash
   tail -100 /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log
   ```

## Support

For issues or questions:
1. Check the log file for detailed error messages
2. Run with `--dry-run` to debug without making changes
3. Verify all services are running: `docker ps`

---

**Last Updated:** 2026-01-22  
**Script Version:** 1.0  
**Tested With:** LazyLibrarian (latest), Jellyfin 10.11.6
