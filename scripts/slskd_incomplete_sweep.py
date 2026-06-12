#!/usr/bin/env python3
"""Sweep orphaned dirs from the slskd-owned zones of /downloads/incomplete.

Background
----------
slskd writes in-progress Soulseek downloads under an incomplete dir. After the
Phase 1 tidy, slskd uses /downloads/incomplete/slskd and qBittorrent uses
/downloads/incomplete/qbittorrent -- but legacy orphan album folders remain at
the /downloads/incomplete root (from before the split), and new orphans can
accumulate under incomplete/slskd whenever a Soulseek transfer is cancelled or
dies mid-download. This script deletes those orphans safely.

It NEVER enters /downloads/incomplete/qbittorrent -- qBittorrent owns that temp
dir and deleting it would corrupt live torrents.

A candidate dir is deleted only if it clears ALL three gates:
  1. not referenced by an active slskd transfer (by dir basename),
  2. not referenced by a live qBittorrent torrent (save_path/content_path/name
     basename) -- qBittorrent historically shared /downloads/incomplete,
  3. its mtime is older than --min-age-hours (default 24).

If EITHER reference fetch fails, the sweep aborts (exit 2) rather than deleting
with an incomplete protection set.

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some rmtrees failed; details on stderr)
  2 fatal (config missing, slskd/qBittorrent unreachable, containment violation)

Environment
-----------
  API_KEY_SLSKD      (required) administrator key for slskd /api/v0
  SLSKD_HOST         (default: http://localhost:5030)
  QBITTORRENT_USER   (required) qBittorrent WebUI username
  QBITTORRENT_PASS   (required) qBittorrent WebUI password
  QBITTORRENT_HOST   (default: http://localhost:8080)
  INCOMPLETE_DIR     (default: /mnt/drive/downloads/incomplete)

Usage
-----
  python scripts/slskd_incomplete_sweep.py --dry-run
  python scripts/slskd_incomplete_sweep.py --min-age-hours 24 --limit 50
"""

from __future__ import annotations

import argparse
import datetime as _dt
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if "API_KEY_SLSKD" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_SLSKD_HOST = "http://localhost:5030"
DEFAULT_QBT_HOST = "http://localhost:8080"
DEFAULT_INCOMPLETE_DIR = "/mnt/drive/downloads/incomplete"
DEFAULT_MIN_AGE_HOURS = 24.0
MANAGED_SUBDIRS = frozenset({"qbittorrent", "slskd"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Sweep orphaned dirs from the slskd-owned zones of /downloads/incomplete."
  )
  parser.add_argument(
    "--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
    help=f"Only delete dirs older than this (default {DEFAULT_MIN_AGE_HOURS}).",
  )
  parser.add_argument("--limit", type=int, default=0, help="Cap deletions per run (0 = unlimited).")
  parser.add_argument("--dry-run", action="store_true", help="Report the plan and exit 0.")
  return parser.parse_args(argv)


def _trailing_segment(path: str) -> str:
  """Last path component, normalizing both `\\` and `/` separators."""
  if not path:
    return ""
  normalized = path.replace("/", "\\").rstrip("\\")
  if "\\" not in normalized:
    return normalized
  return normalized.rsplit("\\", 1)[-1]


def plan_incomplete_sweep(
  candidates: list[tuple[Path, float]],
  slskd_refs: set[str],
  qbt_refs: set[str],
  *,
  now: _dt.datetime,
  min_age_hours: float,
) -> list[Path]:
  """Return candidate dirs to delete: orphaned by both ref sets AND old enough.

  Pure. ``candidates`` are (dir, mtime_epoch) pairs already restricted to the
  sweep zones by the caller. A dir is deleted only if its basename is in
  neither ``slskd_refs`` nor ``qbt_refs`` and its mtime is older than
  ``min_age_hours``. Order preserved.
  """
  cutoff = (now - _dt.timedelta(hours=min_age_hours)).timestamp()
  out: list[Path] = []
  for path, mtime in candidates:
    if path.name in slskd_refs or path.name in qbt_refs:
      continue
    if mtime >= cutoff:
      continue
    out.append(path)
  return out


def _request(method: str, url: str, api_key: str, *, timeout: int = 15) -> tuple[int, bytes]:
  req = urllib.request.Request(url, method=method, headers={"X-API-Key": api_key})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return resp.status, resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read()


def fetch_slskd_refs(host: str, api_key: str) -> set[str]:
  """Basenames of dirs referenced by ANY current slskd transfer (all states).

  Raises RuntimeError on HTTP/JSON failure so main() aborts rather than sweeping
  with an incomplete protection set.
  """
  status, body = _request("GET", f"{host}/api/v0/transfers/downloads", api_key)
  if status >= 400:
    raise RuntimeError(f"slskd transfers returned HTTP {status}")
  try:
    data = json.loads(body) if body else []
  except json.JSONDecodeError as exc:
    raise RuntimeError(f"slskd transfers returned malformed JSON: {exc}") from exc
  refs: set[str] = set()
  for user in data if isinstance(data, list) else []:
    for directory in user.get("directories", []):
      seg = _trailing_segment(directory.get("directory", ""))
      if seg:
        refs.add(seg)
  return refs


def fetch_qbt_refs(host: str, user: str, pw: str) -> set[str]:
  """Basenames referenced by live qBittorrent torrents (save_path/content_path/name).

  Raises RuntimeError on auth/HTTP/JSON failure (main() aborts on a partial set).
  """
  opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
  )
  login = urllib.parse.urlencode({"username": user, "password": pw}).encode()
  try:
    req = urllib.request.Request(f"{host}/api/v2/auth/login", data=login, headers={"Referer": host})
    with opener.open(req, timeout=15) as resp:  # noqa: S310 - localhost
      if resp.status not in (200, 204):  # v5.2.1 returns 204
        raise RuntimeError(f"qBittorrent login HTTP {resp.status}")
    req2 = urllib.request.Request(f"{host}/api/v2/torrents/info", headers={"Referer": host})
    with opener.open(req2, timeout=15) as resp:  # noqa: S310 - localhost
      torrents = json.loads(resp.read())
  except (urllib.error.URLError, json.JSONDecodeError) as exc:
    raise RuntimeError(f"qBittorrent query failed: {exc}") from exc
  refs: set[str] = set()
  for t in torrents:
    for key in ("save_path", "content_path", "name"):
      val = t.get(key)
      if isinstance(val, str) and val:
        base = os.path.basename(val.rstrip("/").rstrip("\\"))
        if base:
          refs.add(base)
  return refs


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  slskd_key = os.environ.get("API_KEY_SLSKD")
  qbt_host = os.environ.get("QBITTORRENT_HOST", DEFAULT_QBT_HOST).rstrip("/")
  qbt_user = os.environ.get("QBITTORRENT_USER")
  qbt_pass = os.environ.get("QBITTORRENT_PASS")
  incomplete_dir = Path(os.environ.get("INCOMPLETE_DIR", DEFAULT_INCOMPLETE_DIR))
  if not slskd_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2
  if not qbt_user or not qbt_pass:
    print("ERROR: QBITTORRENT_USER / QBITTORRENT_PASS not set (check .env)", file=sys.stderr)
    return 2
  # Wired in later tasks.
  _ = args, slskd_host, qbt_host, incomplete_dir
  return 0


if __name__ == "__main__":
  sys.exit(main())
