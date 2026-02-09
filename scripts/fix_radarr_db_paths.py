#!/usr/bin/env python3
"""Fix Radarr database paths to match renamed folders."""
import sqlite3
import sys
import os
import re
from pathlib import Path

RADARR_DB = os.getenv("CONFIG_DIRECTORY", "/mnt/docker-usb") + "/radarr/radarr.db"

def fix_path(path: str) -> str:
    """Fix broken path patterns."""
    # Replace literal {Movie Collection: - } with proper separator
    fixed = re.sub(r'\{Movie Collection: - \}', ' - ', path)
    # Remove standalone pattern at start
    fixed = re.sub(r'^/movies/\{Movie Collection: - \}', '/movies/', fixed)
    return fixed

def main() -> int:
    """Update all movie paths in Radarr database."""
    if not os.path.exists(RADARR_DB):
        print(f"ERROR: Database not found: {RADARR_DB}", file=sys.stderr)
        return 1
    
    # Backup
    backup = f"{RADARR_DB}.backup-path-fix"
    Path(backup).write_bytes(Path(RADARR_DB).read_bytes())
    print(f"✓ Created backup: {backup}")
    
    conn = sqlite3.connect(RADARR_DB)
    cursor = conn.cursor()
    
    try:
        # Get all movies with broken paths
        cursor.execute("SELECT Id, Path FROM Movies WHERE Path LIKE '%{Movie Collection: - }%'")
        movies = cursor.fetchall()
        
        print(f"Found {len(movies)} movies with broken paths")
        
        for movie_id, old_path in movies:
            new_path = fix_path(old_path)
            if old_path != new_path:
                cursor.execute("UPDATE Movies SET Path = ? WHERE Id = ?", (new_path, movie_id))
                print(f"  Fixed movie ID {movie_id}:")
                print(f"    Old: {old_path}")
                print(f"    New: {new_path}")
        
        # Also fix MovieFiles table
        cursor.execute("SELECT Id, RelativePath FROM MovieFiles WHERE RelativePath LIKE '%{Movie Collection: - }%'")
        files = cursor.fetchall()
        
        for file_id, old_rel_path in files:
            new_rel_path = fix_path(old_rel_path)
            if old_rel_path != new_rel_path:
                cursor.execute("UPDATE MovieFiles SET RelativePath = ? WHERE Id = ?", (new_rel_path, file_id))
                print(f"  Fixed file: {new_rel_path}")
        
        conn.commit()
        print(f"\n✓ Fixed {len(movies)} movie paths")
        
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
