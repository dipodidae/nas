#!/usr/bin/env python3
"""Apply the approved qBittorrent cleanup plan. DRY-RUN BY DEFAULT.

Companion to `scripts/qbit_cleanup_plan.py`, which classifies; this one acts.
Reasoning for every rule: `docs/arr-qbittorrent-pollution.md`.

Nothing is removed unless `--execute` is passed. Every run, dry or not, writes a
manifest of exactly what was in scope to `logs/qbit_cleanup_<ts>.json` first, so
a mistake is at least auditable after the fact.

Removal routes differ by bucket, deliberately:

  D, E  still tracked in an *arr queue -> removed via the *arr's queue API with
        removeFromClient=true and blocklist=true, so the release is blocklisted
        and the *arr searches for a replacement. Deleting these straight out of
        qBittorrent would leave a stranded queue item and no re-search.
  F     unknown to every *arr -> removed directly from qBittorrent with
        deleteFiles=true. No *arr has a record to clean up.

Exit codes: 0 success, 1 partial, 2 fatal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qbit_cleanup_plan import (  # noqa: E402
    ARRS,
    GIB,
    QBIT_URL,
    Torrent,
    _read_api_key,
    classify,
    fetch_arr_state,
    fetch_share_goals,
    fetch_torrents,
    proposed_for_removal,
    qbit_session,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
LOG_DIR = REPO_ROOT / "logs"


def arr_queue_index(config_dir: Path) -> dict[str, tuple[str, int]]:
  """Map torrent hash -> (arr name, queue record id) for everything queued."""
  index: dict[str, tuple[str, int]] = {}
  for app in ARRS:
    key = _read_api_key(config_dir, app)
    if not key:
      continue
    port, api = ARRS[app]
    try:
      resp = requests.get(
        f"http://127.0.0.1:{port}/api/{api}/queue?pageSize=1000&includeUnknownItems=true",
        headers={"X-Api-Key": key},
        timeout=60,
      )
      resp.raise_for_status()
      for record in resp.json().get("records", []):
        dl = (record.get("downloadId") or "").lower()
        if dl:
          index.setdefault(dl, (app, record["id"]))
    except (requests.RequestException, ValueError):
      continue
  return index


def remove_via_arr(app: str, record_id: int, key: str) -> tuple[bool, str]:
  """Delete a queue item, its client torrent and its files, and blocklist it."""
  port, api = ARRS[app]
  try:
    resp = requests.delete(
      f"http://127.0.0.1:{port}/api/{api}/queue/{record_id}",
      params={"removeFromClient": "true", "blocklist": "true", "skipRedownload": "false"},
      headers={"X-Api-Key": key},
      timeout=120,
    )
  except requests.RequestException as exc:
    return False, str(exc)
  return (resp.status_code in (200, 201, 202, 204)), f"HTTP {resp.status_code}"


def remove_via_qbit(session: requests.Session, hashes: list[str]) -> tuple[bool, str]:
  """Delete torrents and their data straight from qBittorrent."""
  if not hashes:
    return True, "nothing to do"
  try:
    resp = session.post(
      f"{QBIT_URL}/api/v2/torrents/delete",
      data={"hashes": "|".join(hashes), "deleteFiles": "true"},
      timeout=180,
    )
  except requests.RequestException as exc:
    return False, str(exc)
  return (resp.status_code in (200, 204)), f"HTTP {resp.status_code}"


def write_manifest(scope: dict[str, list[Torrent]], executed: bool) -> Path:
  LOG_DIR.mkdir(exist_ok=True)
  path = LOG_DIR / f"qbit_cleanup_{int(time.time())}.json"
  path.write_text(
    json.dumps(
      {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "executed": executed,
        "steps": {
          label: [
            {"hash": t.hash, "name": t.name, "size": t.size, "state": t.state,
             "category": t.category, "ratio": round(t.ratio, 3), "private": t.private}
            for t in items
          ]
          for label, items in scope.items()
        },
      },
      indent=2,
    ),
    encoding="utf-8",
  )
  return path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--execute", action="store_true", help="actually remove (default: dry run)")
  parser.add_argument(
    "--config-dir",
    type=Path,
    default=Path(os.environ.get("CONFIG_DIRECTORY", REPO_ROOT / ".docker-config")),
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
    print("ERROR: cannot authenticate to qBittorrent", file=sys.stderr)
    return 2

  torrents = fetch_torrents(session)
  _, goal_ratio, goal_minutes, _ = fetch_share_goals(session)
  queued, known, degraded = fetch_arr_state(args.config_dir)
  if degraded:
    print("ERROR: *arr state incomplete; refusing to act on a partial picture:", file=sys.stderr)
    for msg in degraded:
      print(f"  {msg}", file=sys.stderr)
    return 2

  buckets = classify(torrents, queued, known, goal_ratio, goal_minutes)
  scope = proposed_for_removal(buckets)
  manifest = write_manifest(scope, args.execute)
  total = sum(t.size for items in scope.values() for t in items)
  count = sum(len(items) for items in scope.values())
  mode = "EXECUTING" if args.execute else "DRY RUN (pass --execute to act)"
  print(f"{mode}: {count} torrents, {total / GIB:.1f} GiB")
  print(f"manifest: {manifest}")
  print()

  qindex = arr_queue_index(args.config_dir)
  keys = {app: _read_api_key(args.config_dir, app) for app in ARRS}
  failures = 0
  direct: list[str] = []

  for label, items in scope.items():
    print(f"── {label}: {len(items)} torrents, {sum(t.size for t in items) / GIB:.1f} GiB")
    for tor in sorted(items, key=lambda t: -t.size):
      route = "arr-queue+blocklist" if tor.hash in qindex else "qbit-direct"
      print(f"   [{route:<19}] {tor.size / GIB:7.1f}G  {tor.name[:56]}")
      if not args.execute:
        continue
      if tor.hash in qindex:
        app, record_id = qindex[tor.hash]
        ok, detail = remove_via_arr(app, record_id, keys[app] or "")
        if not ok:
          failures += 1
          print(f"      FAILED via {app}: {detail}")
      else:
        direct.append(tor.hash)

  if args.execute and direct:
    ok, detail = remove_via_qbit(session, direct)
    print(f"\nqBittorrent direct delete of {len(direct)} torrents: {'ok' if ok else 'FAILED'} ({detail})")
    if not ok:
      failures += len(direct)

  if not args.execute:
    print("\nNothing was changed. Re-run with --execute to apply.")
    return 0
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
