# Ebook Deduplication - Quick Start

## 🚀 Three Commands You Need

### 1️⃣ Scan (Safe - No Changes)

```bash
cd /home/tom/nas
python3 scripts/deduplicate_ebooks_filesystem.py --scan-only
```

**What it does:** Shows you all duplicates without touching anything.

---

### 2️⃣ Dry-Run (Preview Changes)

```bash
python3 scripts/deduplicate_ebooks_filesystem.py --dry-run
```

**What it does:** Shows exactly which files would be deleted.

---

### 3️⃣ Execute (Actually Clean)

```bash
python3 scripts/deduplicate_ebooks_filesystem.py
```

**What it does:** Deletes duplicates, keeps best formats, cleans Jellyfin.

---

## 📊 Your Library Status

**Total files:** 1,215
**Duplicates found:** 556 groups (~659 files)
**Space to free:** ~500-800 MB

---

## �� What Gets Kept

✅ **EPUB** (best for e-readers)
✅ **MOBI** (Kindle format)
✅ Files in `Author/Title/Book.epub` structure

## 🗑️ What Gets Deleted

❌ **PDF** when EPUB exists
❌ Loose duplicates
❌ Temp files like "Book (1).epub"
❌ Metadata (.opf, .jpg)

---

## 📝 Check Results

```bash
# View the log
less /home/tom/nas/logs/ebook_filesystem_dedup_*.log

# View summary
tail -30 /home/tom/nas/logs/ebook_filesystem_dedup_*.log
```

---

## 🔧 Options

| Command           | What it does          |
| ----------------- | --------------------- |
| `--scan-only`     | Just scan, no changes |
| `--dry-run`       | Preview changes       |
| `--no-jellyfin`   | Skip Jellyfin cleanup |
| `--jellyfin-only` | Only clean Jellyfin   |
| _(no flags)_      | Full execution        |

---

## 📚 Full Docs

Read the complete guide:

```bash
less /home/tom/nas/scripts/EBOOK_DEDUPLICATION_README.md
```

---

**Ready?** Start with step 1️⃣ (scan-only)!
