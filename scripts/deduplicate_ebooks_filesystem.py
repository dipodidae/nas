#!/usr/bin/env python3
"""
Ebook Library Filesystem Deduplication and Cleanup Script

This script safely:
1. Scans the Books directory for duplicate ebook files (same title/author)
2. Prefers non-PDF formats (EPUB, MOBI, AZW3) over PDF when duplicates exist
3. Cleans up Jellyfin library entries pointing to missing files
4. Updates LazyLibrarian database to remove orphaned entries
5. Provides dry-run mode and comprehensive logging

Usage:
    python3 deduplicate_ebooks_filesystem.py --dry-run    # Preview changes
    python3 deduplicate_ebooks_filesystem.py              # Execute changes
    python3 deduplicate_ebooks_filesystem.py --scan-only  # Just scan and report
"""

import argparse
import logging
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

# =============================================================================
# Configuration
# =============================================================================

LAZYLIBRARIAN_DB = Path("/mnt/docker-usb/lazylibrarian/lazylibrarian.db")
BOOKS_DIR = Path("/mnt/drive-next/Books")
LOG_DIR = Path("/home/tom/nas/logs")
JELLYFIN_API_URL = "http://localhost:8096"
JELLYFIN_API_KEY = "d0ba4efb1a664e2b8870363719c57939"

# Format preference: higher number = higher priority
FORMAT_PRIORITY = {
    ".epub": 100,
    ".mobi": 90,
    ".azw3": 85,
    ".azw": 80,
    ".pdf": 10,
    ".txt": 5,
    ".fb2": 70,
    ".cbz": 60,
    ".cbr": 55,
}

# Ebook file extensions to process
EBOOK_EXTENSIONS = {".epub", ".mobi", ".azw3", ".azw", ".pdf", ".txt", ".fb2", ".cbz", ".cbr"}

# Patterns for non-ebook files to exclude
METADATA_PATTERNS = [
    r"\.opf$",
    r"\.jpg$",
    r"\.jpeg$",
    r"\.png$",
    r"\.gif$",
    r"\.xml$",
    r"\.nfo$",
    r"\.db$",
]

# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(dry_run: bool) -> logging.Logger:
    """Setup logging with both file and console handlers."""
    LOG_DIR.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "dry_run" if dry_run else "execution"
    log_file = LOG_DIR / f"ebook_filesystem_dedup_{mode}_{timestamp}.log"

    logger = logging.getLogger("ebook_dedup_fs")
    logger.setLevel(logging.DEBUG)

    # Remove any existing handlers
    logger.handlers.clear()

    # File handler - detailed
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    # Console handler - concise
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.info(f"{'DRY RUN' if dry_run else 'EXECUTION'} MODE")
    logger.info(f"Log file: {log_file}")

    return logger

# =============================================================================
# Utility Functions
# =============================================================================

def normalize_title(title: str) -> str:
    """Normalize book title for comparison."""
    if not title:
        return ""

    # Convert to lowercase
    title = title.lower()

    # Remove file extensions
    for ext in EBOOK_EXTENSIONS:
        title = title.replace(ext, "")

    # Remove common punctuation and extra spaces
    title = re.sub(r'[:\-,\.\'\"()\[\]_]+', " ", title)
    title = re.sub(r"\s+", " ", title)

    # Remove common leading articles
    title = re.sub(r"^(the|a|an)\s+", "", title)

    # Remove series info like "(Book 1)" or "Book 1"
    title = re.sub(r"\s*\(?book\s*\d+\)?", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s*\(?\d+\)?$", "", title)

    return title.strip()

def normalize_author(author: str) -> str:
    """Normalize author name for comparison."""
    if not author:
        return ""

    author = author.lower()
    author = re.sub(r"[,\.]+", " ", author)
    author = re.sub(r"\s+", " ", author)
    author = re.sub(r"\s*(jr|sr|ii|iii|iv)\s*$", "", author, flags=re.IGNORECASE)

    return author.strip()

def extract_metadata_from_filename(filepath: Path) -> tuple[str, str]:
    """
    Extract title and author from filename.
    Common patterns:
    - "Title - Author.ext"
    - "Author/Title/Title.ext"
    - "Title.ext"
    """
    filename = filepath.stem
    parent_dirs = filepath.parts[-3:-1]  # Get 2 parent directories

    # Try pattern: "Title - Author"
    if " - " in filename:
        parts = filename.split(" - ", 1)
        title = parts[0].strip()
        author = parts[1].strip() if len(parts) > 1 else ""
    else:
        title = filename
        author = ""

    # If no author in filename, check parent directory
    if not author and len(parent_dirs) > 0:
        # Check if parent directory looks like an author name
        potential_author = parent_dirs[0]
        # Author dirs usually don't have numbers or special chars
        if not re.search(r"\d{2,}|\(|\[", potential_author):
            author = potential_author

    return (normalize_title(title), normalize_author(author))

def get_file_priority(filepath: Path) -> int:
    """Get priority score for a file."""
    ext = filepath.suffix.lower()
    priority = FORMAT_PRIORITY.get(ext, 0)

    # Bonus points for being in a structured directory (author/title/)
    if len(filepath.parts) > 3:  # Has at least Books/Author/Title/file
        priority += 5

    # Penalty for weird characters or temp files
    if re.search(r"[\[\(]\d+[\]\)]|tmp|temp|copy", filepath.name, re.IGNORECASE):
        priority -= 10

    return priority

def is_metadata_file(filepath: Path) -> bool:
    """Check if file is metadata (not an ebook)."""
    return any(re.search(pattern, str(filepath), re.IGNORECASE) for pattern in METADATA_PATTERNS)

def is_ebook_file(filepath: Path) -> bool:
    """Check if file is a valid ebook."""
    return filepath.suffix.lower() in EBOOK_EXTENSIONS

# =============================================================================
# Filesystem Scanning
# =============================================================================

def scan_books_directory(logger: logging.Logger) -> list[Path]:
    """Scan books directory for all ebook files."""
    logger.info(f"Scanning directory: {BOOKS_DIR}")

    ebook_files = []
    metadata_files = []

    for root, dirs, files in os.walk(BOOKS_DIR):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for filename in files:
            filepath = Path(root) / filename

            if is_metadata_file(filepath):
                metadata_files.append(filepath)
                continue

            if is_ebook_file(filepath):
                ebook_files.append(filepath)

    logger.info(f"Found {len(ebook_files)} ebook files")
    logger.info(f"Found {len(metadata_files)} metadata files")

    return ebook_files

def find_filesystem_duplicates(ebook_files: list[Path], logger: logging.Logger) -> dict[str, list[Path]]:
    """Group ebook files by normalized (author, title) to find duplicates."""
    groups = defaultdict(list)

    for filepath in ebook_files:
        title, author = extract_metadata_from_filename(filepath)

        if not title:
            logger.debug(f"Could not extract title from: {filepath}")
            continue

        # Create key: "author||title" or just "title" if no author
        key = f"{author}||{title}" if author else f"unknown||{title}"

        groups[key].append(filepath)

    # Filter to only groups with duplicates
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}

    logger.info(f"Found {len(duplicates)} duplicate groups containing {sum(len(v) for v in duplicates.values())} total files")

    return duplicates

def select_best_file(files: list[Path], logger: logging.Logger) -> tuple[Path, list[Path]]:
    """
    Select the best file from a group of duplicates.
    Returns: (keeper, list_of_duplicates_to_remove)
    """
    # Filter out files that don't exist
    valid_files = [f for f in files if f.exists() and f.is_file()]

    if not valid_files:
        logger.warning("No valid files in duplicate group")
        return (None, files)

    # Sort by priority (descending)
    valid_files.sort(key=lambda f: get_file_priority(f), reverse=True)

    keeper = valid_files[0]
    to_remove = valid_files[1:]

    return (keeper, to_remove)

# =============================================================================
# File System Operations
# =============================================================================

def delete_file(filepath: Path, logger: logging.Logger, dry_run: bool):
    """Safely delete a file and its associated metadata files."""
    if dry_run:
        logger.info(f"[DRY RUN] Would delete: {filepath}")
        # Also check for metadata files
        for ext in [".opf", ".jpg", ".png", ".jpeg"]:
            metadata_file = filepath.with_suffix(ext)
            if metadata_file.exists():
                logger.info(f"[DRY RUN] Would delete metadata: {metadata_file}")
        return

    try:
        if filepath.exists() and filepath.is_file():
            # Delete associated metadata files first
            for ext in [".opf", ".jpg", ".png", ".jpeg", ".nfo", ".xml"]:
                metadata_file = filepath.with_suffix(ext)
                if metadata_file.exists():
                    metadata_file.unlink()
                    logger.debug(f"Deleted metadata: {metadata_file}")

            # Delete the main file
            filepath.unlink()
            logger.info(f"Deleted: {filepath}")

            # Try to remove empty parent directories
            try:
                parent = filepath.parent
                if parent != BOOKS_DIR and not any(parent.iterdir()):
                    parent.rmdir()
                    logger.debug(f"Removed empty directory: {parent}")
            except OSError:
                pass  # Directory not empty or can't be removed
        else:
            logger.warning(f"File not found, cannot delete: {filepath}")
    except OSError as e:
        logger.error(f"Failed to delete {filepath}: {e}")

# =============================================================================
# LazyLibrarian Database Operations
# =============================================================================

def clean_lazylibrarian_orphans(deleted_files: set[str], logger: logging.Logger, dry_run: bool) -> int:
    """Remove LazyLibrarian database entries for deleted files."""
    if not deleted_files:
        return 0

    logger.info("Cleaning LazyLibrarian database orphans...")

    try:
        conn = sqlite3.connect(LAZYLIBRARIAN_DB)
        cursor = conn.cursor()

        removed_count = 0

        for deleted_file in deleted_files:
            # Convert absolute path to container path
            container_path = str(deleted_file).replace(str(BOOKS_DIR), "/books")

            if dry_run:
                logger.debug(f"[DRY RUN] Would remove DB entry for: {container_path}")
            else:
                cursor.execute("""
                    DELETE FROM books WHERE BookFile = ?
                """, (container_path,))
                if cursor.rowcount > 0:
                    logger.info(f"Removed DB entry for: {container_path}")
                    removed_count += 1

        if not dry_run:
            conn.commit()

        conn.close()

        logger.info(f"Removed {removed_count} orphan entries from LazyLibrarian")
        return removed_count

    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        return 0

# =============================================================================
# Jellyfin API Operations
# =============================================================================

def jellyfin_api_get(endpoint: str, logger: logging.Logger) -> dict:
    """Make GET request to Jellyfin API."""
    url = f"{JELLYFIN_API_URL}{endpoint}"
    headers = {"X-MediaBrowser-Token": JELLYFIN_API_KEY}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        logger.error(f"Jellyfin API error: {e}")
        return {}

def jellyfin_api_delete(endpoint: str, logger: logging.Logger, dry_run: bool) -> bool:
    """Make DELETE request to Jellyfin API."""
    if dry_run:
        logger.info(f"[DRY RUN] Would DELETE {endpoint}")
        return True

    url = f"{JELLYFIN_API_URL}{endpoint}"
    headers = {"X-MediaBrowser-Token": JELLYFIN_API_KEY}

    try:
        response = requests.delete(url, headers=headers, timeout=30)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Jellyfin API DELETE error: {e}")
        return False

def jellyfin_api_post(endpoint: str, logger: logging.Logger) -> bool:
    """Make POST request to Jellyfin API."""
    url = f"{JELLYFIN_API_URL}{endpoint}"
    headers = {"X-MediaBrowser-Token": JELLYFIN_API_KEY}

    try:
        response = requests.post(url, headers=headers, timeout=30)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.error(f"Jellyfin API POST error: {e}")
        return False

def clean_jellyfin_library(logger: logging.Logger, dry_run: bool) -> dict[str, int]:
    """Clean up Jellyfin library - remove entries with missing files."""
    stats = {
        "total_books": 0,
        "missing_files": 0,
        "metadata_entries": 0,
        "entries_removed": 0,
    }

    logger.info("=" * 70)
    logger.info("Starting Jellyfin Library Cleanup")
    logger.info("=" * 70)

    # Get all book items from Jellyfin
    logger.info("Fetching books from Jellyfin...")
    response = jellyfin_api_get("/Items?IncludeItemTypes=Book&Recursive=true&Fields=Path", logger)

    items = response.get("Items", [])
    stats["total_books"] = len(items)
    logger.info(f"Found {len(items)} book entries in Jellyfin")

    if not items:
        logger.warning("No books found in Jellyfin library")
        return stats

    for item in items:
        item_id = item.get("Id")
        item_name = item.get("Name")
        item_path = item.get("Path", "")

        # Check if file exists
        if not item_path:
            logger.warning(f"Book '{item_name}' (ID: {item_id}) has no path")
            stats["missing_files"] += 1

            logger.info(f"Removing orphan entry: {item_name}")
            if jellyfin_api_delete(f"/Items/{item_id}", logger, dry_run):
                stats["entries_removed"] += 1
            continue

        # Check if it's a metadata file (shouldn't be in library)
        if is_metadata_file(Path(item_path)):
            logger.info(f"Found metadata file entry: {item_name} -> {item_path}")
            stats["metadata_entries"] += 1

            logger.info(f"Removing metadata entry: {item_name}")
            if jellyfin_api_delete(f"/Items/{item_id}", logger, dry_run):
                stats["entries_removed"] += 1
            continue

        # Check if file exists on disk
        if not Path(item_path).exists():
            logger.warning(f"Missing file: {item_name} -> {item_path}")
            stats["missing_files"] += 1

            logger.info(f"Removing missing file entry: {item_name}")
            if jellyfin_api_delete(f"/Items/{item_id}", logger, dry_run):
                stats["entries_removed"] += 1
            continue

    # Trigger library scan if we made changes
    if stats["entries_removed"] > 0 and not dry_run:
        logger.info("Triggering Jellyfin library refresh...")
        # Trigger a full library scan
        if jellyfin_api_post("/Library/Refresh", logger):
            logger.info("Library refresh triggered")

    return stats

# =============================================================================
# Main Deduplication Logic
# =============================================================================

def deduplicate_ebooks(logger: logging.Logger, dry_run: bool, scan_only: bool) -> dict[str, int]:
    """Main deduplication logic."""
    stats = {
        "total_files": 0,
        "duplicate_groups": 0,
        "files_deleted": 0,
        "space_freed_mb": 0,
        "db_entries_removed": 0,
    }

    logger.info("=" * 70)
    logger.info("Starting Filesystem Ebook Deduplication")
    logger.info("=" * 70)

    # Scan for all ebook files
    ebook_files = scan_books_directory(logger)
    stats["total_files"] = len(ebook_files)

    # Find duplicates
    duplicate_groups = find_filesystem_duplicates(ebook_files, logger)
    stats["duplicate_groups"] = len(duplicate_groups)

    if not duplicate_groups:
        logger.info("No duplicates found!")
        return stats

    deleted_files = set()

    # Process each duplicate group
    for group_key, group_files in sorted(duplicate_groups.items()):
        author, title = group_key.split("||")
        logger.info("")
        logger.info(f"Processing duplicate: '{title.title()}' by '{author.title()}'")
        logger.info(f"  Found {len(group_files)} copies:")

        for filepath in group_files:
            priority = get_file_priority(filepath)
            size_mb = filepath.stat().st_size / (1024 * 1024)
            logger.info(f"    [{priority:3d}] {size_mb:6.2f} MB - {filepath.relative_to(BOOKS_DIR)}")

        if scan_only:
            continue

        # Select best file
        keeper, to_remove = select_best_file(group_files, logger)

        if keeper:
            logger.info(f"  ✓ Keeping: {keeper.relative_to(BOOKS_DIR)}")
        else:
            logger.warning("  ! No valid keeper found for this group!")
            continue

        # Remove duplicates
        for dup_file in to_remove:
            logger.info(f"  ✗ Removing: {dup_file.relative_to(BOOKS_DIR)}")

            # Track space freed
            if dup_file.exists():
                stats["space_freed_mb"] += dup_file.stat().st_size / (1024 * 1024)

            delete_file(dup_file, logger, dry_run)
            stats["files_deleted"] += 1
            deleted_files.add(str(dup_file))

    if not scan_only:
        # Clean LazyLibrarian database
        stats["db_entries_removed"] = clean_lazylibrarian_orphans(deleted_files, logger, dry_run)

    return stats

# =============================================================================
# Main Function
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Deduplicate ebook files and clean Jellyfin library"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without executing them"
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Only scan and report duplicates, do not delete anything"
    )
    parser.add_argument(
        "--jellyfin-only",
        action="store_true",
        help="Only clean Jellyfin library, skip deduplication"
    )
    parser.add_argument(
        "--no-jellyfin",
        action="store_true",
        help="Skip Jellyfin cleanup, only deduplicate files"
    )

    args = parser.parse_args()

    # Setup logging
    logger = setup_logging(args.dry_run or args.scan_only)

    # Validate paths
    if not LAZYLIBRARIAN_DB.exists():
        logger.error(f"LazyLibrarian database not found: {LAZYLIBRARIAN_DB}")
        sys.exit(1)

    if not BOOKS_DIR.exists():
        logger.error(f"Books directory not found: {BOOKS_DIR}")
        sys.exit(1)

    # Execute requested operations
    total_stats = {}

    if not args.jellyfin_only:
        dedup_stats = deduplicate_ebooks(logger, args.dry_run, args.scan_only)
        total_stats.update(dedup_stats)

    if not args.no_jellyfin and not args.scan_only:
        jellyfin_stats = clean_jellyfin_library(logger, args.dry_run)
        total_stats.update({"jellyfin_" + k: v for k, v in jellyfin_stats.items()})

    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info(f"{'SCAN ' if args.scan_only else ''}{'DRY RUN ' if args.dry_run else ''}SUMMARY")
    logger.info("=" * 70)

    for key, value in sorted(total_stats.items()):
        formatted_key = key.replace("_", " ").title()
        if "mb" in key:
            logger.info(f"{formatted_key}: {value:.2f} MB")
        else:
            logger.info(f"{formatted_key}: {value}")

    if args.scan_only:
        logger.info("")
        logger.info("This was a SCAN ONLY - no changes were made.")
        logger.info("Run without --scan-only to deduplicate.")
    elif args.dry_run:
        logger.info("")
        logger.info("This was a DRY RUN - no changes were made.")
        logger.info("Run without --dry-run to execute changes.")

    logger.info("=" * 70)
    logger.info("Complete!")
    logger.info("=" * 70)

if __name__ == "__main__":
    main()
