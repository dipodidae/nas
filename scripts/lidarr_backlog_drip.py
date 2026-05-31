#!/usr/bin/env python3
"""Drip-feed Lidarr's missing-album backlog into Soulseek without flooding it.

Why this exists
---------------
Lidarr's built-in ``MissingAlbumSearch`` searches *every* wanted album at once.
With a large backlog (thousands of monitored-but-missing albums) that dumps
hundreds of grabs onto slskd in one shot; most peers queue them remotely and
they wedge at 0 bytes, holding slots forever (the classic Tubifarry/slskd clog).
See the qBittorrent-stalled and slskd-cleanup runbooks for the post-mortem.

This script is the controlled alternative: it only searches a small batch, and
only when slskd has spare capacity. The gate is slskd's *in-flight* download
count (anything not in a Completed state). When in-flight is below the
threshold, it searches the next ``--batch`` missing albums it hasn't touched
inside the cooldown window; otherwise it does nothing. Run it on a short cron
(e.g. every 15 min) and the backlog drains steadily and can never re-clog —
when downloads back up, the drip pauses itself.

A rolling state file records when each album was last searched so successive
runs walk through the whole backlog and only retry an album after the cooldown,
rather than hammering the same first page every time.

Exit codes
----------
  0 success (searched a batch, or intentionally idle: queue busy / nothing due)
  1 partial (the search command POST failed)
  2 fatal (config missing, Lidarr/slskd unreachable, bad response shape)

Environment
-----------
  API_KEY_LIDARR        (required) Lidarr API key
  API_KEY_SLSKD         (required) slskd API key (capacity gate)
  LIDARR_HOST           (default: http://localhost:8686)
  SLSKD_HOST            (default: http://localhost:5030)

Usage
-----
  python scripts/lidarr_backlog_drip.py                       # gated drip
  python scripts/lidarr_backlog_drip.py --dry-run             # report only
  python scripts/lidarr_backlog_drip.py --threshold 40 --batch 20
  python scripts/lidarr_backlog_drip.py --state logs/drip.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_SLSKD_HOST = "http://localhost:5030"
DEFAULT_THRESHOLD = 40
DEFAULT_BATCH = 20
DEFAULT_COOLDOWN_HOURS = 12.0
# Seconds between individual AlbumSearch dispatches. The Soulseek server bans
# the account for "too many operations / flooding / quickly repeating a search"
# when slskd fires a burst of searches at once. Pacing the batch over the cron
# window (20 searches x 20s ~= 7 min, inside the 15-min interval) keeps us well
# under that threshold while preserving throughput. See the slskd-flood-ban
# runbook. 0 disables pacing (fires the whole batch in one command — burst).
DEFAULT_SEARCH_DELAY = 20.0


def _lidarr(host: str, api_key: str, path: str, method: str = "GET", body: object = None) -> object:
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


def _slskd_downloads(host: str, api_key: str) -> object:
  req = urllib.request.Request(
    f"{host}/api/v0/transfers/downloads",
    headers={"X-API-Key": api_key},
  )
  with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
    raw = resp.read()
    return json.loads(raw) if raw else []


def count_inflight(downloads: object) -> int:
  """Count slskd download files not in a terminal (Completed) state.

  Pure over the /transfers/downloads payload so it can be unit-tested. Anything
  whose state does not start with 'Completed' is occupying a transfer slot
  (Queued, Initializing, InProgress, Requested, …).
  """
  if not isinstance(downloads, list):
    return 0
  n = 0
  for user in downloads:
    for d in user.get("directories", []):
      for f in d.get("files", []):
        state = str(f.get("state", ""))
        if not state.startswith("Completed"):
          n += 1
  return n


def select_albums(
  missing: list[dict],
  state: dict[str, float],
  *,
  cooldown_hours: float,
  batch: int,
  now: float,
) -> tuple[list[int], dict[str, float]]:
  """Pick the next batch of album ids to search and return the updated state.

  Albums searched inside the cooldown window are skipped, so repeated runs walk
  the whole backlog instead of re-firing the same first page. The returned
  state is pruned of entries older than the cooldown (they're eligible again
  and need no record). Pure for testability.
  """
  cooldown_s = cooldown_hours * 3600.0
  picked: list[int] = []
  new_state = {k: v for k, v in state.items() if now - v < cooldown_s}
  for rec in missing:
    if len(picked) >= batch:
      break
    aid = rec.get("id")
    if not isinstance(aid, int):
      continue
    last = state.get(str(aid))
    if last is not None and now - last < cooldown_s:
      continue  # still cooling down
    picked.append(aid)
    new_state[str(aid)] = now
  return picked, new_state


def load_state(path: Path | None) -> dict[str, float]:
  if path is None or not path.exists():
    return {}
  try:
    data = json.loads(path.read_text())
    return {str(k): float(v) for k, v in data.items()}
  except (json.JSONDecodeError, TypeError, ValueError, OSError):
    return {}


def save_state(path: Path | None, state: dict[str, float]) -> None:
  if path is None:
    return
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))
  except OSError as exc:
    print(f"WARNING: could not write state file {path}: {exc}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Drip-feed Lidarr's missing-album backlog into Soulseek, gated on slskd capacity."
  )
  parser.add_argument("--dry-run", action="store_true", help="Report planned actions and exit 0.")
  parser.add_argument(
    "--threshold", type=int, default=DEFAULT_THRESHOLD,
    help=f"Pause when slskd in-flight downloads >= this (default {DEFAULT_THRESHOLD}).",
  )
  parser.add_argument(
    "--batch", type=int, default=DEFAULT_BATCH,
    help=f"Albums to search per run when capacity is free (default {DEFAULT_BATCH}).",
  )
  parser.add_argument(
    "--cooldown-hours", type=float, default=DEFAULT_COOLDOWN_HOURS,
    help=f"Don't re-search the same album within this window (default {DEFAULT_COOLDOWN_HOURS}).",
  )
  parser.add_argument(
    "--search-delay", type=float, default=DEFAULT_SEARCH_DELAY,
    help=(
      f"Seconds between individual album searches (default {DEFAULT_SEARCH_DELAY}). "
      "Paces the batch so slskd doesn't burst-flood the Soulseek server and earn "
      "a 30-min flood ban. 0 = fire the whole batch in one command (legacy burst)."
    ),
  )
  parser.add_argument(
    "--state", type=Path, default=None,
    help="JSON file tracking per-album last-searched epoch across runs.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  lidarr_host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  lidarr_key = os.environ.get("API_KEY_LIDARR")
  slskd_key = os.environ.get("API_KEY_SLSKD")
  if not lidarr_key:
    print("ERROR: API_KEY_LIDARR not set", file=sys.stderr)
    return 2
  if not slskd_key:
    print("ERROR: API_KEY_SLSKD not set", file=sys.stderr)
    return 2

  # 1) Capacity gate — how busy is slskd right now?
  try:
    downloads = _slskd_downloads(slskd_host, slskd_key)
  except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
    print(f"ERROR: slskd unreachable at {slskd_host}: {exc}", file=sys.stderr)
    return 2
  inflight = count_inflight(downloads)
  if inflight >= args.threshold:
    print(f"idle: slskd in-flight {inflight} >= threshold {args.threshold} — holding off")
    return 0
  print(f"capacity ok: slskd in-flight {inflight} < threshold {args.threshold}")

  # 2) Fetch the missing backlog (one page is plenty for one batch; sorted
  #    newest-first so fresher releases — likelier to be well-seeded — go first).
  page_size = max(args.batch * 5, 100)
  try:
    resp = _lidarr(
      lidarr_host, lidarr_key,
      f"/wanted/missing?page=1&pageSize={page_size}"
      "&sortKey=albums.releaseDate&sortDirection=descending&includeArtist=false",
    )
  except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
    print(f"ERROR: Lidarr unreachable at {lidarr_host}: {exc}", file=sys.stderr)
    return 2
  if not isinstance(resp, dict) or not isinstance(resp.get("records"), list):
    print("ERROR: unexpected Lidarr /wanted/missing response shape", file=sys.stderr)
    return 2
  total = resp.get("totalRecords", 0)
  missing = resp["records"]

  state = load_state(args.state)
  now = time.time()
  ids, new_state = select_albums(
    missing, state, cooldown_hours=args.cooldown_hours, batch=args.batch, now=now,
  )
  if not ids:
    print(f"nothing due: {total} missing, but all of this page are within "
          f"the {args.cooldown_hours}h cooldown")
    return 0

  if args.dry_run:
    print(f"DRY: would search {len(ids)} album(s) (of {total} missing), "
          f"{args.search_delay}s apart: {ids}")
    return 0

  # Pace the searches: one AlbumSearch per album, spaced by --search-delay, so
  # slskd trickles them onto the Soulseek network instead of bursting the whole
  # batch at once (which earns a 30-min flood ban). delay<=0 keeps the legacy
  # single-command burst.
  delay = args.search_delay
  if delay <= 0:
    try:
      _lidarr(lidarr_host, lidarr_key, "/command", "POST",
              {"name": "AlbumSearch", "albumIds": ids})
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
      print(f"WARNING: AlbumSearch command failed: {exc}", file=sys.stderr)
      return 1
    save_state(args.state, new_state)
    print(f"searched {len(ids)} album(s) of {total} missing in one burst "
          f"(slskd in-flight {inflight})")
    return 0

  searched = 0
  failed = 0
  for i, aid in enumerate(ids):
    try:
      _lidarr(lidarr_host, lidarr_key, "/command", "POST",
              {"name": "AlbumSearch", "albumIds": [aid]})
      searched += 1
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError) as exc:
      print(f"WARNING: AlbumSearch failed for album {aid}: {exc}", file=sys.stderr)
      new_state.pop(str(aid), None)  # leave it un-stamped so it retries next run
      failed += 1
    if i < len(ids) - 1:
      time.sleep(delay)

  save_state(args.state, new_state)
  print(f"searched {searched}/{len(ids)} album(s) of {total} missing, "
        f"{delay}s apart (slskd in-flight {inflight}; {failed} failed)")
  return 1 if failed else 0


if __name__ == "__main__":
  sys.exit(main())
