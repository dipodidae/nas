#!/usr/bin/env python3
"""
Jellyfin notification script for Radarr/Sonarr.
Triggers immediate library scan when media is imported.
"""

import os
import sys
import urllib.request
import urllib.error
import json


def notify_jellyfin():
    """Trigger Jellyfin library scan for the Movies library."""
    # Get Jellyfin connection details from environment or defaults
    host = os.getenv("JELLYFIN_HOST", "jellyfin")
    port = int(os.getenv("JELLYFIN_PORT", "8096"))
    api_key = os.getenv("JELLYFIN_API_KEY", "")
    
    if not api_key:
        print("ERROR: JELLYFIN_API_KEY not set", file=sys.stderr)
        return False
    
    # Construct API URL for library refresh
    url = f"http://{host}:{port}/Library/Refresh?api_key={api_key}"
    
    try:
        # Send POST request to trigger library scan
        req = urllib.request.Request(url, method="POST")
        req.add_header("Content-Type", "application/json")
        
        with urllib.request.urlopen(req, timeout=10) as response:
            if response.status == 204 or response.status == 200:
                print(f"Successfully triggered Jellyfin library scan")
                return True
            else:
                print(f"Unexpected response: {response.status}", file=sys.stderr)
                return False
                
    except urllib.error.URLError as e:
        print(f"Failed to connect to Jellyfin: {e}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Error triggering Jellyfin scan: {e}", file=sys.stderr)
        return False


def main():
    """Main entry point when called from Radarr/Sonarr."""
    # Radarr sets these environment variables on import
    event_type = os.getenv("radarr_eventtype", os.getenv("sonarr_eventtype", "Unknown"))
    
    print(f"Event type: {event_type}")
    
    # Only trigger on Download/Import events
    if event_type in ["Download", "Upgrade", "Rename"]:
        success = notify_jellyfin()
        sys.exit(0 if success else 1)
    else:
        print(f"Skipping event type: {event_type}")
        sys.exit(0)


if __name__ == "__main__":
    main()
