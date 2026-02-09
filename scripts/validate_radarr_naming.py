#!/usr/bin/env python3
"""
Validate and fix Radarr movie folder naming configuration.
Run this after any Radarr config changes to ensure folders stay properly formatted.
"""
import os
import sys
import json
import subprocess
import re
from pathlib import Path

RADARR_API_KEY = os.getenv("API_KEY_RADARR")
RADARR_URL = "http://localhost:7878"
MOVIES_DIR = os.getenv("SHARE_DIRECTORY", "/mnt/drive-next") + "/Movies"

EXPECTED_FOLDER_FORMAT = "{Movie Collection}{Movie Collection: - }{Movie CleanTitleThe} ({Release Year})"

def get_naming_config() -> dict:
    """Fetch current naming configuration from Radarr API."""
    cmd = [
        "curl", "-s",
        "-H", f"X-Api-Key: {RADARR_API_KEY}",
        f"{RADARR_URL}/api/v3/config/naming"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

def validate_naming_config() -> bool:
    """Check if naming configuration matches expected format."""
    config = get_naming_config()
    folder_format = config.get("movieFolderFormat", "")
    rename_enabled = config.get("renameMovies", False)
    
    issues = []
    
    if folder_format != EXPECTED_FOLDER_FORMAT:
        issues.append(f"Folder format mismatch:\n  Expected: {EXPECTED_FOLDER_FORMAT}\n  Current:  {folder_format}")
    
    if not rename_enabled:
        issues.append("Movie renaming is DISABLED - should be enabled")
    
    if issues:
        print("NAMING CONFIG ISSUES FOUND:", file=sys.stderr)
        for issue in issues:
            print(f"  - {issue}", file=sys.stderr)
        return False
    
    print("✓ Naming configuration is correct")
    return True

def check_folder_names() -> int:
    """Scan movie folders for literal pattern text."""
    if not os.path.exists(MOVIES_DIR):
        print(f"ERROR: Movies directory not found: {MOVIES_DIR}", file=sys.stderr)
        return 1
    
    movies_path = Path(MOVIES_DIR)
    broken_folders = [f for f in movies_path.iterdir() if f.is_dir() and '{' in f.name]
    
    if broken_folders:
        print(f"ERROR: Found {len(broken_folders)} folders with literal pattern text:", file=sys.stderr)
        for folder in broken_folders[:5]:
            print(f"  - {folder.name}", file=sys.stderr)
        if len(broken_folders) > 5:
            print(f"  ... and {len(broken_folders) - 5} more", file=sys.stderr)
        print("\nRun: python3 scripts/fix_radarr_folders.py", file=sys.stderr)
        return 1
    
    print("✓ All movie folder names are clean")
    return 0

def main() -> int:
    """Validate Radarr naming configuration and folder names."""
    if not RADARR_API_KEY:
        print("ERROR: API_KEY_RADARR environment variable not set", file=sys.stderr)
        return 1
    
    print("Checking Radarr naming configuration...")
    config_ok = validate_naming_config()
    
    print("\nChecking movie folder names...")
    folders_ok = check_folder_names() == 0
    
    if config_ok and folders_ok:
        print("\n✓ All checks passed - Radarr naming is properly configured")
        return 0
    else:
        print("\n✗ Issues found - see errors above", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
