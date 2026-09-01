#!/usr/bin/env python3
"""Scan a single Jellyfin library (or all of them) via the API.

Why this exists
---------------
Jellyfin's built-in "Scan Media Library" (`RefreshLibrary`) scheduled task is a
single *global* job — there is no way to schedule TV Shows weekly while keeping
Music daily. The library scan is also the largest remaining memory event on this
host (~1.56GB peak with `MALLOC_ARENA_MAX=2` + `DOTNET_EnableWriteXorExecute=0`
in place; see `docs/jellyfin-playback-audit.md`), so scanning three libraries
when only one has new content is real, avoidable cost.

This script drives the *per-library* refresh endpoint instead, so cron can give
each library its own cadence and the global task can be disabled:

    POST /Items/{virtualFolderItemId}/Refresh
        ?metadataRefreshMode=Default&imageRefreshMode=Default
        &replaceAllMetadata=false&replaceAllImages=false&recursive=true

That is the same call Jellyfin's own web UI makes for the per-library "Scan
library" button: `Default` mode fetches metadata for *new* items only and never
re-fetches metadata that already exists, so it is safe to run on a schedule.

The endpoint returns 204 immediately and the scan then runs in Jellyfin's
internal refresh queue, so a successful exit means **accepted, not finished**.
There is deliberately no `--wait`: verified on 10.11.11, a per-item refresh does
*not* drive the `RefreshLibrary` scheduled task's state (it stays `Idle`
throughout) and `BaseItemDto` exposes no refresh-progress field, so there is no
honest REST signal to poll. Watch progress in the Jellyfin dashboard, or in
`logs/jellyfin-mem.log` (the scan is visible as an `anon` bump).

Exit codes
----------
  0  every requested library was accepted
  1  partial — at least one library was accepted and at least one failed
  2  fatal (no API key, Jellyfin unreachable, no library matched)

Environment
-----------
  API_KEY_JELLYFIN     (required) Jellyfin API key
  JELLYFIN_HOST        (default: http://localhost:8096)

Usage
-----
  python scripts/jellyfin_library_scan.py --library "TV Shows"
  python scripts/jellyfin_library_scan.py --library Movies --library Music
  python scripts/jellyfin_library_scan.py --all
  python scripts/jellyfin_library_scan.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

if "API_KEY_JELLYFIN" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_JELLYFIN_HOST = "http://localhost:8096"


@dataclass(frozen=True)
class Library:
  """One Jellyfin virtual folder (what the UI calls a library)."""

  name: str
  item_id: str
  collection_type: str
  locations: tuple[str, ...]


def _request(host: str, api_key: str, path: str, method: str = "GET", timeout: int = 60):
  """Call the Jellyfin API. Returns (status, parsed-body-or-None)."""
  url = f"{host.rstrip('/')}/{path.lstrip('/')}"
  # Token goes in a header, never the query string — SWAG's access log and the
  # shell history would both retain it as a URL parameter.
  req = urllib.request.Request(
    url,
    method=method,
    headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
  )
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      body = resp.read().decode("utf-8", "replace")
      return resp.status, (json.loads(body) if body.strip() else None)
  except urllib.error.HTTPError as exc:
    return exc.code, None
  except (OSError, json.JSONDecodeError):
    return None, None


def fetch_libraries(host: str, api_key: str) -> list[Library] | None:
  """List Jellyfin's virtual folders. Returns None if unreachable."""
  status, body = _request(host, api_key, "Library/VirtualFolders")
  if status != 200 or not isinstance(body, list):
    return None
  return [
    Library(
      name=str(v.get("Name", "")),
      item_id=str(v.get("ItemId", "")),
      collection_type=str(v.get("CollectionType", "")),
      locations=tuple(v.get("Locations") or ()),
    )
    for v in body
  ]


def select_libraries(libraries: list[Library], wanted: list[str]) -> tuple[list[Library], list[str]]:
  """Match requested names against the library list, case-insensitively.

  Returns (matched, unmatched-names). Order follows the request, not the server.
  """
  by_name = {lib.name.casefold(): lib for lib in libraries}
  matched: list[Library] = []
  missing: list[str] = []
  for name in wanted:
    lib = by_name.get(name.casefold())
    if lib is None:
      missing.append(name)
    elif lib not in matched:
      matched.append(lib)
  return matched, missing


def scan_library(host: str, api_key: str, library: Library) -> bool:
  """Queue a recursive metadata refresh for one library. True if accepted."""
  query = urllib.parse.urlencode(
    {
      "metadataRefreshMode": "Default",
      "imageRefreshMode": "Default",
      "replaceAllMetadata": "false",
      "replaceAllImages": "false",
      "recursive": "true",
    }
  )
  status, _ = _request(host, api_key, f"Items/{library.item_id}/Refresh?{query}", method="POST")
  return status in (200, 202, 204)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Trigger a per-library Jellyfin scan (replaces the global scheduled task).",
  )
  parser.add_argument(
    "--library",
    action="append",
    default=[],
    metavar="NAME",
    help="Library name as shown in Jellyfin (repeatable). Case-insensitive.",
  )
  parser.add_argument("--all", action="store_true", help="Scan every library.")
  parser.add_argument("--list", action="store_true", help="List libraries and exit.")
  parser.add_argument(
    "--dry-run",
    action="store_true",
    help="Show what would be scanned without calling the refresh endpoint.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  api_key = os.getenv("API_KEY_JELLYFIN")
  if not api_key:
    print("ERROR: API_KEY_JELLYFIN not set (expected in .env)", file=sys.stderr)
    return 2

  host = os.getenv("JELLYFIN_HOST", DEFAULT_JELLYFIN_HOST)
  libraries = fetch_libraries(host, api_key)
  if libraries is None:
    print(f"ERROR: could not list libraries from {host}", file=sys.stderr)
    return 2

  if args.list:
    for lib in libraries:
      print(f"{lib.name}\t{lib.item_id}\t{lib.collection_type}\t{','.join(lib.locations)}")
    return 0

  if args.all:
    targets, missing = libraries, []
  elif args.library:
    targets, missing = select_libraries(libraries, args.library)
  else:
    print("ERROR: pass --library NAME (repeatable), --all, or --list", file=sys.stderr)
    return 2

  for name in missing:
    known = ", ".join(lib.name for lib in libraries)
    print(f"ERROR: no library named {name!r} (have: {known})", file=sys.stderr)
  if not targets:
    return 2

  failed = 0
  for lib in targets:
    if args.dry_run:
      print(f"DRY-RUN would scan {lib.name!r} ({lib.item_id})")
      continue
    if scan_library(host, api_key, lib):
      print(f"queued scan for {lib.name!r} ({lib.item_id})")
    else:
      print(f"ERROR: refresh call failed for {lib.name!r}", file=sys.stderr)
      failed += 1

  if args.dry_run:
    return 1 if missing else 0

  if failed == len(targets):
    return 2
  return 1 if (failed or missing) else 0


if __name__ == "__main__":
  sys.exit(main())
