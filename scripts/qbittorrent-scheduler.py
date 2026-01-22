#!/usr/bin/env python3
"""
qBittorrent Time-Based Scheduler
Switches between aggressive downloading (01:00-08:00) and idle mode (08:00-01:00).
Runs every minute via cron or systemd timer.
"""
import sys
import json
from datetime import datetime
from pathlib import Path
import requests
import logging

# Configuration
QB_HOST = "http://localhost:8080"
QB_USER = "admin"
QB_PASS = "sotm19858514"

# Speed limits (KB/s, 0 = unlimited)
DOWNLOAD_WINDOW_START = 1  # 01:00
DOWNLOAD_WINDOW_END = 8    # 08:00

AGGRESSIVE_CONFIG = {
    "dl_limit": 0,              # Unlimited download
    "up_limit": 5000 * 1024,    # 5 MB/s upload (keep some ratio for health)
    "alt_dl_limit": 0,
    "alt_up_limit": 0,
    "scheduler_enabled": False,  # We handle scheduling ourselves
}

IDLE_CONFIG = {
    "dl_limit": 50 * 1024,      # 50 KB/s download (near zero)
    "up_limit": 50 * 1024,      # 50 KB/s upload
    "alt_dl_limit": 0,
    "alt_up_limit": 0,
    "scheduler_enabled": False,
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/qbittorrent-scheduler.log'),
        logging.StreamHandler()
    ]
)

class QBittorrentAPI:
    def __init__(self, host, username, password):
        self.host = host
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.headers.update({"Referer": host})
        
    def login(self):
        """Authenticate with qBittorrent"""
        try:
            response = self.session.post(
                f"{self.host}/api/v2/auth/login",
                data={"username": self.username, "password": self.password},
                timeout=10
            )
            if response.status_code == 200 and response.text == "Ok.":
                logging.info("✓ Authenticated successfully")
                return True
            logging.error(f"✗ Authentication failed: {response.text}")
            return False
        except requests.RequestException as e:
            logging.error(f"✗ Connection failed: {e}")
            return False
    
    def set_preferences(self, prefs):
        """Update qBittorrent preferences"""
        try:
            response = self.session.post(
                f"{self.host}/api/v2/app/setPreferences",
                data={"json": json.dumps(prefs)},
                timeout=10
            )
            if response.status_code == 200:
                return True
            logging.error(f"✗ Failed to set preferences: {response.status_code}")
            return False
        except requests.RequestException as e:
            logging.error(f"✗ Request failed: {e}")
            return False
    
    def pause_all(self):
        """Pause all active torrents"""
        try:
            response = self.session.post(
                f"{self.host}/api/v2/torrents/pause",
                data={"hashes": "all"},
                timeout=10
            )
            return response.status_code == 200
        except requests.RequestException as e:
            logging.error(f"✗ Failed to pause: {e}")
            return False
    
    def resume_all(self):
        """Resume all paused torrents"""
        try:
            response = self.session.post(
                f"{self.host}/api/v2/torrents/resume",
                data={"hashes": "all"},
                timeout=10
            )
            return response.status_code == 200
        except requests.RequestException as e:
            logging.error(f"✗ Failed to resume: {e}")
            return False

def is_download_window():
    """Check if current hour is within download window"""
    current_hour = datetime.now().hour
    if DOWNLOAD_WINDOW_START < DOWNLOAD_WINDOW_END:
        return DOWNLOAD_WINDOW_START <= current_hour < DOWNLOAD_WINDOW_END
    else:
        # Handle wraparound (e.g., 22:00 to 06:00)
        return current_hour >= DOWNLOAD_WINDOW_START or current_hour < DOWNLOAD_WINDOW_END

def main():
    api = QBittorrentAPI(QB_HOST, QB_USER, QB_PASS)
    
    if not api.login():
        logging.error("Cannot proceed without authentication")
        sys.exit(1)
    
    is_active = is_download_window()
    current_time = datetime.now().strftime("%H:%M")
    
    if is_active:
        logging.info(f"🚀 [{current_time}] ACTIVE WINDOW - Setting aggressive mode")
        if api.set_preferences(AGGRESSIVE_CONFIG):
            api.resume_all()
            logging.info("✓ Configured for maximum download speed")
    else:
        logging.info(f"💤 [{current_time}] IDLE WINDOW - Throttling to minimum")
        if api.set_preferences(IDLE_CONFIG):
            logging.info("✓ Throttled to 50 KB/s (near-idle)")
            # Optional: uncomment to pause instead of throttle
            # api.pause_all()
            # logging.info("✓ All torrents paused")

if __name__ == "__main__":
    main()
