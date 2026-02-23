#!/usr/bin/env python3
"""Update slow-responding indexers with longer timeout."""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_KEY_PROWLARR")
BASE_URL = "http://localhost:9696/api/v1/indexer"
TIMEOUT = 60  # 60 second timeout

# Indexers to update
UPDATES = {
    70: 18,  # Internet Archive
    69: 22,  # LinuxTracker
}

headers = {"X-Api-Key": API_KEY}

for indexer_id, new_priority in UPDATES.items():
    print(f"\nUpdating indexer {indexer_id} to priority {new_priority}...")
    
    try:
        # Get current config
        print(f"  Fetching current config...")
        response = requests.get(f"{BASE_URL}/{indexer_id}", headers=headers, timeout=TIMEOUT)
        
        if response.status_code != 200:
            print(f"  ✗ Failed to fetch: {response.status_code}")
            continue
        
        indexer = response.json()
        print(f"  Current: {indexer['name']} (priority: {indexer.get('priority', 'N/A')})")
        
        # Update priority
        indexer['priority'] = new_priority
        
        print(f"  Sending update...")
        response = requests.put(f"{BASE_URL}/{indexer_id}", json=indexer, headers=headers, timeout=TIMEOUT)
        
        if response.status_code in [200, 202]:
            print(f"  ✓ Successfully updated {indexer['name']} -> priority {new_priority}")
        else:
            print(f"  ✗ Failed: {response.status_code} - {response.text[:100]}")
            
    except requests.Timeout:
        print(f"  ✗ Timeout after {TIMEOUT} seconds")
    except Exception as e:
        print(f"  ✗ Error: {e}")

print("\n✅ Done!")
