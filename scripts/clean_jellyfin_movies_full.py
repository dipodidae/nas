#!/usr/bin/env python3
"""Clean ALL movie-related data from Jellyfin database including related tables."""
import sqlite3
import sys
import os
from pathlib import Path

JELLYFIN_DB = os.getenv("CONFIG_DIRECTORY", "/mnt/docker-usb") + "/jellyfin/data/data/jellyfin.db"

def main() -> int:
    """Remove all movie-related entries and orphaned data."""
    if not os.path.exists(JELLYFIN_DB):
        print(f"ERROR: Database not found: {JELLYFIN_DB}", file=sys.stderr)
        return 1
    
    # Backup
    backup = f"{JELLYFIN_DB}.backup-full-clean"
    Path(backup).write_bytes(Path(JELLYFIN_DB).read_bytes())
    print(f"✓ Created backup: {backup}")
    
    conn = sqlite3.connect(JELLYFIN_DB)
    cursor = conn.cursor()
    
    try:
        # Get movie IDs
        cursor.execute("SELECT Id FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.Movie'")
        movie_ids = [row[0] for row in cursor.fetchall()]
        
        cursor.execute("SELECT Id FROM BaseItems WHERE Type = 'MediaBrowser.Controller.Entities.Movies.BoxSet'")
        boxset_ids = [row[0] for row in cursor.fetchall()]
        
        all_ids = movie_ids + boxset_ids
        print(f"Found {len(movie_ids)} movies and {len(boxset_ids)} boxsets")
        
        if not all_ids:
            print("No movies to clean")
            return 0
        
        # Delete from related tables
        placeholders = ','.join(['?' for _ in all_ids])
        
        tables_to_clean = [
            'BaseItemProviders',
            'BaseItemImageInfos',
            'MediaStreamInfos',
            'Chapters',
            'AncestorIds',
            'UserData',
            'ItemValues',
            'PeopleBaseItemMap',
            'BaseItemTrailerTypes',
            'AttachmentStreamInfos',
            'MediaSegments',
            'TrickplayInfos',
            'KeyframeData'
        ]
        
        for table in tables_to_clean:
            try:
                cursor.execute(f"DELETE FROM {table} WHERE ItemId IN ({placeholders})", all_ids)
                deleted = cursor.rowcount
                if deleted > 0:
                    print(f"  Cleaned {deleted} rows from {table}")
            except Exception as e:
                print(f"  Note: {table} - {e}")
        
        # Delete the movies themselves
        cursor.execute(f"DELETE FROM BaseItems WHERE Id IN ({placeholders})", all_ids)
        print(f"✓ Deleted {len(all_ids)} items from BaseItems")
        
        conn.commit()
        print(f"✓ Database fully cleaned")
        
        return 0
        
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    finally:
        conn.close()

if __name__ == "__main__":
    sys.exit(main())
