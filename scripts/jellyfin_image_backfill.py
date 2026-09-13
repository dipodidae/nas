#!/usr/bin/env python3
"""Backfill missing Jellyfin artist and album images from the enabled providers.

Why a script and not just a library scan
----------------------------------------
On 2026-09-13 half the music library's artists -- 1,647 of 3,331 -- had no
primary image, while 96% of albums had one. The cause was not the disk: only
756 of 2,741 artist folders hold any image at all, because Lidarr's metadata
server has image URLs for just 956 of its 3,519 artists, and ``sacad``
(scripts/album_art.py) is an *album* cover tool that never touches artist art.
Jellyfin was the only layer that could fill the gap and it had exactly one
image provider enabled for music -- TheAudioDB -- with the Fanart, Cover Art
Archive and last.fm plugins installed, active, and unticked.

With those enabled, 1,628 of the 1,647 image-less artists carry a
``MusicBrainzArtist`` id for a provider to key off. A plain library scan will
not go and get them: Jellyfin only queries image providers for an item it is
actually refreshing, and a scan skips items whose files have not changed. So
this walks the items that are *missing an image* and refreshes those.

Shape
-----
Bounded and self-throttling, like lidarr_backlog_drip.py and album_art.py:
``--limit`` items per run, ``--delay`` seconds apart, and a state file that puts
an item on a ``--cooldown-days`` bench after an attempt so an artist no provider
has cannot be re-queried every week forever.

It reports by effect, not by status code. Jellyfin answers ``204`` to a refresh
it has merely queued, so after the batch this waits ``--settle`` seconds and
re-reads the same items to count how many actually gained an image. A run that
refreshed 200 items and gained 0 says so.

Exit codes
----------
  0  success (including a run with nothing due)
  1  partial - at least one refresh call failed
  2  fatal - no API key, or Jellyfin unreachable

Environment
-----------
  API_KEY_JELLYFIN_ARR  the arr-integrations key (NOT API_KEY_JELLYFIN, which is
                        Jellyseerr's). Sent as ``Authorization: MediaBrowser
                        Token="..."`` -- the scheme that works with
                        EnableLegacyAuthorization off (ADR-0035).
  JELLYFIN_URL          default http://localhost:8096

Usage
-----
  python scripts/jellyfin_image_backfill.py --dry-run
  python scripts/jellyfin_image_backfill.py --apply --limit 200
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://localhost:8096"
DEFAULT_STATE = "logs/jellyfin_image_backfill.json"
ITEM_TYPES = ("MusicArtist", "MusicAlbum")


def has_primary(item: dict[str, Any]) -> bool:
  """True if Jellyfin already holds a primary image for the item. Pure."""
  return bool((item.get("ImageTags") or {}).get("Primary"))


def select_targets(
  items: list[dict[str, Any]],
  attempts: dict[str, float],
  now: float,
  cooldown_days: float,
  limit: int,
) -> list[dict[str, Any]]:
  """Items missing a primary image and off cooldown, oldest attempt first. Pure.

  An item never attempted sorts before one attempted long ago, so a fresh
  import is picked up on the next run rather than queueing behind the backlog.
  """
  cooldown_s = cooldown_days * 86400

  def due_at(item: dict[str, Any]) -> float:
    """Last attempt, or -inf for one never tried so it can never be benched."""
    return attempts.get(item["Id"], float("-inf"))

  due = [item for item in items if not has_primary(item) and (now - due_at(item)) >= cooldown_s]
  due.sort(key=due_at)
  return due[:limit]


def record_attempts(attempts: dict[str, float], ids: list[str], now: float) -> dict[str, float]:
  """Stamp each id with the attempt time. Pure - returns a new mapping."""
  updated = dict(attempts)
  for item_id in ids:
    updated[item_id] = now
  return updated


def prune_attempts(attempts: dict[str, float], live_ids: set[str]) -> dict[str, float]:
  """Drop bench entries for items Jellyfin no longer has. Pure.

  Without this the state file grows forever and a deleted-then-reimported
  album would stay benched under a stale id.
  """
  return {k: v for k, v in attempts.items() if k in live_ids}


class Jellyfin:
  def __init__(self, base_url: str, api_key: str, timeout: float = 120.0) -> None:
    self.base = base_url.rstrip("/")
    self.headers = {
      "Authorization": f'MediaBrowser Token="{api_key}"',
      "Content-Type": "application/json",
    }
    self.timeout = timeout

  def _open(self, path: str, method: str = "GET") -> Any:
    req = urllib.request.Request(self.base + path, headers=self.headers, method=method)
    with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
      body = resp.read()
      return json.loads(body) if body else None

  def items(self, item_type: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode(
      {
        "Recursive": "true",
        "IncludeItemTypes": item_type,
        "Fields": "ProviderIds",
        "EnableImages": "true",
        "Limit": 100000,
      }
    )
    return (self._open(f"/Items?{query}") or {}).get("Items", [])

  def refresh(self, item_id: str) -> None:
    """Queue a refresh that searches for images the item is missing.

    ``ImageRefreshMode=FullRefresh`` with ``ReplaceAllImages=false`` is the
    "search for missing images" combination -- it queries remote providers but
    never overwrites an image the item already has.
    """
    query = urllib.parse.urlencode(
      {
        "metadataRefreshMode": "Default",
        "imageRefreshMode": "FullRefresh",
        "replaceAllMetadata": "false",
        "replaceAllImages": "false",
      }
    )
    self._open(f"/Items/{item_id}/Refresh?{query}", method="POST")


def load_state(path: Path) -> dict[str, float]:
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return {}
  attempts = data.get("attempts")
  return attempts if isinstance(attempts, dict) else {}


def save_state(path: Path, attempts: dict[str, float]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = {"attempts": attempts, "written": time.time()}
  tmp = path.with_suffix(path.suffix + ".tmp")
  tmp.write_text(json.dumps(payload), encoding="utf-8")
  tmp.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--url", default=os.getenv("JELLYFIN_URL", DEFAULT_URL))
  ap.add_argument("--apply", action="store_true", help="Actually queue refreshes")
  ap.add_argument(
    "--dry-run", action="store_true", help="Report what would be refreshed (the default)"
  )
  ap.add_argument("--limit", type=int, default=200, help="Max items to refresh per run")
  ap.add_argument("--delay", type=float, default=1.5, help="Seconds between refresh calls")
  ap.add_argument(
    "--cooldown-days",
    type=float,
    default=30.0,
    help="Do not retry an item that was attempted within this many days",
  )
  ap.add_argument(
    "--settle",
    type=float,
    default=90.0,
    help="Seconds to wait before re-reading the batch to measure what it actually gained",
  )
  ap.add_argument("--state", default=DEFAULT_STATE)
  return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  api_key = os.getenv("API_KEY_JELLYFIN_ARR")
  if not api_key:
    print("API_KEY_JELLYFIN_ARR is not set", file=sys.stderr)
    return 2

  jf = Jellyfin(args.url, api_key)
  now = time.time()
  state_path = Path(args.state)
  attempts = load_state(state_path)

  everything: list[dict[str, Any]] = []
  try:
    for item_type in ITEM_TYPES:
      everything.extend(jf.items(item_type))
  except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
    print(f"Jellyfin unreachable at {args.url}: {exc}", file=sys.stderr)
    return 2

  attempts = prune_attempts(attempts, {item["Id"] for item in everything})
  gaps = [item for item in everything if not has_primary(item)]
  targets = select_targets(everything, attempts, now, args.cooldown_days, args.limit)

  print(
    f"{len(everything)} music items, {len(gaps)} missing a primary image, "
    f"{len(targets)} due this run (limit {args.limit}, cooldown {args.cooldown_days:g}d)"
  )
  if not args.apply:
    for item in targets[:10]:
      print(f"  would refresh {item.get('Type')}: {item.get('Name')}")
    if len(targets) > 10:
      print(f"  ... and {len(targets) - 10} more")
    print("DRY-RUN: nothing queued. Pass --apply.")
    return 0
  if not targets:
    save_state(state_path, attempts)
    return 0

  failed = 0
  for item in targets:
    try:
      jf.refresh(item["Id"])
    except (urllib.error.URLError, OSError) as exc:
      failed += 1
      print(f"  refresh failed for {item.get('Name')!r}: {exc}", file=sys.stderr)
    if args.delay:
      time.sleep(args.delay)

  attempts = record_attempts(attempts, [item["Id"] for item in targets], now)
  save_state(state_path, attempts)

  # A 204 means "queued", so measure the effect instead of trusting it.
  if args.settle:
    time.sleep(args.settle)
  gained = 0
  try:
    after = {item["Id"]: item for item in (jf.items("MusicArtist") + jf.items("MusicAlbum"))}
    gained = sum(1 for item in targets if has_primary(after.get(item["Id"], {})))
  except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
    print(f"  could not re-read to verify: {exc}", file=sys.stderr)

  print(
    f"refreshed {len(targets) - failed}/{len(targets)} items; "
    f"{gained} now have an image ({len(gaps) - gained} gaps left)"
  )
  return 1 if failed else 0


if __name__ == "__main__":
  sys.exit(main())
