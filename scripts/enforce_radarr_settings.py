#!/usr/bin/env python3
"""
Monitor and enforce critical Radarr settings that affect folder naming.
This prevents AutoConfig or manual changes from breaking naming again.
"""
import json
import sys
import os
import subprocess

RADARR_API_KEY = os.getenv("API_KEY_RADARR")
RADARR_URL = "http://localhost:7878"

REQUIRED_SETTINGS = {
    "naming": {
        "movieFolderFormat": "{Movie Collection}{Movie Collection: - }{Movie CleanTitleThe} ({Release Year})",
        "renameMovies": True
    },
    "mediamanagement": {
        "autoRenameFolders": True
    }
}

def get_radarr_config(endpoint: str) -> dict:
    """Fetch config from Radarr API."""
    cmd = ["curl", "-s", "-H", f"X-Api-Key: {RADARR_API_KEY}", f"{RADARR_URL}/api/v3/config/{endpoint}"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)

def set_radarr_config(endpoint: str, data: dict) -> bool:
    """Update config via Radarr API."""
    cmd = [
        "curl", "-s", "-X", "PUT",
        "-H", f"X-Api-Key: {RADARR_API_KEY}",
        "-H", "Content-Type: application/json",
        f"{RADARR_URL}/api/v3/config/{endpoint}",
        "--data-raw", json.dumps(data)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0

def main() -> int:
    """Validate and enforce critical Radarr settings."""
    if not RADARR_API_KEY:
        print("ERROR: API_KEY_RADARR not set", file=sys.stderr)
        return 1
    
    issues_found = []
    fixes_applied = []
    
    # Check naming config
    try:
        naming_config = get_radarr_config("naming")
        for key, expected_value in REQUIRED_SETTINGS["naming"].items():
            current_value = naming_config.get(key)
            if current_value != expected_value:
                issues_found.append(f"Naming.{key}: Expected {expected_value}, got {current_value}")
                naming_config[key] = expected_value
                fixes_applied.append(f"Naming.{key} corrected")
        
        if fixes_applied:
            if set_radarr_config("naming", naming_config):
                print("✓ Applied naming config fixes")
            else:
                print("✗ Failed to apply naming fixes", file=sys.stderr)
                return 1
    
    except Exception as e:
        print(f"ERROR checking naming: {e}", file=sys.stderr)
        return 1
    
    # Check media management config
    try:
        mm_config = get_radarr_config("mediamanagement")
        for key, expected_value in REQUIRED_SETTINGS["mediamanagement"].items():
            current_value = mm_config.get(key)
            if current_value != expected_value:
                issues_found.append(f"MediaManagement.{key}: Expected {expected_value}, got {current_value}")
                mm_config[key] = expected_value
                fixes_applied.append(f"MediaManagement.{key} corrected")
        
        if "MediaManagement" in str(fixes_applied):
            if set_radarr_config("mediamanagement", mm_config):
                print("✓ Applied media management fixes")
            else:
                print("✗ Failed to apply media management fixes", file=sys.stderr)
                return 1
    
    except Exception as e:
        print(f"ERROR checking media management: {e}", file=sys.stderr)
        return 1
    
    if issues_found:
        print(f"\n⚠️  Found and fixed {len(issues_found)} configuration issues:")
        for issue in issues_found:
            print(f"  - {issue}")
        return 0
    else:
        print("✅ All critical settings are correct")
        print(f"   • Folder format: {naming_config['movieFolderFormat']}")
        print(f"   • Auto-rename folders: {mm_config['autoRenameFolders']}")
        return 0

if __name__ == "__main__":
    sys.exit(main())
