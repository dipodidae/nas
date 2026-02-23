#!/usr/bin/env python3
"""
Trigger Jellyfin Movies library rescan.

This script initiates a library scan in Jellyfin to pick up renamed/moved movie files.
Jellyfin automatically detects moved files and updates its database accordingly.

Exit codes:
    0 - Success
    1 - Error (API key not set or request failed)
"""
from __future__ import annotations

import os
import subprocess
import sys

JELLYFIN_API_KEY = os.getenv("API_KEY_JELLYFIN")
JELLYFIN_URL = "http://localhost:8096"


def trigger_rescan() -> int:
    """Trigger Jellyfin library rescan via API."""
    if not JELLYFIN_API_KEY:
        print("ERROR: API_KEY_JELLYFIN not set", file=sys.stderr)
        return 1

    cmd = [
        "curl",
        "-s",
        "-X",
        "POST",
        f"{JELLYFIN_URL}/Library/Refresh",
        "-H",
        f"X-Emby-Token: {JELLYFIN_API_KEY}",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("✓ Jellyfin library rescan triggered")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"ERROR: Failed to trigger rescan: {e}", file=sys.stderr)
        print(f"  Output: {e.stderr}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(trigger_rescan())
