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


def plan_pref_changes(current: dict, desired: dict) -> dict:
  """Return the subset of ``desired`` whose value differs from ``current``.

  Pure. Keys absent from ``current`` count as differing (will be set).
  """
  return {k: v for k, v in desired.items() if current.get(k) != v}


def collect_unmanaged_hashes(torrents: list[dict]) -> list[str]:
  """Hashes of torrents not already auto-managed (TMM off / missing).

  Pure over ``GET /api/v2/torrents/info``. Order preserved.
  """
  return [t["hash"] for t in torrents if t.get("hash") and not t.get("auto_tmm", False)]


def summarize_targets(torrents: list[dict], categories: dict) -> dict[str, int]:
  """Count where each torrent will land once auto-managed.

  Pure. A torrent's target is its category's ``savePath``; an empty/missing
  category or empty savePath falls back to the qBittorrent default save path
  (reported as the literal ``"(default save path)"``).
  """
  out: dict[str, int] = {}
  for t in torrents:
    cat = t.get("category") or ""
    save = categories.get(cat, {}).get("savePath") or ""
    key = save if save else "(default save path)"
    out[key] = out.get(key, 0) + 1
  return out


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
