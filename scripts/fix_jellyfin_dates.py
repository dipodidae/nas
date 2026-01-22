#!/usr/bin/env python3
"""
Fix Jellyfin movie DateCreated by using DateLastRefreshed instead.
This corrects sorting when file timestamps don't reflect when files were added.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path("/home/tom/.local/share/jellyfin/data/jellyfin.db")


def fix_movie_dates(db_path: Path, dry_run: bool = True) -> None:
    """Update DateCreated to match DateLastRefreshed for all movies."""

    if not db_path.exists():
        print(f"Error: Database not found at {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Get movies where DateCreated != DateLastRefreshed
    query = """
        SELECT Id, Name, DateCreated, DateLastRefreshed
        FROM BaseItems
        WHERE IsMovie = 1
        AND DateLastRefreshed IS NOT NULL
        ORDER BY DateLastRefreshed DESC
    """

    cursor.execute(query)
    movies = cursor.fetchall()

    print(f"Found {len(movies)} movies")
    print(f"\n{'DRY RUN - ' if dry_run else ''}Top 20 movies by scan date:")
    print("-" * 80)

    updated_count = 0
    for i, (movie_id, name, date_created, date_refreshed) in enumerate(movies[:20]):
        print(f"{i+1}. {name}")
        print(f"   Current DateCreated: {date_created}")
        print(f"   DateLastRefreshed:   {date_refreshed}")

        if date_created != date_refreshed:
            if not dry_run:
                update_query = """
                    UPDATE BaseItems
                    SET DateCreated = ?
                    WHERE Id = ?
                """
                cursor.execute(update_query, (date_refreshed, movie_id))
                updated_count += 1
            print("   -> Would update" if dry_run else "   -> UPDATED")
        else:
            print("   -> Already correct")
        print()

    if not dry_run:
        conn.commit()
        print(f"\n✓ Updated {updated_count} movies")
    else:
        print("\n✓ Would update movies (run with --apply to actually update)")

    conn.close()


def main():

    # The database is inside the container, need to access via docker exec
    print("This script needs to run inside the Jellyfin container.")
    print("Use: docker exec jellyfin python3 /path/to/script.py")
    sys.exit(1)


if __name__ == "__main__":
    main()
