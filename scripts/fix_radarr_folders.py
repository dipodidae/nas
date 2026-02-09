#!/usr/bin/env python3
"""Fix Radarr movie folder names by removing literal pattern text."""
import os
import sys
import re
from pathlib import Path

MOVIES_DIR = os.getenv("SHARE_DIRECTORY", "/mnt/drive-next") + "/Movies"

def fix_folder_name(name: str) -> str:
    """Remove literal pattern text from folder names."""
    # Replace literal {Movie Collection: - } with proper separator " - "
    fixed = re.sub(r'\{Movie Collection: - \}', ' - ', name)
    # Clean up any leading/trailing spaces or dashes
    fixed = fixed.strip()
    return fixed

def main() -> int:
    """Rename all broken movie folders."""
    if not os.path.exists(MOVIES_DIR):
        print(f"ERROR: Movies directory not found: {MOVIES_DIR}", file=sys.stderr)
        return 1
    
    movies_path = Path(MOVIES_DIR)
    broken_folders = [f for f in movies_path.iterdir() if f.is_dir() and '{' in f.name]
    
    if not broken_folders:
        print("No broken folders found")
        return 0
    
    print(f"Found {len(broken_folders)} broken folders to fix")
    
    for folder in broken_folders:
        old_name = folder.name
        new_name = fix_folder_name(old_name)
        
        if old_name == new_name:
            print(f"SKIP: No change needed for {old_name}")
            continue
        
        new_path = folder.parent / new_name
        
        if new_path.exists():
            print(f"ERROR: Target already exists: {new_name}", file=sys.stderr)
            continue
        
        try:
            folder.rename(new_path)
            print(f"RENAMED: {old_name} -> {new_name}")
        except Exception as e:
            print(f"ERROR renaming {old_name}: {e}", file=sys.stderr)
            return 1
    
    print(f"\nSuccessfully fixed {len(broken_folders)} folders")
    return 0

if __name__ == "__main__":
    sys.exit(main())
