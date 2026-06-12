#!/usr/bin/env python3
"""Enforce qBittorrent Auto Torrent Management so categories drive save paths.

Background
----------
The *arr apps tag torrents with categories (arr-sonarr / arr-radarr) whose save
paths are correct (/downloads/complete/{sonarr,radarr}), but qBittorrent's Auto
Torrent Management (TMM) is OFF — so the category never drives the save path and
every torrent lands in the global default /downloads/complete/manual. This
script turns TMM on and flips existing torrents to auto-managed so qBittorrent
relocates each into its category folder (an instant same-filesystem rename;
hardlinks into the library are preserved). It also points qBittorrent's temp
(incomplete) path at /downloads/incomplete/qbittorrent so it stops sharing one
flat incomplete dir with slskd.

Idempotent: a run with TMM already on and all torrents managed is a no-op.

Exit codes
----------
  0 success (or dry-run / nothing to change)
  1 partial (some API calls failed; details on stderr)
  2 fatal (config missing, qBittorrent unreachable, auth failed)

Environment
-----------
  QBITTORRENT_USER   (required) WebUI username
  QBITTORRENT_PASS   (required) WebUI password
  QBITTORRENT_HOST   (default: http://localhost:8080)

Usage
-----
  python scripts/qbittorrent_settings_enforce.py            # ACT
  python scripts/qbittorrent_settings_enforce.py --dry-run  # preview, exit 0
"""

from __future__ import annotations

import argparse
import http.cookiejar  # noqa: F401 – used by QbtClient (Task 5)
import json  # noqa: F401 – used by QbtClient (Task 5)
import os
import sys
import urllib.error  # noqa: F401 – used by QbtClient (Task 5)
import urllib.parse  # noqa: F401 – used by QbtClient (Task 5)
import urllib.request  # noqa: F401 – used by QbtClient (Task 5)

if "QBITTORRENT_USER" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_QBT_HOST = "http://localhost:8080"
DESIRED_PREFS = {
  "auto_tmm_enabled": True,
  "category_changed_tmm_enabled": True,
  "save_path_changed_tmm_enabled": True,
  "temp_path_enabled": True,
  "temp_path": "/downloads/incomplete/qbittorrent",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Enable qBittorrent Auto TMM and relocate existing torrents into category folders."
  )
  parser.add_argument("--dry-run", action="store_true", help="Report the plan and exit 0.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  _args = parse_args(argv)
  _host = os.environ.get("QBITTORRENT_HOST", DEFAULT_QBT_HOST).rstrip("/")
  user = os.environ.get("QBITTORRENT_USER")
  pw = os.environ.get("QBITTORRENT_PASS")
  if not user or not pw:
    print("ERROR: QBITTORRENT_USER / QBITTORRENT_PASS not set (check .env)", file=sys.stderr)
    return 2
  # Wired in later tasks.
  return 0


if __name__ == "__main__":
  sys.exit(main())
