#!/usr/bin/env python3
"""Drop Lidarr queue items wedged in `completed / importFailed`.

Background
----------
Lidarr's queue accumulates rows where slskd finished the download but Lidarr
refuses to import the result — typically with messages like
``Album release not requested`` (peer sent a different MusicBrainz release than
Lidarr asked for) or ``Album match is not close enough: X% vs 80%``. These
sit in the queue forever:

- There is no Lidarr setting equivalent to "remove failed import after N hours";
  ``autoRedownloadFailed`` only fires for download-side failures.
- While they remain in the queue, Tubifarry never calls back to slskd to clear
  the transfer, so ``slskd_cleanup.py`` correctly refuses to touch them (active
  queue gate) and the slskd connection pool gums up.

Net effect: searches slow down, new grabs stall, throughput collapses — the
classic "clog" downstream of the slskd/Tubifarry interaction.

What this script does
---------------------
1. GET `/api/v1/queue?pageSize=200`
2. For every record whose ``trackedDownloadState == 'importFailed'`` and whose
   ``added`` timestamp is older than ``--min-age-hours`` (default 1):
   ``DELETE /api/v1/queue/{id}?removeFromClient=true&blocklist=true&skipRedownload=false``
   That removes the row, kills the slskd transfer record (Tubifarry), blocklists
   the specific release so it isn't re-grabbed, and asks Lidarr to search for a
   different release of the same album.

Safety rails
------------
- **Age gate**: only delete records older than ``--min-age-hours``. Records with
  no ``added`` timestamp are skipped conservatively.
- **State gate**: only ``importFailed`` rows are touched. Downloading / importing
  rows are left alone.
- ``--dry-run`` reports the plan and exits 0.

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some deletes failed; details on stderr)
  2 fatal (config missing, Lidarr unreachable, HTTP error)

Environment
-----------
  API_KEY_LIDARR (required) Lidarr API key
  LIDARR_HOST    (default: http://localhost:8686)

Usage
-----
  python scripts/lidarr_queue_unstick.py               # clean now
  python scripts/lidarr_queue_unstick.py --dry-run     # report only
  python scripts/lidarr_queue_unstick.py --min-age-hours 0   # clear all immediately
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_HOST = "http://localhost:8686"
DEFAULT_MIN_AGE_HOURS = 1.0
FAILED_STATE = "importFailed"


@dataclass(frozen=True)
class WedgedItem:
  queue_id: int
  title: str
  status: str
  tracked_state: str
  added: _dt.datetime | None


def _request(
  method: str,
  url: str,
  api_key: str,
  *,
  timeout: int = 15,
) -> tuple[int, bytes]:
  req = urllib.request.Request(url, method=method, headers={"X-Api-Key": api_key})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return resp.status, resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read()


def _parse_iso(value: str | None) -> _dt.datetime | None:
  """Parse Lidarr's ISO-8601 timestamps. Returns naive UTC for comparison.

  Lidarr emits ``2026-05-25T11:58:44Z`` (with trailing Z). Strip the Z and
  treat the result as naive UTC so it can be compared against
  ``datetime.utcnow()``.
  """
  if not value:
    return None
  try:
    cleaned = value.rstrip("Z")
    if "." in cleaned:
      head, frac = cleaned.split(".", 1)
      frac = frac.split("+", 1)[0].split("-", 1)[0]
      cleaned = f"{head}.{frac[:6]}"
    return _dt.datetime.fromisoformat(cleaned)
  except (TypeError, ValueError):
    return None


def fetch_queue(host: str, api_key: str) -> list[dict]:
  url = f"{host}/api/v1/queue?pageSize=200&includeUnknownArtistItems=true"
  status, body = _request("GET", url, api_key)
  if status >= 400:
    raise RuntimeError(f"GET /api/v1/queue returned HTTP {status}")
  try:
    return json.loads(body).get("records", [])
  except (TypeError, ValueError, json.JSONDecodeError) as exc:
    raise RuntimeError(f"queue response was not JSON: {exc}") from exc


def collect_wedged(records: list[dict]) -> list[WedgedItem]:
  out: list[WedgedItem] = []
  for r in records:
    if r.get("trackedDownloadState") != FAILED_STATE:
      continue
    qid = r.get("id")
    if not isinstance(qid, int):
      continue
    out.append(
      WedgedItem(
        queue_id=qid,
        title=str(r.get("title", "")),
        status=str(r.get("status", "")),
        tracked_state=str(r.get("trackedDownloadState", "")),
        added=_parse_iso(r.get("added")),
      )
    )
  return out


def filter_old_enough(
  items: list[WedgedItem],
  min_age_hours: float,
  now: _dt.datetime | None = None,
) -> tuple[list[WedgedItem], list[WedgedItem]]:
  """Split into (eligible, skipped) by ``added`` age.

  Items without an ``added`` timestamp are conservatively skipped.
  """
  now = now or _dt.datetime.now(_dt.UTC).replace(tzinfo=None)
  threshold = _dt.timedelta(hours=min_age_hours)
  eligible: list[WedgedItem] = []
  skipped: list[WedgedItem] = []
  for item in items:
    if item.added is None:
      skipped.append(item)
      continue
    if (now - item.added) >= threshold:
      eligible.append(item)
    else:
      skipped.append(item)
  return eligible, skipped


def delete_item(
  host: str,
  api_key: str,
  item: WedgedItem,
  *,
  blocklist: bool = True,
  skip_redownload: bool = False,
) -> bool:
  params = urllib.parse.urlencode(
    {
      "removeFromClient": "true",
      "blocklist": "true" if blocklist else "false",
      "skipRedownload": "true" if skip_redownload else "false",
    }
  )
  url = f"{host}/api/v1/queue/{item.queue_id}?{params}"
  status, _ = _request("DELETE", url, api_key)
  return 200 <= status < 300


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Drop Lidarr queue items wedged in completed/importFailed state."
  )
  parser.add_argument(
    "--min-age-hours",
    type=float,
    default=DEFAULT_MIN_AGE_HOURS,
    help=f"Only delete items older than this (default {DEFAULT_MIN_AGE_HOURS}).",
  )
  parser.add_argument(
    "--dry-run", action="store_true", help="Report planned actions and exit 0."
  )
  parser.add_argument(
    "--no-blocklist",
    action="store_true",
    help="Remove without blocklisting (NOT recommended — Tubifarry will re-grab the same junk).",
  )
  parser.add_argument(
    "--skip-redownload",
    action="store_true",
    help="Block but do not auto-search for a different release.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("LIDARR_HOST", DEFAULT_HOST).rstrip("/")
  api_key = os.environ.get("API_KEY_LIDARR")
  if not api_key:
    print("ERROR: API_KEY_LIDARR not set (check .env)", file=sys.stderr)
    return 2

  try:
    records = fetch_queue(host, api_key)
  except (urllib.error.URLError, RuntimeError) as exc:
    print(f"ERROR: cannot reach Lidarr: {exc}", file=sys.stderr)
    return 2

  all_wedged = collect_wedged(records)
  if not all_wedged:
    print("nothing to clean: 0 importFailed items in queue")
    return 0

  eligible, skipped = filter_old_enough(all_wedged, args.min_age_hours)
  if not eligible:
    print(
      f"nothing eligible: {len(all_wedged)} importFailed items all younger than "
      f"{args.min_age_hours}h (or missing 'added') — skipping"
    )
    return 0

  print(
    f"plan: remove {len(eligible)} importFailed item(s) "
    f"(skipping {len(skipped)} younger than {args.min_age_hours}h) "
    f"[blocklist={'no' if args.no_blocklist else 'yes'}, "
    f"skipRedownload={'yes' if args.skip_redownload else 'no'}]"
  )

  if args.dry_run:
    for item in eligible[:10]:
      print(f"  DRY remove #{item.queue_id} [{item.tracked_state}] {item.title[:80]}")
    if len(eligible) > 10:
      print(f"  ... and {len(eligible) - 10} more")
    return 0

  deleted = 0
  failed: list[WedgedItem] = []
  for item in eligible:
    if delete_item(
      host,
      api_key,
      item,
      blocklist=not args.no_blocklist,
      skip_redownload=args.skip_redownload,
    ):
      deleted += 1
    else:
      failed.append(item)

  print(f"removed {deleted}/{len(eligible)} importFailed item(s)")
  if failed:
    print(
      f"WARNING: {len(failed)} delete(s) failed; first id={failed[0].queue_id}",
      file=sys.stderr,
    )
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
