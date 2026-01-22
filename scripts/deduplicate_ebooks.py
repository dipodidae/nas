#!/usr/bin/env python3
"""
LazyLibrarian & Jellyfin Ebook Deduplication and Cleanup Script

This script safely:
1. Deduplicates books in LazyLibrarian by matching title + author
2. Prefers non-PDF formats (EPUB, MOBI, AZW3) over PDF when duplicates exist
3. Cleans up Jellyfin library entries pointing to missing files
4. Provides dry-run mode and comprehensive logging

Usage:
    python3 deduplicate_ebooks.py --dry-run    # Preview changes
    python3 deduplicate_ebooks.py              # Execute changes
    python3 deduplicate_ebooks.py --jellyfin-only  # Only clean Jellyfin
"""

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Tuple
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
    '.epub': 100,
    '.mobi': 90,
    '.azw3': 85,
    '.azw': 80,
    '.pdf': 10,
    '.txt': 5,
}

# Patterns for garbage files to exclude
GARBAGE_PATTERNS = [
    r'\.opf$',
    r'\.jpg$',
    r'\.png$',
    r'\.xml$',
    r'\.nfo$',
    r'/\.',  # Hidden files
]

# =============================================================================
# Logging Setup
# =============================================================================

def setup_logging(dry_run: bool) -> logging.Logger:
    """Setup logging with both file and console handlers."""
    LOG_DIR.mkdir(exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "dry_run" if dry_run else "execution"
    log_file = LOG_DIR / f"ebook_dedup_{mode}_{timestamp}.log"
    
    logger = logging.getLogger("ebook_dedup")
    logger.setLevel(logging.INFO)
    
    # File handler - detailed
    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    ))
    
    # Console handler - concise
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    
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
    
    # Remove common punctuation and extra spaces
    title = re.sub(r'[:\-,\.\'\"()\[\]]+', ' ', title)
    title = re.sub(r'\s+', ' ', title)
    
    # Remove common leading articles
    title = re.sub(r'^(the|a|an)\s+', '', title)
    
    return title.strip()

def normalize_author(author: str) -> str:
    """Normalize author name for comparison."""
    if not author:
        return ""
    
    author = author.lower()
    author = re.sub(r'[,\.]+', ' ', author)
    author = re.sub(r'\s+', ' ', author)
    
    return author.strip()

def get_file_format_priority(filepath: str) -> int:
    """Get priority score for a file format."""
    ext = Path(filepath).suffix.lower()
    return FORMAT_PRIORITY.get(ext, 0)

def is_garbage_file(filepath: str) -> bool:
    """Check if file matches garbage patterns."""
    for pattern in GARBAGE_PATTERNS:
        if re.search(pattern, filepath):
            return True
    return False

def file_exists(filepath: str) -> bool:
    """Check if file exists and is a regular file."""
    if not filepath:
        return False
    
    path = Path(filepath)
    return path.exists() and path.is_file()

# =============================================================================
# LazyLibrarian Database Operations
# =============================================================================

def get_all_books_from_db(logger: logging.Logger) -> List[Dict]:
    """Fetch all books from LazyLibrarian database."""
    logger.info(f"Connecting to database: {LAZYLIBRARIAN_DB}")
    
    try:
        conn = sqlite3.connect(LAZYLIBRARIAN_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT b.BookID, a.AuthorName, b.BookName, b.BookFile, b.BookIsbn, 
                   b.BookDate, b.Status, b.BookLibrary, b.AuthorID
            FROM books b
            LEFT JOIN authors a ON b.AuthorID = a.AuthorID
            WHERE b.BookFile IS NOT NULL AND b.BookFile != ''
            ORDER BY a.AuthorName, b.BookName
        """)
        
        books = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        logger.info(f"Found {len(books)} books in database")
        return books
        
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        sys.exit(1)

def update_book_file_in_db(book_id: str, new_file: str, logger: logging.Logger, dry_run: bool):
    """Update book file path in database."""
    if dry_run:
        logger.info(f"[DRY RUN] Would update BookID {book_id} to file: {new_file}")
        return
    
    try:
        conn = sqlite3.connect(LAZYLIBRARIAN_DB)
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE books SET BookFile = ? WHERE BookID = ?
        """, (new_file, book_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Updated BookID {book_id} to file: {new_file}")
        
    except sqlite3.Error as e:
        logger.error(f"Failed to update BookID {book_id}: {e}")

def remove_book_from_db(book_id: str, logger: logging.Logger, dry_run: bool):
    """Remove book entry from database."""
    if dry_run:
        logger.info(f"[DRY RUN] Would remove BookID {book_id} from database")
        return
    
    try:
        conn = sqlite3.connect(LAZYLIBRARIAN_DB)
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM books WHERE BookID = ?", (book_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Removed BookID {book_id} from database")
        
    except sqlite3.Error as e:
        logger.error(f"Failed to remove BookID {book_id}: {e}")

# =============================================================================
# File System Operations
# =============================================================================

def delete_file(filepath: str, logger: logging.Logger, dry_run: bool):
    """Safely delete a file."""
    if dry_run:
        logger.info(f"[DRY RUN] Would delete: {filepath}")
        return
    
    try:
        path = Path(filepath)
        if path.exists() and path.is_file():
            path.unlink()
            logger.info(f"Deleted: {filepath}")
        else:
            logger.warning(f"File not found, cannot delete: {filepath}")
    except OSError as e:
        logger.error(f"Failed to delete {filepath}: {e}")

# =============================================================================
# Deduplication Logic
# =============================================================================

def find_duplicates(books: List[Dict], logger: logging.Logger) -> Dict[str, List[Dict]]:
    """Group books by normalized (author, title) to find duplicates."""
    groups = defaultdict(list)
    
    for book in books:
        author = normalize_author(book.get('AuthorName', ''))
        title = normalize_title(book.get('BookName', ''))
        
        if not author or not title:
            logger.debug(f"Skipping book with missing author/title: {book.get('BookID')}")
            continue
        
        key = f"{author}||{title}"
        groups[key].append(book)
    
    # Filter to only groups with duplicates
    duplicates = {k: v for k, v in groups.items() if len(v) > 1}
    
    logger.info(f"Found {len(duplicates)} duplicate groups containing {sum(len(v) for v in duplicates.values())} total books")
    
    return duplicates

def select_best_format(books: List[Dict], logger: logging.Logger) -> Tuple[Dict, List[Dict]]:
    """
    Select the best format from a group of duplicate books.
    Returns: (keeper, list_of_duplicates_to_remove)
    """
    # Filter out books with missing files first
    valid_books = [b for b in books if file_exists(b.get('BookFile', ''))]
    
    if not valid_books:
        logger.warning(f"No valid files found for duplicate group")
        return (None, books)
    
    # Sort by format priority (descending)
    valid_books.sort(key=lambda b: get_file_format_priority(b.get('BookFile', '')), reverse=True)
    
    keeper = valid_books[0]
    to_remove = valid_books[1:]
    
    # Also include books with missing files
    missing_files = [b for b in books if not file_exists(b.get('BookFile', ''))]
    to_remove.extend(missing_files)
    
    return (keeper, to_remove)

def deduplicate_books(logger: logging.Logger, dry_run: bool) -> Dict[str, int]:
    """Main deduplication logic."""
    stats = {
        'total_books': 0,
        'duplicate_groups': 0,
        'files_deleted': 0,
        'db_entries_updated': 0,
        'db_entries_removed': 0,
    }
    
    logger.info("=" * 70)
    logger.info("Starting LazyLibrarian Deduplication")
    logger.info("=" * 70)
    
    # Get all books
    books = get_all_books_from_db(logger)
    stats['total_books'] = len(books)
    
    # Find duplicates
    duplicate_groups = find_duplicates(books, logger)
    stats['duplicate_groups'] = len(duplicate_groups)
    
    if not duplicate_groups:
        logger.info("No duplicates found!")
        return stats
    
    # Process each duplicate group
    for group_key, group_books in duplicate_groups.items():
        author, title = group_key.split('||')
        logger.info("")
        logger.info(f"Processing duplicate: {title.title()} by {author.title()}")
        logger.info(f"  Found {len(group_books)} copies:")
        
        for book in group_books:
            filepath = book.get('BookFile', '')
            exists = "✓" if file_exists(filepath) else "✗"
            priority = get_file_format_priority(filepath)
            logger.info(f"    {exists} [{priority:3d}] {filepath}")
        
        # Select best format
        keeper, to_remove = select_best_format(group_books, logger)
        
        if keeper:
            logger.info(f"  Keeping: {keeper.get('BookFile')}")
        else:
            logger.warning(f"  No valid keeper found for this group!")
            continue
        
        # Remove duplicates
        for dup in to_remove:
            dup_file = dup.get('BookFile', '')
            dup_id = dup.get('BookID')
            
            logger.info(f"  Removing duplicate: {dup_file}")
            
            # Delete file if it exists
            if file_exists(dup_file):
                delete_file(dup_file, logger, dry_run)
                stats['files_deleted'] += 1
            
            # Remove from database
            remove_book_from_db(dup_id, logger, dry_run)
            stats['db_entries_removed'] += 1
    
    return stats

# =============================================================================
# Jellyfin Cleanup
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

def clean_jellyfin_library(logger: logging.Logger, dry_run: bool) -> Dict[str, int]:
    """Clean up Jellyfin library - remove entries with missing files."""
    stats = {
        'total_books': 0,
        'missing_files': 0,
        'garbage_entries': 0,
        'entries_removed': 0,
    }
    
    logger.info("=" * 70)
    logger.info("Starting Jellyfin Library Cleanup")
    logger.info("=" * 70)
    
    # Get all book items from Jellyfin
    logger.info("Fetching books from Jellyfin...")
    response = jellyfin_api_get("/Items?IncludeItemTypes=Book&Recursive=true&Fields=Path", logger)
    
    items = response.get('Items', [])
    stats['total_books'] = len(items)
    logger.info(f"Found {len(items)} book entries in Jellyfin")
    
    for item in items:
        item_id = item.get('Id')
        item_name = item.get('Name')
        item_path = item.get('Path', '')
        
        # Check if file exists
        if not item_path:
            logger.warning(f"Book '{item_name}' (ID: {item_id}) has no path")
            stats['missing_files'] += 1
            
            logger.info(f"Removing orphan entry: {item_name}")
            if jellyfin_api_delete(f"/Items/{item_id}", logger, dry_run):
                stats['entries_removed'] += 1
            continue
        
        # Check if it's a garbage file
        if is_garbage_file(item_path):
            logger.info(f"Found garbage file: {item_name} -> {item_path}")
            stats['garbage_entries'] += 1
            
            logger.info(f"Removing garbage entry: {item_name}")
            if jellyfin_api_delete(f"/Items/{item_id}", logger, dry_run):
                stats['entries_removed'] += 1
            continue
        
        # Check if file exists on disk
        if not file_exists(item_path):
            logger.warning(f"Missing file: {item_name} -> {item_path}")
            stats['missing_files'] += 1
            
            logger.info(f"Removing missing file entry: {item_name}")
            if jellyfin_api_delete(f"/Items/{item_id}", logger, dry_run):
                stats['entries_removed'] += 1
            continue
    
    # Trigger library scan if we made changes
    if stats['entries_removed'] > 0 and not dry_run:
        logger.info("Triggering Jellyfin library refresh...")
        # Find the Books library ID
        libraries = jellyfin_api_get("/Library/VirtualFolders", logger)
        for lib in libraries:
            if lib.get('Name') == 'Books' or 'Book' in lib.get('CollectionType', ''):
                lib_id = lib.get('ItemId')
                if lib_id:
                    jellyfin_api_get(f"/Items/{lib_id}/Refresh?Recursive=true", logger)
                    logger.info("Library refresh triggered")
                break
    
    return stats

# =============================================================================
# Main Function
# =============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Deduplicate LazyLibrarian ebooks and clean Jellyfin library"
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview changes without executing them'
    )
    parser.add_argument(
        '--jellyfin-only',
        action='store_true',
        help='Only clean Jellyfin library, skip LazyLibrarian deduplication'
    )
    parser.add_argument(
        '--lazylibrarian-only',
        action='store_true',
        help='Only deduplicate LazyLibrarian, skip Jellyfin cleanup'
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logger = setup_logging(args.dry_run)
    
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
        dedup_stats = deduplicate_books(logger, args.dry_run)
        total_stats.update({'lazylibrarian_' + k: v for k, v in dedup_stats.items()})
    
    if not args.lazylibrarian_only:
        jellyfin_stats = clean_jellyfin_library(logger, args.dry_run)
        total_stats.update({'jellyfin_' + k: v for k, v in jellyfin_stats.items()})
    
    # Summary
    logger.info("")
    logger.info("=" * 70)
    logger.info(f"{'DRY RUN ' if args.dry_run else ''}SUMMARY")
    logger.info("=" * 70)
    
    for key, value in sorted(total_stats.items()):
        logger.info(f"{key.replace('_', ' ').title()}: {value}")
    
    if args.dry_run:
        logger.info("")
        logger.info("This was a DRY RUN - no changes were made.")
        logger.info("Run without --dry-run to execute changes.")
    
    logger.info("=" * 70)
    logger.info("Deduplication complete!")
    logger.info("=" * 70)

if __name__ == "__main__":
    main()
