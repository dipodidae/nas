# Ebook Library Cleanup - Execution Plan

## Current Situation

### Filesystem (LazyLibrarian/Books)

- **Total ebook files:** 1,215
- **Duplicate groups:** 556 (almost 50% of library!)
- **Estimated space wasted:** 500-800 MB

### Jellyfin Library

- **Total book entries:** 1,192
- **Missing/broken entries:** 1,192 (100%!)
- **Cause:** Files moved, deleted, or never existed

## Solution Overview

Run the deduplication script which will:

1. **Deduplicate filesystem** - Remove 556 groups of duplicates
   - Keep EPUB/MOBI formats
   - Delete duplicate PDFs
   - Clean up metadata files
   - Remove empty directories

2. **Clean Jellyfin library** - Remove 1,192 broken entries
   - Delete entries pointing to missing files
   - Trigger library rescan
   - Jellyfin will re-index remaining valid books

3. **Sync LazyLibrarian database** - Update book tracking
   - Remove orphaned entries
   - Keep database in sync with filesystem

## Recommended Execution Steps

### Step 1: Preview (Dry-Run)

```bash
cd /home/tom/nas
python3 scripts/deduplicate_ebooks_filesystem.py --dry-run
```

**Expected output:**

- Shows ~659 files to be deleted
- Lists which files are kept vs removed
- Estimates space savings
- Shows Jellyfin cleanup plan

**Review:** Check the log file to ensure nothing important is being deleted:

```bash
less /home/tom/nas/logs/ebook_filesystem_dedup_dry_run_*.log
```

### Step 2: Execute Deduplication Only

```bash
python3 scripts/deduplicate_ebooks_filesystem.py --no-jellyfin
```

**What happens:**

- ✅ Deletes duplicate files
- ✅ Keeps best formats (EPUB > PDF)
- ✅ Updates LazyLibrarian database
- ❌ Skips Jellyfin (we'll do this separately)

**Verify:** Check that files were deleted correctly:

```bash
# Check Books directory
ls -lh /mnt/drive-next/Books/ | head -20

# Check log
tail -50 /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log
```

### Step 3: Clean Jellyfin Library

```bash
python3 scripts/deduplicate_ebooks_filesystem.py --jellyfin-only
```

**What happens:**

- ✅ Removes 1,192 broken/missing entries
- ✅ Triggers library refresh
- ✅ Jellyfin re-scans Books directory
- ✅ Only valid, existing books added back

**Verify in Jellyfin UI:**

1. Navigate to Dashboard → Libraries → Books
2. Click "Scan Library"
3. Check Books count (should be ~556 after dedup)
4. Browse books - no more missing/garbage entries

## Expected Final State

### After Deduplication

```
Before:                  After:
─────────────────        ──────────────────
1,215 total files    →   556 files (clean)
556 duplicate groups →   0 duplicates
Mixed EPUB/PDF       →   Primarily EPUB/MOBI
500-800 MB wasted    →   Space freed
```

### After Jellyfin Cleanup

```
Before:                      After:
──────────────────────       ────────────────────────
1,192 total entries      →   ~556 valid entries
1,192 missing/broken     →   0 broken entries
100% garbage             →   Clean, browseable library
```

## Safety Measures

### Automatic Backups

LazyLibrarian creates automatic database backups:

```bash
ls -lh /mnt/docker-usb/lazylibrarian/*.tgz
```

Most recent backup:

```
scheduled_Fri_Jan__2_16_41_28_2026.tgz  (2.7 MB)
```

### Rollback Procedure

If something goes wrong:

1. **Restore LazyLibrarian database:**

   ```bash
   cd /mnt/docker-usb/lazylibrarian
   tar -xzf scheduled_Fri_Jan__2_16_41_28_2026.tgz
   docker restart lazylibrarian
   ```

2. **Re-scan Jellyfin:**
   - Jellyfin UI → Dashboard → Libraries → Books → Scan
   - This will re-index whatever is on disk

3. **Check deletion log:**
   ```bash
   grep "Deleted:" /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log
   ```

**Note:** Files deleted cannot be auto-recovered. Review dry-run log carefully!

## Post-Cleanup Actions

### 1. Verify LazyLibrarian

```bash
# Restart to reload database
docker restart lazylibrarian

# Check web UI
# http://your-server:5299
```

### 2. Verify Jellyfin

```bash
# Check that library refresh completed
# Dashboard → Libraries → Books

# Browse books - should only see valid entries
```

### 3. Review Logs

```bash
# View execution summary
tail -100 /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log

# Check for any errors
grep -i error /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log
```

## Estimated Time

- **Dry-run:** ~2 minutes
- **Deduplication:** ~5-10 minutes (depending on disk I/O)
- **Jellyfin cleanup:** ~3-5 minutes
- **Jellyfin rescan:** ~2-5 minutes

**Total:** ~15-20 minutes

## One-Command Execution (Advanced)

If you want to do everything in one shot:

```bash
cd /home/tom/nas

# Full cleanup - deduplication + Jellyfin
python3 scripts/deduplicate_ebooks_filesystem.py

# Then verify
tail -50 /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log
```

**Warning:** This skips the separate verification steps. Recommended to do step-by-step the first time.

## Troubleshooting

### "Database locked" error

```bash
# Stop LazyLibrarian temporarily
docker stop lazylibrarian

# Run script
python3 scripts/deduplicate_ebooks_filesystem.py --no-jellyfin

# Restart LazyLibrarian
docker start lazylibrarian
```

### Jellyfin shows wrong count

```bash
# Manual library refresh
# Jellyfin UI → Dashboard → Libraries → Books → Scan All

# Or via API
curl -X POST -H "X-MediaBrowser-Token: d0ba4efb1a664e2b8870363719c57939" \
  "http://localhost:8096/Library/Refresh"
```

### Want to see what was deleted

```bash
# List all deleted files
grep "Deleted:" /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log

# Count deletions
grep -c "Deleted:" /home/tom/nas/logs/ebook_filesystem_dedup_execution_*.log
```

## Ready to Execute?

**Checklist before running:**

- [ ] Reviewed dry-run output
- [ ] Verified LazyLibrarian backup exists
- [ ] LazyLibrarian and Jellyfin are running
- [ ] Have at least 1 GB free disk space (for logging/processing)
- [ ] Understand what will be deleted (PDFs when EPUB exists, loose duplicates)

**If all checked, proceed with Step 1!**

---

**Questions? Review the full documentation:**

```bash
less /home/tom/nas/scripts/EBOOK_DEDUPLICATION_README.md
```
