#!/usr/bin/env python3
"""Purge Lidarr artists with no track files + flush the entire slskd download queue.

Why
---
Lidarr accumulates "wanted" artists with 0 track files on disk. Each of those
artists keeps Lidarr searching slskd, which in turn fills slskd's transfer
manager with `Completed, Errored` rows (search misses, peer timeouts, etc.).
Over time the slskd transfer manager gets so clogged that even artists that
*could* be downloaded never make progress.

This script clears both problems in one pass:

1. Lidarr: DELETE every artist whose ``statistics.trackFileCount`` is 0.
   ``deleteFiles=false`` (nothing to delete — there are no files) and
   ``addImportListExclusion=false`` (so they can be re-added cleanly later).
2. slskd: DELETE every transfer in the downloads queue, regardless of state.
   For active states (Queued/InProgress/...) we DELETE once to cancel, then
   DELETE again with ``?remove=true`` to evict the record. Completed states
   only need the ``?remove=true`` call.

Exit codes
----------
  0 success
  1 partial (some deletes failed; details on stderr)
  2 fatal (config missing, host unreachable)

Environment
-----------
  API_KEY_LIDARR   (required) Lidarr API key
  API_KEY_SLSKD    (required) slskd admin key
  LIDARR_HOST      (default: http://localhost:8686)
  SLSKD_HOST       (default: http://localhost:5030)

Usage
-----
  python scripts/lidarr_purge_empty_artists.py            # do it
  python scripts/lidarr_purge_empty_artists.py --dry-run  # report only
  python scripts/lidarr_purge_empty_artists.py --keep-slskd
  python scripts/lidarr_purge_empty_artists.py --keep-artists
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_SLSKD_HOST = "http://localhost:5030"
COMPLETED_PREFIX = "Completed"


def _request(
  method: str,
  url: str,
  api_key: str,
  *,
  header: str,
  timeout: int = 30,
) -> tuple[int, bytes]:
  req = urllib.request.Request(url, method=method, headers={header: api_key})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return resp.status, resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read()


# ---------------------------------------------------------------------------
# Lidarr
# ---------------------------------------------------------------------------


def fetch_artists(host: str, api_key: str) -> list[dict]:
  status, body = _request(
    "GET", f"{host}/api/v1/artist", api_key, header="X-Api-Key"
  )
  if status >= 400:
    raise RuntimeError(f"GET /api/v1/artist returned HTTP {status}")
  return json.loads(body)


def split_artists(artists: list[dict]) -> tuple[list[dict], list[dict]]:
  """Return (keep, purge). Keep any artist with at least one track file."""
  keep: list[dict] = []
  purge: list[dict] = []
  for a in artists:
    stats = a.get("statistics") or {}
    if int(stats.get("trackFileCount") or 0) > 0:
      keep.append(a)
    else:
      purge.append(a)
  return keep, purge


def delete_artist(host: str, api_key: str, artist_id: int) -> bool:
  url = (
    f"{host}/api/v1/artist/{artist_id}"
    "?deleteFiles=false&addImportListExclusion=false"
  )
  status, _ = _request("DELETE", url, api_key, header="X-Api-Key")
  return 200 <= status < 300


# ---------------------------------------------------------------------------
# slskd
# ---------------------------------------------------------------------------


def fetch_slskd_downloads(host: str, api_key: str) -> list[dict]:
  status, body = _request(
    "GET", f"{host}/api/v0/transfers/downloads", api_key, header="X-API-Key"
  )
  if status >= 400:
    raise RuntimeError(f"GET /api/v0/transfers/downloads returned HTTP {status}")
  return json.loads(body)


def flatten_transfers(downloads: list[dict]) -> list[tuple[str, str, str]]:
  """Return list of (username, transfer_id, state) for every file in the queue."""
  out: list[tuple[str, str, str]] = []
  for user in downloads:
    username = user.get("username", "")
    for directory in user.get("directories", []):
      for f in directory.get("files", []):
        tid = f.get("id", "")
        state = f.get("state", "")
        if username and tid:
          out.append((username, tid, state))
  return out


def slskd_delete(
  host: str, api_key: str, username: str, transfer_id: str, *, remove: bool
) -> int:
  user = urllib.parse.quote(username, safe="")
  url = f"{host}/api/v0/transfers/downloads/{user}/{transfer_id}"
  if remove:
    url += "?remove=true"
  status, _ = _request("DELETE", url, api_key, header="X-API-Key")
  return status


def purge_transfer(
  host: str, api_key: str, username: str, transfer_id: str, state: str
) -> bool:
  """Cancel-if-active then remove. Returns True iff the final remove succeeded."""
  if not state.startswith(COMPLETED_PREFIX):
    # Cancel first; ignore the status — if it 404s we'll find out on remove.
    slskd_delete(host, api_key, username, transfer_id, remove=False)
  status = slskd_delete(host, api_key, username, transfer_id, remove=True)
  return 200 <= status < 300 or status == 404


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  p = argparse.ArgumentParser(
    description=(
      "Delete every Lidarr artist with 0 track files and flush the slskd "
      "download queue."
    )
  )
  p.add_argument("--dry-run", action="store_true", help="Report only; change nothing.")
  p.add_argument(
    "--keep-slskd",
    action="store_true",
    help="Only purge Lidarr artists; leave the slskd queue alone.",
  )
  p.add_argument(
    "--keep-artists",
    action="store_true",
    help="Only flush slskd; leave Lidarr artists alone.",
  )
  return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)

  lidarr_host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  lidarr_key = os.environ.get("API_KEY_LIDARR")
  slskd_key = os.environ.get("API_KEY_SLSKD")

  if not args.keep_artists and not lidarr_key:
    print("ERROR: API_KEY_LIDARR not set (check .env)", file=sys.stderr)
    return 2
  if not args.keep_slskd and not slskd_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2

  artist_failed = 0
  artist_deleted = 0
  slskd_failed = 0
  slskd_deleted = 0

  # ---- Lidarr ----------------------------------------------------------------
  if not args.keep_artists:
    try:
      artists = fetch_artists(lidarr_host, lidarr_key)
    except (urllib.error.URLError, RuntimeError) as exc:
      print(f"ERROR: cannot reach Lidarr: {exc}", file=sys.stderr)
      return 2

    keep, purge = split_artists(artists)
    print(
      f"Lidarr: {len(artists)} artists total — keeping {len(keep)} with files, "
      f"purging {len(purge)} empty"
    )
    for a in purge[:5]:
      print(f"  - purge: [{a['id']}] {a['artistName']}")
    if len(purge) > 5:
      print(f"  ... and {len(purge) - 5} more")
    for a in keep:
      tfc = (a.get("statistics") or {}).get("trackFileCount", "?")
      print(f"  + keep:  [{a['id']}] {a['artistName']} ({tfc} files)")

    if not args.dry_run:
      for a in purge:
        if delete_artist(lidarr_host, lidarr_key, a["id"]):
          artist_deleted += 1
        else:
          artist_failed += 1
          print(
            f"WARNING: failed to delete artist [{a['id']}] {a['artistName']}",
            file=sys.stderr,
          )
      print(f"Lidarr: deleted {artist_deleted}/{len(purge)} artists")

  # ---- slskd -----------------------------------------------------------------
  if not args.keep_slskd:
    try:
      downloads = fetch_slskd_downloads(slskd_host, slskd_key)
    except (urllib.error.URLError, RuntimeError) as exc:
      print(f"ERROR: cannot reach slskd: {exc}", file=sys.stderr)
      return 2

    transfers = flatten_transfers(downloads)
    states: dict[str, int] = {}
    for _, _, s in transfers:
      states[s] = states.get(s, 0) + 1
    print(f"slskd: {len(transfers)} transfer rows across {len(downloads)} peers")
    for s, c in sorted(states.items(), key=lambda x: -x[1]):
      print(f"  {c:5d}  {s}")

    if not args.dry_run:
      for username, tid, state in transfers:
        if purge_transfer(slskd_host, slskd_key, username, tid, state):
          slskd_deleted += 1
        else:
          slskd_failed += 1
      print(
        f"slskd: removed {slskd_deleted}/{len(transfers)} transfers "
        f"({slskd_failed} failed)"
      )

  if args.dry_run:
    print("dry-run: no changes made")
    return 0

  if artist_failed or slskd_failed:
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
