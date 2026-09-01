#!/usr/bin/env python3
"""Classify qBittorrent torrents into pollution buckets and print a cleanup plan.

READ-ONLY BY CONSTRUCTION. This script has no deletion code path -- not a flag,
not a branch, not a commented-out call. It issues GET requests only. If you want
something deleted, that is a separate, reviewed change.

Full analysis and the reasoning behind each bucket:
`docs/arr-qbittorrent-pollution.md`.

Buckets
-------
A  seeding toward the global share goal            -> working as intended
D  complete + paused but the *arr refuses to import -> junk (fake release)
E  never finished downloading, swarm is dead        -> junk once aged
F  no *arr history at all (series/movie deleted)    -> junk, unreachable by any *arr
Z  anything that matches none of the above          -> inspect by hand

Exit codes: 0 clean run, 1 partial (a service was unreachable), 2 fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
CONFIG_DIR_DEFAULT = REPO_ROOT / ".docker-config"

QBIT_URL = "http://127.0.0.1:8080"
ARRS: dict[str, tuple[int, str]] = {
  "sonarr": (8989, "v3"),
  "radarr": (7878, "v3"),
  "lidarr": (8686, "v1"),
}

# Bucket E is only proposed for removal once it is provably dead, not merely slow.
STALE_DAYS = 14.0
GIB = 1024**3
# Safety stop for history paging. Bucket F (propose-for-deletion) is derived from
# "absent from history", so an incomplete history would over-report it -- if this
# cap is ever hit the run reports itself degraded rather than proposing deletions
# it cannot justify.
MAX_HISTORY_PAGES = 40

BUCKET_LABELS = {
  "A": "seeding toward the global goal (working as intended)",
  "D": "complete + paused, import blocked by the *arr",
  "E": "never imported: stalled / dead swarm",
  "F": "orphaned: no *arr history (series or movie deleted)",
  "Z": "unclassified - inspect by hand",
}


@dataclass
class Torrent:
  """One qBittorrent torrent, reduced to the fields this analysis needs."""

  hash: str
  name: str
  category: str
  state: str
  size: int
  ratio: float
  seeding_time: int
  added_on: int
  completion_on: int
  progress: float
  availability: float
  private: bool | None
  content_path: str

  @property
  def age_days(self) -> float:
    return (time.time() - self.added_on) / 86400 if self.added_on else 0.0

  @property
  def dead_swarm(self) -> bool:
    """No complete copy visible and nothing downloaded -- it cannot finish."""
    return self.progress == 0.0 and self.availability < 1.0


@dataclass
class Plan:
  buckets: dict[str, list[Torrent]] = field(default_factory=dict)
  goal_ratio: float = 0.0
  goal_minutes: float = 0.0
  limits_enabled: bool = False
  pause_action: bool = False
  degraded: list[str] = field(default_factory=list)


def _read_api_key(config_dir: Path, app: str) -> str | None:
  """Pull an *arr API key out of its config.xml. Never logged."""
  path = config_dir / app / "config.xml"
  if not path.is_file():
    return None
  for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    if "<ApiKey>" in line:
      return line.split("<ApiKey>", 1)[1].split("</ApiKey>", 1)[0].strip()
  return None


def qbit_session(user: str, password: str) -> requests.Session | None:
  """Log in to qBittorrent. Returns None if authentication fails."""
  session = requests.Session()
  try:
    resp = session.post(
      f"{QBIT_URL}/api/v2/auth/login",
      data={"username": user, "password": password},
      timeout=30,
    )
  except requests.RequestException:
    return None
  return session if resp.status_code in (200, 204) else None


def fetch_torrents(session: requests.Session) -> list[Torrent]:
  resp = session.get(f"{QBIT_URL}/api/v2/torrents/info", timeout=60)
  resp.raise_for_status()
  return [
    Torrent(
      hash=t["hash"].lower(),
      name=t.get("name", ""),
      category=t.get("category", ""),
      state=t.get("state", ""),
      size=int(t.get("size", 0)),
      ratio=float(t.get("ratio", 0.0)),
      seeding_time=int(t.get("seeding_time", 0)),
      added_on=int(t.get("added_on", 0)),
      completion_on=int(t.get("completion_on", 0)),
      progress=float(t.get("progress", 0.0)),
      availability=float(t.get("availability", 0.0)),
      private=t.get("private"),
      content_path=t.get("content_path", ""),
    )
    for t in resp.json()
  ]


def fetch_share_goals(session: requests.Session) -> tuple[bool, float, float, bool]:
  """Return (limits_enabled, ratio_goal, minutes_goal, action_is_pause)."""
  prefs = session.get(f"{QBIT_URL}/api/v2/app/preferences", timeout=30).json()
  ratio = float(prefs.get("max_ratio", 0)) if prefs.get("max_ratio_enabled") else 0.0
  minutes = float(prefs.get("max_seeding_time", 0)) if prefs.get("max_seeding_time_enabled") else 0.0
  enabled = bool(prefs.get("max_ratio_enabled") or prefs.get("max_seeding_time_enabled"))
  return enabled, ratio, minutes, int(prefs.get("max_ratio_act", 0)) == 0


def _arr_get(app: str, path: str, key: str) -> Any:
  port, api = ARRS[app]
  resp = requests.get(
    f"http://127.0.0.1:{port}/api/{api}/{path}",
    headers={"X-Api-Key": key},
    timeout=60,
  )
  resp.raise_for_status()
  return resp.json()


def uses_qbittorrent(app: str, key: str) -> bool:
  """True if this *arr actually has an enabled qBittorrent download client.

  Lidarr on this stack downloads via Slskd, so its history is irrelevant to
  qBittorrent bucketing -- and it is enormous (839,800 records at the time of
  writing, ~1,680 pages). Skipping it is both correct and the difference
  between a 2-second run and a hang.
  """
  try:
    for client in _arr_get(app, "downloadclient", key):
      if client.get("implementation") == "QBittorrent" and client.get("enable"):
        return True
  except (requests.RequestException, ValueError):
    return True  # can't tell -- assume yes rather than silently under-report
  return False


def fetch_arr_state(config_dir: Path) -> tuple[set[str], set[str], list[str]]:
  """Return (hashes in a queue, hashes anywhere in history, degraded messages).

  History is paged in full: a partial sample silently misclassifies the oldest
  torrents into bucket F, which is exactly the bucket that proposes deletion.
  """
  queued: set[str] = set()
  known: set[str] = set()
  degraded: list[str] = []
  for app in ARRS:
    key = _read_api_key(config_dir, app)
    if not key:
      degraded.append(f"{app}: no API key at {config_dir / app / 'config.xml'}")
      continue
    try:
      queue = _arr_get(app, "queue?pageSize=1000&includeUnknownItems=true", key)
      for record in queue.get("records", []):
        if record.get("downloadId"):
          queued.add(record["downloadId"].lower())
      if not uses_qbittorrent(app, key):
        continue
      page = 1
      while True:
        hist = _arr_get(app, f"history?page={page}&pageSize=500", key)
        records = hist.get("records", [])
        if not records:
          break
        for record in records:
          if record.get("downloadId"):
            known.add(record["downloadId"].lower())
        total = int(hist.get("totalRecords", 0))
        if page * 500 >= total:
          break
        if page > MAX_HISTORY_PAGES:
          degraded.append(
            f"{app}: history exceeds {MAX_HISTORY_PAGES * 500} records; "
            "bucket F may over-report. Raise MAX_HISTORY_PAGES to be sure."
          )
          break
        page += 1
    except (requests.RequestException, ValueError) as exc:
      degraded.append(f"{app}: {exc}")
  return queued, known, degraded


def classify(
  torrents: list[Torrent],
  queued: set[str],
  known: set[str],
  goal_ratio: float,
  goal_minutes: float,
) -> dict[str, list[Torrent]]:
  """Sort torrents into buckets. Pure -- no I/O, so it is unit-testable."""
  buckets: dict[str, list[Torrent]] = {k: [] for k in BUCKET_LABELS}
  for tor in torrents:
    in_queue = tor.hash in queued
    goal_met = (goal_ratio and tor.ratio >= goal_ratio) or (
      goal_minutes and tor.seeding_time >= goal_minutes * 60
    )
    if tor.category.startswith("arr-") and tor.hash not in known:
      buckets["F"].append(tor)
    elif in_queue and tor.state == "stoppedUP":
      buckets["D"].append(tor)
    elif in_queue and tor.state in ("stalledDL", "queuedDL", "metaDL", "downloading"):
      buckets["E"].append(tor)
    elif not in_queue and (
      (tor.state in ("queuedUP", "uploading", "stalledUP") and not goal_met)
      or (tor.state == "stoppedUP" and goal_met)
    ):
      # Either still working toward the goal, or already paused having met it --
      # both are the removal contract behaving correctly.
      buckets["A"].append(tor)
    else:
      buckets["Z"].append(tor)
  return buckets


def proposed_for_removal(buckets: dict[str, list[Torrent]]) -> dict[str, list[Torrent]]:
  """The subset this report would put in front of a human, with its rule.

  Deliberately conservative: a torrent whose tracker cannot be determined
  (no metadata yet, so `private` is None) is never proposed.
  """
  return {
    "1. import blocked, fake release (bucket D)": [
      t for t in buckets["D"] if t.private is False
    ],
    f"2. dead swarm >{STALE_DAYS:.0f}d at 0% (bucket E)": [
      t for t in buckets["E"] if t.private is False and t.age_days > STALE_DAYS and t.dead_swarm
    ],
    "3. orphaned by a deleted series (bucket F)": [
      t for t in buckets["F"] if t.private is False
    ],
  }


def render(plan: Plan, detail_bucket: str | None) -> None:
  print("qBittorrent pollution report  (READ-ONLY -- nothing was changed)")
  print("=" * 72)
  if not plan.limits_enabled:
    print("!! qBittorrent has NO global share limits: torrents can never auto-pause,")
    print("!! so the *arrs will never be asked to remove them.")
  else:
    print(
      f"global goal: ratio >= {plan.goal_ratio} OR seeding >= {plan.goal_minutes:.0f} min"
      f"   action={'Pause (correct)' if plan.pause_action else 'REMOVE (the *arrs reject this)'}"
    )
  print()
  total_n = sum(len(v) for v in plan.buckets.values())
  total_b = sum(t.size for v in plan.buckets.values() for t in v)
  print(f"{'BUCKET':<58}{'N':>5}{'SIZE':>11}")
  print("-" * 74)
  for key, label in BUCKET_LABELS.items():
    items = plan.buckets.get(key, [])
    if not items:
      continue
    size = sum(t.size for t in items)
    print(f"{key}. {label:<55}{len(items):>5}{size / GIB:>9.1f}G")
  print("-" * 74)
  print(f"{'TOTAL':<58}{total_n:>5}{total_b / GIB:>9.1f}G")
  print()
  print("PROPOSED FOR YOUR APPROVAL (not executed, ordered by increasing risk)")
  print("-" * 74)
  grand = 0
  for label, items in proposed_for_removal(plan.buckets).items():
    size = sum(t.size for t in items)
    grand += size
    print(f"  {label:<56}{len(items):>4}{size / GIB:>9.1f}G")
  print(f"  {'would reclaim':<56}{'':>4}{grand / GIB:>9.1f}G")
  print()
  print("  Excluded on purpose: torrents whose tracker cannot be determined")
  print("  (no metadata yet), and anything still seeding toward the goal.")
  if detail_bucket:
    items = plan.buckets.get(detail_bucket.upper(), [])
    print()
    print(f"BUCKET {detail_bucket.upper()} -- {BUCKET_LABELS.get(detail_bucket.upper(), '?')}")
    print("-" * 74)
    for tor in sorted(items, key=lambda t: -t.size):
      print(
        f"  {tor.size / GIB:7.1f}G  r={tor.ratio:5.2f}  {tor.age_days:5.1f}d  "
        f"{tor.state:<11} {tor.name[:52]}"
      )
  if plan.degraded:
    print()
    print("DEGRADED -- results are incomplete:")
    for msg in plan.degraded:
      print(f"  ! {msg}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--bucket", help="print every torrent in one bucket (A/D/E/F/Z)")
  parser.add_argument("--detail", action="store_true", help="alias for --bucket with all buckets")
  parser.add_argument("--json", action="store_true", help="emit machine-readable output")
  parser.add_argument(
    "--config-dir",
    type=Path,
    default=Path(os.environ.get("CONFIG_DIRECTORY", CONFIG_DIR_DEFAULT)),
    help="where the *arr config.xml files live",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  load_dotenv(ENV_PATH)
  user = os.environ.get("QBITTORRENT_USER", "")
  password = os.environ.get("QBITTORRENT_PASS", "")
  if not user or not password:
    print("ERROR: QBITTORRENT_USER / QBITTORRENT_PASS missing from .env", file=sys.stderr)
    return 2
  session = qbit_session(user, password)
  if session is None:
    print(f"ERROR: cannot authenticate to qBittorrent at {QBIT_URL}", file=sys.stderr)
    return 2
  try:
    torrents = fetch_torrents(session)
    enabled, goal_ratio, goal_minutes, pause_action = fetch_share_goals(session)
  except (requests.RequestException, ValueError) as exc:
    print(f"ERROR: reading qBittorrent failed: {exc}", file=sys.stderr)
    return 2

  queued, known, degraded = fetch_arr_state(args.config_dir)
  plan = Plan(
    buckets=classify(torrents, queued, known, goal_ratio, goal_minutes),
    goal_ratio=goal_ratio,
    goal_minutes=goal_minutes,
    limits_enabled=enabled,
    pause_action=pause_action,
    degraded=degraded,
  )

  if args.json:
    print(
      json.dumps(
        {
          k: [{"hash": t.hash, "name": t.name, "size": t.size, "state": t.state} for t in v]
          for k, v in plan.buckets.items()
        },
        indent=2,
      )
    )
  else:
    render(plan, args.bucket or ("F" if args.detail else None))

  return 1 if degraded else 0


if __name__ == "__main__":
  sys.exit(main())
