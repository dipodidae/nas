#!/usr/bin/env python3
"""Clean Jellyfin database of all movie entries to force clean rescan."""
import sqlite3
import sys
import os
from pathlib import Path

JELLYFIN_DB = os.getenv("CONFIG_DIRECTORY", "/mnt/docker-usb") + "/jellyfin/data/data/jellyfin.db"

def main() -> int:
    """Remove all movie-related entries from Jellyfin database."""
    if not os.path.exists(JELLYFIN_DB):
        print(f"ERROR: Database not found: {JELLYFIN_DB}", file=sys.stderr)
        return 1
    
    # Backup first
    backup = f"{JELLYFIN_DB}.backup-before-clean"
    Path(backup).write_bytes(Path(JELLYFIN_DB).read_bytes())
    print(f"✓ Created backup: {backup}")
    
    conn = sqlite3.connect(JELLYFIN_DB)
    cursor = conn.cursor()
    
    try:
        # Get count before
        cursor.execute("SELECT COUNT(*) FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'")
        before_count = cursor.fetchone()[0]
        print(f"Found {before_count} movie entries in database")
        
        # Delete all movie entries
        cursor.execute("DELETE FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'")
        conn.commit()
        
        # Also delete boxsets
        cursor.execute("DELETE FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.BoxSet'")
        conn.commit()
        
        # Get count after
        cursor.execute("SELECT COUNT(*) FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'")
        after_count = cursor.fetchone()[0]
        
        print(f"✓ Deleted {before_count - after_count} movie entries")
        print(f"✓ Database cleaned - restart Jellyfin to rescan")
        
        return 0
        
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

if __name__ == "__main__":
    sys.exit(main())
