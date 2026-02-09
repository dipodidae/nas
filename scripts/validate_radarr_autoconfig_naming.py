#!/usr/bin/env python3
"""
Validate that Radarr's naming configuration matches the locked custom naming.json.
This ensures AutoConfig script hasn't overwritten with external sources.
"""
import json
import sys
import os
import subprocess

RADARR_API_KEY = os.getenv("API_KEY_RADARR")
RADARR_URL = "http://localhost:7878"
NAMING_JSON_PATH = os.getenv("CONFIG_DIRECTORY", "/mnt/docker-usb") + "/radarr/extended/naming.json"

def get_expected_naming() -> dict:
    """Load expected naming from custom naming.json."""
    with open(NAMING_JSON_PATH, 'r') as f:
        naming_data = json.load(f)
    return {
        'movieFolderFormat': naming_data['folder']['default'],
        'standardMovieFormat': naming_data['file']['default']
    }

def get_current_naming() -> dict:
    """Fetch current naming from Radarr API."""
    cmd = [
        "curl", "-s",
        "-H", f"X-Api-Key: {RADARR_API_KEY}",
        f"{RADARR_URL}/api/v3/config/naming"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    return {
        'movieFolderFormat': data['movieFolderFormat'],
        'standardMovieFormat': data['standardMovieFormat']
    }

def main() -> int:
    """Validate naming configuration."""
    if not RADARR_API_KEY:
        print("ERROR: API_KEY_RADARR not set", file=sys.stderr)
        return 1
    
    if not os.path.exists(NAMING_JSON_PATH):
        print(f"ERROR: naming.json not found at {NAMING_JSON_PATH}", file=sys.stderr)
        return 1
    
    expected = get_expected_naming()
    current = get_current_naming()
    
    issues = []
    
    if current['movieFolderFormat'] != expected['movieFolderFormat']:
        issues.append("Folder Format Mismatch")
        issues.append(f"  Expected: {expected['movieFolderFormat']}")
        issues.append(f"  Current:  {current['movieFolderFormat']}")
    
    if current['standardMovieFormat'] != expected['standardMovieFormat']:
        issues.append("File Format Mismatch")
        issues.append(f"  Expected: {expected['standardMovieFormat']}")
        issues.append(f"  Current:  {current['standardMovieFormat']}")
    
    if issues:
        print("❌ NAMING CONFIGURATION DRIFT DETECTED!", file=sys.stderr)
        print("", file=sys.stderr)
        for issue in issues:
            print(issue, file=sys.stderr)
        print("", file=sys.stderr)
        print("AutoConfig may have overwritten the naming configuration!", file=sys.stderr)
        print("Fix: Restore naming.json and ensure it exists before Radarr starts", file=sys.stderr)
        return 1
    
    print("✅ Naming configuration matches expected format")
    print(f"   Folder: {current['movieFolderFormat']}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
