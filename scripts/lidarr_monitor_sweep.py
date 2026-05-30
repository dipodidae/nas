#!/usr/bin/env python3
"""Re-monitor + search Lidarr artists that landed with nothing monitored.

Why this exists
---------------
Bulk-adding artists/albums (e.g. via the lidarr-bulk webapp) routinely leaves
artists in a dead state: the artist record is monitored, but *zero* of its
albums are — so Lidarr never searches for anything and "nothing happens". Two
Lidarr behaviours cause it: it intermittently ignores ``addOptions.monitor`` on
add, and the ``RefreshArtist`` it enqueues runs AlbumMonitoredService a beat
later and unmonitors the albums a just-added monitor call had set (the clobber).

This sweep is the self-healing backstop: it finds **monitored artists that have
albums but none monitored**, monitors the whole discography, and kicks an
artist search. It is deliberately conservative — it never touches an
*unmonitored* artist (that's a deliberate "I don't want this" signal), and it
skips artists that already have at least one monitored album (album-tab adds
where the user wanted just one). An artist it fixes ends up with monitored
albums, so subsequent runs skip it — the sweep is self-limiting and a no-op in
steady state.

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some monitor/search calls failed)
  2 fatal (config missing, Lidarr unreachable)

Environment
-----------
  API_KEY_LIDARR        (required) Lidarr API key
  LIDARR_HOST           (default: http://localhost:8686)

Usage
-----
  python scripts/lidarr_monitor_sweep.py              # fix + search
  python scripts/lidarr_monitor_sweep.py --dry-run    # report only
  python scripts/lidarr_monitor_sweep.py --no-search  # monitor only
  python scripts/lidarr_monitor_sweep.py --limit 25   # cap artists per run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_LIDARR_HOST = "http://localhost:8686"


@dataclass(frozen=True)
class BrokenArtist:
  artist_id: int
  name: str
  album_ids: list[int]


def _request(host: str, api_key: str, path: str, method: str = "GET", body: object = None) -> object:
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request(
    f"{host}/api/v1{path}",
    data=data,
    method=method,
    headers={"X-Api-Key": api_key, "Content-Type": "application/json"},
  )
  with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - localhost
    raw = resp.read()
    return json.loads(raw) if raw else None


def find_broken_artists(
  artists: list[dict], albums: list[dict]
) -> list[BrokenArtist]:
  """Monitored artists that own albums but have zero monitored albums.

  Pure function over the two API payloads so it can be unit-tested without a
  live Lidarr.
  """
  by_artist: dict[int, list[dict]] = defaultdict(list)
  for alb in albums:
    aid = alb.get("artistId")
    if isinstance(aid, int):
      by_artist[aid].append(alb)

  broken: list[BrokenArtist] = []
  for art in artists:
    if not art.get("monitored"):
      continue  # unmonitored artist = deliberate; leave alone
    aid = art.get("id")
    arts_albums = by_artist.get(aid, [])
    if not arts_albums:
      continue  # no discography (still refreshing, or genuinely none)
    if any(a.get("monitored") for a in arts_albums):
      continue  # already has monitored albums — not broken
    broken.append(
      BrokenArtist(
        artist_id=aid,
        name=art.get("artistName", "?"),
        album_ids=[a["id"] for a in arts_albums if a.get("id")],
      )
    )
  return broken


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Re-monitor + search Lidarr artists left with zero monitored albums."
  )
  parser.add_argument("--dry-run", action="store_true", help="Report only.")
  parser.add_argument(
    "--no-search", action="store_true", help="Monitor albums but do not trigger a search."
  )
  parser.add_argument(
    "--limit", type=int, default=0, help="Cap artists fixed per run (0 = unlimited)."
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  api_key = os.environ.get("API_KEY_LIDARR")
  if not api_key:
    print("ERROR: API_KEY_LIDARR not set", file=sys.stderr)
    return 2

  try:
    artists = _request(host, api_key, "/artist")
    albums = _request(host, api_key, "/album")
  except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
    print(f"ERROR: Lidarr unreachable at {host}: {exc}", file=sys.stderr)
    return 2
  if not isinstance(artists, list) or not isinstance(albums, list):
    print("ERROR: unexpected Lidarr response shape", file=sys.stderr)
    return 2

  broken = find_broken_artists(artists, albums)
  print(f"monitored artists with zero monitored albums: {len(broken)}")
  if not broken:
    return 0

  if args.limit and len(broken) > args.limit:
    print(f"limiting to first {args.limit} of {len(broken)}")
    broken = broken[: args.limit]

  if args.dry_run:
    for b in broken[:20]:
      print(f"  DRY {b.name} — would monitor {len(b.album_ids)} albums"
            f"{'' if args.no_search else ' + search'}")
    if len(broken) > 20:
      print(f"  ... and {len(broken) - 20} more")
    return 0

  failures = 0
  fixed_albums = 0
  for b in broken:
    try:
      _request(host, api_key, "/artist/editor", "PUT",
               {"artistIds": [b.artist_id], "monitored": True})
      if b.album_ids:
        _request(host, api_key, "/album/monitor", "PUT",
                 {"albumIds": b.album_ids, "monitored": True})
      if not args.no_search:
        _request(host, api_key, "/command", "POST",
                 {"name": "ArtistSearch", "artistId": b.artist_id})
      fixed_albums += len(b.album_ids)
      print(f"  fixed {b.name} ({len(b.album_ids)} albums"
            f"{'' if args.no_search else ' + search'})")
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
      print(f"  WARNING: failed for {b.name}: {exc}", file=sys.stderr)
      failures += 1

  print(f"done: fixed {len(broken) - failures}/{len(broken)} artists, {fixed_albums} albums monitored")
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
