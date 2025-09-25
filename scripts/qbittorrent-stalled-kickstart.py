#!/usr/bin/env python3
"""qBittorrent stalled torrent kickstarter.

Identifies stalled torrents via qBittorrent Web API and performs a sequence of
"kick" actions (resume, reannounce, optional recheck) intended to nudge them
back into activity without destructive side effects.

Environment Variables (loaded from .env best-effort):
  QBITTORRENT_USER (required)
  QBITTORRENT_PASS (required)
  QBITTORRENT_HOST (optional, default: http://localhost)
  QBITTORRENT_PORT (optional, default: 8080)

Exit Codes:
  0 - success / no stalled torrents
  1 - partial (some actions failed)
  2 - fatal (auth failure / connectivity / configuration)

Usage examples:
  python scripts/qbittorrent-stalled-kickstart.py              # default actions
  python scripts/qbittorrent-stalled-kickstart.py --dry-run    # show what would happen
  python scripts/qbittorrent-stalled-kickstart.py --recheck --max 5
  python scripts/qbittorrent-stalled-kickstart.py --min-age 30 # ignore recent (<30m) torrents

Safety notes:
  * Rechecking can be I/O heavy — only performed when --recheck provided.
  * The script never deletes or force re-announces aggressively; one passive reannounce per run.
  * Actions operate only on torrents currently considered stalled by qBittorrent.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

try:  # pragma: no cover - convenience only
  from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
  def load_dotenv():  # type: ignore
    return False


load_dotenv()

DEFAULT_HOST = "http://localhost"
DEFAULT_PORT = 8080
API_BASE_SUFFIX = "/api/v2"

# Portable UTC (Python <3.11 lacks datetime.UTC). Use alias when available, fallback to timezone.utc.
UTC = datetime.UTC if hasattr(datetime, "UTC") else timezone.utc  # noqa: UP017 SIM108


@dataclass
class QBConfig:
  host: str
  port: int
  username: str
  password: str

  @property
  def base_url(self) -> str:
    # qBittorrent API root (no trailing slash)
    return f"{self.host}:{self.port}{API_BASE_SUFFIX}".rstrip("/")


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(description="Kickstart stalled qBittorrent torrents")
  p.add_argument("--dry-run", action="store_true", help="Show planned actions only")
  p.add_argument("--recheck", action="store_true", help="Also request a recheck (heavier operation)")
  p.add_argument("--max", type=int, default=None, help="Limit number of torrents to act on")
  p.add_argument(
    "--min-age",
    type=int,
    default=10,
    metavar="MINUTES",
    help="Ignore torrents added within this many minutes (default: 10)",
  )
  p.add_argument(
    "--filters",
    nargs="*",
    default=["stalled"],
    choices=["stalled", "stalled_uploading", "stalled_downloading"],
    help="API filters to query (combined unique set)",
  )
  p.add_argument(
    "--no-reannounce",
    action="store_true",
    help="Skip reannounce (only resume / optional recheck)",
  )
  return p.parse_args()


def build_config() -> QBConfig:
  user = os.getenv("QBITTORRENT_USER")
  pwd = os.getenv("QBITTORRENT_PASS")
  if not user or not pwd:
    print("❌ Missing required QBITTORRENT_USER / QBITTORRENT_PASS in environment/.env")
    sys.exit(2)
  host = os.getenv("QBITTORRENT_HOST", DEFAULT_HOST)
  port = int(os.getenv("QBITTORRENT_PORT", str(DEFAULT_PORT)))
  return QBConfig(host=host.rstrip("/"), port=port, username=user, password=pwd)


def authenticate(session: requests.Session, cfg: QBConfig) -> None:
  url = f"{cfg.base_url}/auth/login"
  resp = session.post(url, data={"username": cfg.username, "password": cfg.password}, timeout=10)
  if resp.text != "Ok.":
    print(f"❌ Authentication failed (status {resp.status_code}): {resp.text.strip()}")
    sys.exit(2)


def fetch_torrents(session: requests.Session, cfg: QBConfig, filter_name: str) -> list[dict[str, Any]]:
  url = f"{cfg.base_url}/torrents/info"
  resp = session.get(url, params={"filter": filter_name}, timeout=15)
  if resp.status_code != 200:
    raise RuntimeError(f"Failed to fetch torrents (filter={filter_name}): HTTP {resp.status_code}")
  try:
    return resp.json()
  except Exception as e:  # pragma: no cover - unexpected
    raise RuntimeError(f"Non-JSON response for torrents (filter={filter_name}): {e}") from e


def unique_torrents(torrent_lists: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
  seen: set[str] = set()
  result: list[dict[str, Any]] = []
  for lst in torrent_lists:
    for t in lst:
      h = t.get("hash")
      if h and h not in seen:
        seen.add(h)
        result.append(t)
  return result


def filter_by_age(torrents: list[dict[str, Any]], min_age_minutes: int) -> list[dict[str, Any]]:
  if min_age_minutes <= 0:
    return torrents
  cutoff = datetime.now(UTC) - timedelta(minutes=min_age_minutes)
  filtered: list[dict[str, Any]] = []
  for t in torrents:
    # qBittorrent returns 'added_on' (unix epoch) in seconds
    added_epoch = t.get("added_on")
    if isinstance(added_epoch, int):
      added_dt = datetime.fromtimestamp(added_epoch, tz=UTC)
      if added_dt <= cutoff:
        filtered.append(t)
    else:
      # If missing / unknown keep (be conservative in action)
      filtered.append(t)
  return filtered


def classify_state(t: dict[str, Any]) -> str:
  state = t.get("state", "")
  # Condense to a higher level bucket for reporting
  if state.startswith("stalled"):
    return "stalled"
  if state.startswith("paused"):
    return "paused"
  if state.endswith("DL"):
    return "downloading"
  if state.endswith("UP"):
    return "uploading"
  return state or "unknown"


def plan_actions(torrents: list[dict[str, Any]], do_recheck: bool) -> dict[str, list[str]]:
  """Return mapping of action -> list of hashes.

  We always try to resume (some clients show stalled while paused) and reannounce unless disabled.
  Recheck optional.
  """
  resume: list[str] = []
  reannounce: list[str] = []
  recheck: list[str] = []
  for t in torrents:
    h = t.get("hash")
    if not h:
      continue
    state = t.get("state", "")
    # Resume if paused
    if state.startswith("paused"):
      resume.append(h)
    # Reannounce all targeted torrents
    reannounce.append(h)
    if do_recheck and state.startswith("stalled"):
      recheck.append(h)
  return {"resume": resume, "reannounce": reannounce, "recheck": recheck}


def _batched(items: list[str], batch_size: int = 50) -> Iterable[list[str]]:
  for i in range(0, len(items), batch_size):
    yield items[i : i + batch_size]


def perform_action(
  session: requests.Session,
  cfg: QBConfig,
  endpoint: str,
  hashes: list[str],
  dry_run: bool,
) -> tuple[int, list[str]]:
  if not hashes:
    return 0, []
  fail: list[str] = []
  for batch in _batched(hashes):
    joined = "|".join(batch)
    url = f"{cfg.base_url}{endpoint}"
    if dry_run:
      print(f"🔍 DRY RUN: {endpoint} -> {len(batch)} torrents")
      continue
    try:
      resp = session.post(url, data={"hashes": joined}, timeout=20)
      if resp.status_code != 200:
        print(f"❌ {endpoint} batch failed (HTTP {resp.status_code})")
        fail.extend(batch)
      else:
        print(f"✅ {endpoint} {len(batch)} torrents")
        # Gentle pause to avoid hammering API
        time.sleep(0.2)
    except Exception as e:  # pragma: no cover - network error path
      print(f"❌ {endpoint} exception: {e}")
      fail.extend(batch)
  return (0 if not fail else 1), fail


def main() -> int:
  args = parse_args()
  cfg = build_config()
  print("🔧 qBittorrent Kickstart")
  print(f"   Host: {cfg.host}")
  print(f"   Port: {cfg.port}")
  print(f"   Filters: {', '.join(args.filters)}")
  print(f"   Min age: {args.min_age}m | Recheck: {args.recheck} | Dry-run: {args.dry_run}")

  session = requests.Session()
  try:
    authenticate(session, cfg)
  except SystemExit:
    return 2
  except Exception as e:  # pragma: no cover - unexpected
    print(f"❌ Authentication error: {e}")
    return 2

  try:
    lists = [fetch_torrents(session, cfg, f) for f in args.filters]
  except Exception as e:
    print(f"❌ Failed to query torrents: {e}")
    return 2

  combined = unique_torrents(lists)
  if not combined:
    print("🎉 No stalled torrents found (filters). Nothing to do.")
    return 0

  # Filter by age
  filtered = filter_by_age(combined, args.min_age)
  if not filtered:
    print("⏭️  All stalled torrents are younger than min-age; skipping.")
    return 0

  if args.max is not None and len(filtered) > args.max:
    filtered = filtered[: args.max]

  print(f"📦 Targeting {len(filtered)} torrent(s) out of {len(combined)} combined candidates")
  for t in filtered:
    name = t.get("name", "<unknown>")
    state_bucket = classify_state(t)
    size = t.get("size")
    sz_mb = f"{size/1024/1024:.1f}MB" if isinstance(size, int) else "?"
    print(f"  • {name} | {state_bucket} | {sz_mb}")

  plan = plan_actions(filtered, args.recheck)
  # Optionally skip reannounce
  if args.no_reannounce:
    plan["reannounce"] = []

  print(
    "\n📝 Planned actions:\n"
    f"    resume: {len(plan['resume'])}\n"
    f"    reannounce: {len(plan['reannounce'])}\n"
    f"    recheck: {len(plan['recheck'])}"
  )

  overall_fail = 0
  # Execute in safe order: resume -> reannounce -> recheck
  order = [
    ("/torrents/resume", plan["resume"]),
    ("/torrents/reannounce", plan["reannounce"]),
    ("/torrents/recheck", plan["recheck"]),
  ]
  for endpoint, hashes in order:
    code, failed = perform_action(session, cfg, endpoint, hashes, args.dry_run)
    if code != 0:
      overall_fail = 1
      if failed:
        print(f"   → Failed {endpoint} for {len(failed)} torrent(s)")

  if overall_fail:
    print("⚠️ Completed with some failed actions")
    return 1
  print("✅ Kickstart sequence complete")
  return 0


if __name__ == "__main__":  # pragma: no cover
  sys.exit(main())
