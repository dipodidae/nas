#!/usr/bin/env python3
"""Make Lidarr's imports actually reach Jellyfin, by fixing the path Lidarr sends.

Why this exists
---------------
Lidarr's built-in "Emby / Jellyfin" connection with *Update Library* enabled
does fire on import: verified 2026-09-01 with Jellyfin request logging on, a
`POST http://jellyfin:8096/mediabrowser/Library/Media/Updated` arrives and
returns 204. It is nonetheless a **silent no-op**, and the 204 is what hides it.

The reason is a path-namespace mismatch. Lidarr's media root is `/music`;
Jellyfin's Music library lives at `/data/movies/music` (the `${SHARE_DIRECTORY}`
mount). `Library/Media/Updated` hands each reported path to Jellyfin's
LibraryMonitor, which resolves it to the library that contains it — and
`/music/...` is under no Jellyfin library, so it is dropped without a word.
Proven by A/B against the live server:

    POST {"Updates":[{"Path":"/music/Bathory/1988 - Blood Fire Death"}]}
      -> 204, no LibraryMonitor line, Jellyfin keeps the stale metadata
    POST {"Updates":[{"Path":"/data/movies/music/Bathory/1988 - Blood Fire Death"}]}
      -> 204, 'LibraryMonitor: "Blood Fire Death" ... will be refreshed', updated

Sonarr and Radarr have `mapFrom`/`mapTo` fields on their MediaBrowser
connection for exactly this, and they are now set. **Lidarr's does not expose
those fields at all** (confirmed against `/api/v1/notification` — its field list
is host/port/useSsl/urlBase/apiKey/notify/updateLibrary and nothing else), so
there is no in-Lidarr fix. This script is the missing mapping, applied outside
Lidarr: poll Lidarr's history for file-level events, translate the paths, and
report them to Jellyfin itself.

Targeted on purpose: it reports the *album folder* that changed, so Jellyfin
refreshes that folder rather than re-walking the whole Music library. A full
Music scan is the box's largest memory event (~1.56GB anon-RSS) and is not
something to run every five minutes — see docs/jellyfin-playback-audit.md.

Exit codes
----------
  0  nothing to do, or every path reported successfully
  1  partial — some paths reported, at least one call failed
  2  fatal (missing API key, Lidarr or Jellyfin unreachable)

Environment
-----------
  API_KEY_LIDARR       (required)
  API_KEY_JELLYFIN     (required)
  LIDARR_HOST          (default: http://localhost:8686)
  JELLYFIN_HOST        (default: http://localhost:8096)

Usage
-----
  python scripts/lidarr_jellyfin_bridge.py --state logs/lidarr_jellyfin_bridge.json
  python scripts/lidarr_jellyfin_bridge.py --dry-run --since-min 120
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_JELLYFIN_HOST = "http://localhost:8096"
DEFAULT_STATE = Path(__file__).resolve().parent.parent / "logs" / "lidarr_jellyfin_bridge.json"
DEFAULT_MAP_FROM = "/music"
DEFAULT_MAP_TO = "/data/movies/music"
# Lidarr history events that mean "a file under /music changed on disk".
# trackFileDeleted is included so a removed album stops haunting Jellyfin.
FILE_EVENTS = {
  "trackFileImported",
  "trackFileRenamed",
  "trackFileRetagged",
  "trackFileDeleted",
}
PATH_FIELDS = ("importedPath", "path", "sourcePath")
# This box does hundreds of Lidarr imports a day and one album alone produces
# ~25 history records, so a single page is NOT reliably enough between runs
# during a backlog drain — fetch_history pages back until it passes the cursor.
# MAX_HISTORY_PAGES bounds the work if the cursor is very old (e.g. the state
# file was lost); anything older than that is left to the weekly library scan.
HISTORY_PAGE_SIZE = 200
MAX_HISTORY_PAGES = 10
DEFAULT_FIRST_RUN_MINUTES = 30


def _get_json(url: str, headers: dict[str, str], timeout: int = 60):
  req = urllib.request.Request(url, headers=headers)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def fetch_history(host: str, api_key: str, since_iso: str) -> list[dict] | None:
  """History records newer than `since_iso`, newest first.

  Pages backwards until it reaches the cursor rather than trusting one page:
  during an import burst a single 200-record page covers well under five
  minutes, and a missed record is a silently unrefreshed album. None if Lidarr
  is unreachable.
  """
  collected: list[dict] = []
  for page in range(1, MAX_HISTORY_PAGES + 1):
    url = (
      f"{host.rstrip('/')}/api/v1/history"
      f"?page={page}&pageSize={HISTORY_PAGE_SIZE}&sortKey=date&sortDirection=descending"
    )
    body = _get_json(url, {"X-Api-Key": api_key})
    if not isinstance(body, dict):
      return None if page == 1 else collected
    records = body.get("records")
    if not isinstance(records, list) or not records:
      return collected
    collected.extend(records)
    if any(str(r.get("date", "")) <= since_iso for r in records):
      return collected
  print(
    f"WARNING: cursor {since_iso} is older than {MAX_HISTORY_PAGES} history pages; "
    "older changes are left to the scheduled library scan",
    file=sys.stderr,
  )
  return collected


def changed_folders(records: list[dict], since_iso: str) -> tuple[list[str], str]:
  """Album folders touched after `since_iso`, plus the new cursor.

  Returns Lidarr-side paths, de-duplicated and ordered oldest-first so a
  Jellyfin refresh sees them in the order they actually happened.
  """
  cursor = since_iso
  folders: list[str] = []
  seen: set[str] = set()
  for record in reversed(records):  # oldest first
    date = str(record.get("date", ""))
    if date <= since_iso:
      continue
    cursor = max(cursor, date)
    if record.get("eventType") not in FILE_EVENTS:
      continue
    data = record.get("data") or {}
    for field in PATH_FIELDS:
      value = data.get(field)
      if not value:
        continue
      folder = str(Path(str(value)).parent)
      if folder not in seen:
        seen.add(folder)
        folders.append(folder)
  return folders, cursor


def translate(folders: list[str], map_from: str, map_to: str) -> list[str]:
  """Rewrite Lidarr's media root to Jellyfin's. Paths outside it are dropped.

  Dropping rather than passing through is deliberate: an untranslated path is
  exactly the silent no-op this script exists to fix, so it should be visible
  as a missing entry rather than a 204 that means nothing.
  """
  prefix = map_from.rstrip("/")
  target = map_to.rstrip("/")
  return [
    target + folder[len(prefix) :]
    for folder in folders
    if folder == prefix or folder.startswith(prefix + "/")
  ]


def report_to_jellyfin(host: str, api_key: str, paths: list[str]) -> bool:
  """POST one Library/Media/Updated batch. True if Jellyfin accepted it."""
  payload = {"Updates": [{"Path": p, "UpdateType": "Modified"} for p in paths]}
  req = urllib.request.Request(
    f"{host.rstrip('/')}/Library/Media/Updated",
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
      "Content-Type": "application/json",
      "Authorization": f'MediaBrowser Token="{api_key}"',
    },
  )
  try:
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - localhost
      return resp.status in (200, 202, 204)
  except (OSError, urllib.error.HTTPError):
    return False


def load_cursor(state_path: Path | None, first_run_minutes: int) -> str:
  """Last processed history timestamp, or a short lookback on first run."""
  if state_path is not None and state_path.exists():
    try:
      value = json.loads(state_path.read_text()).get("cursor")
      if isinstance(value, str) and value:
        return value
    except (OSError, json.JSONDecodeError):
      pass
  start = datetime.now(UTC) - timedelta(minutes=first_run_minutes)
  return start.strftime("%Y-%m-%dT%H:%M:%SZ")


def save_cursor(state_path: Path | None, cursor: str) -> None:
  if state_path is None:
    return
  try:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"cursor": cursor}))
  except OSError as exc:
    print(f"WARNING: could not write state file {state_path}: {exc}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Report Lidarr's file changes to Jellyfin with the paths translated.",
  )
  parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Cursor state file.")
  parser.add_argument("--map-from", default=DEFAULT_MAP_FROM, help="Lidarr's media root.")
  parser.add_argument("--map-to", default=DEFAULT_MAP_TO, help="Jellyfin's path for the same files.")
  parser.add_argument(
    "--since-min",
    type=int,
    default=DEFAULT_FIRST_RUN_MINUTES,
    help=f"Lookback when there is no state file (default {DEFAULT_FIRST_RUN_MINUTES} min).",
  )
  parser.add_argument("--dry-run", action="store_true", help="Print paths, tell Jellyfin nothing.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  lidarr_key = os.getenv("API_KEY_LIDARR")
  jellyfin_key = os.getenv("API_KEY_JELLYFIN")
  if not lidarr_key or not jellyfin_key:
    print("ERROR: API_KEY_LIDARR and API_KEY_JELLYFIN must both be set", file=sys.stderr)
    return 2

  cursor = load_cursor(args.state, args.since_min)
  records = fetch_history(os.getenv("LIDARR_HOST", DEFAULT_LIDARR_HOST), lidarr_key, cursor)
  if records is None:
    print("ERROR: could not read Lidarr history", file=sys.stderr)
    return 2

  folders, new_cursor = changed_folders(records, cursor)
  paths = translate(folders, args.map_from, args.map_to)
  skipped = len(folders) - len(paths)
  if skipped:
    print(f"WARNING: {skipped} folder(s) outside {args.map_from!r}, not reported", file=sys.stderr)

  if not paths:
    print(f"nothing to report (cursor {cursor} -> {new_cursor})")
    if not args.dry_run:
      save_cursor(args.state, new_cursor)
    return 0

  for path in paths:
    print(f"changed: {path}")
  if args.dry_run:
    print(f"DRY-RUN would report {len(paths)} folder(s) to Jellyfin")
    return 0

  if not report_to_jellyfin(os.getenv("JELLYFIN_HOST", DEFAULT_JELLYFIN_HOST), jellyfin_key, paths):
    print("ERROR: Jellyfin rejected the update; cursor not advanced", file=sys.stderr)
    return 2

  save_cursor(args.state, new_cursor)
  print(f"reported {len(paths)} folder(s) to Jellyfin (cursor -> {new_cursor})")
  return 1 if skipped else 0


if __name__ == "__main__":
  sys.exit(main())
