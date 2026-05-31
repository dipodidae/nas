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
2. **Reclaim pass (default on).** For every eligible record whose *only* blocking
   reason is ``Album release not requested`` — i.e. the peer sent a complete,
   valid album that maps to a *different* MusicBrainz release than the one Lidarr
   monitors — re-import the download via the manual-import API **with release
   switching enabled** (``disableReleaseSwitching: false``). Lidarr re-points the
   album's monitored release to the edition on disk and imports the files that
   are already there: no blocklist, no re-download. The now-satisfied queue row
   is removed with ``blocklist=false&skipRedownload=true`` so it isn't re-grabbed.
   The automatic import pipeline deliberately disables release switching (so a
   random peer can't flip your monitored edition), and there is no global toggle
   for it — manual import is the only supported path, which is why these rows sit
   wedged forever otherwise. Skipped with ``--no-reclaim``.
3. **Destructive pass.** For every remaining eligible ``importFailed`` record
   (genuine bad matches: ``Album match is not close enough``, ``Couldn't find
   similar album``, plus any reclaim that failed):
   ``DELETE /api/v1/queue/{id}?removeFromClient=true&blocklist=true&skipRedownload=true``
   That removes the row, kills the slskd transfer record (Tubifarry), and
   blocklists the specific release so it isn't re-grabbed. ``skipRedownload`` is
   **true by default**: an immediate per-row replacement search adds to the
   Soulseek search burst that triggers flood bans, so we leave re-finding to the
   paced ``lidarr_backlog_drip``. Pass ``--redownload`` to search immediately.

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
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_HOST = "http://localhost:8686"
DEFAULT_MIN_AGE_HOURS = 1.0
FAILED_STATE = "importFailed"

# A row is reclaimable when this signal is present and no hard blocker is — the
# files are a valid album, just a different MusicBrainz release than monitored.
RECLAIM_SIGNAL = "album release not requested"
HARD_BLOCKERS = (
  "not close enough",
  "couldn't find similar",
  "destination already exists",
)
# copy preserves the slskd download so the :22/:37 sweeps reap it normally.
DEFAULT_IMPORT_MODE = "copy"
# /manualimport fingerprints audio and is slow.
MANUAL_IMPORT_TIMEOUT = 120
RECLAIM_POLL_TIMEOUT = 90
RECLAIM_POLL_INTERVAL = 4.0


@dataclass(frozen=True)
class WedgedItem:
  queue_id: int
  title: str
  status: str
  tracked_state: str
  added: _dt.datetime | None
  output_path: str = ""
  messages: tuple[str, ...] = field(default_factory=tuple)


def _request(
  method: str,
  url: str,
  api_key: str,
  *,
  data: bytes | None = None,
  timeout: int = 15,
) -> tuple[int, bytes]:
  headers = {"X-Api-Key": api_key}
  if data is not None:
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, data=data, method=method, headers=headers)
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


def _flatten_messages(record: dict) -> tuple[str, ...]:
  """Flatten Lidarr's nested statusMessages into a flat tuple of strings."""
  out: list[str] = []
  for sm in record.get("statusMessages", []):
    out.extend(str(msg) for msg in sm.get("messages", []) if msg)
  return tuple(out)


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
        output_path=str(r.get("outputPath", "")),
        messages=_flatten_messages(r),
      )
    )
  return out


def is_reclaimable(item: WedgedItem) -> bool:
  """True when the row is a pure edition mismatch we can fix by release switch.

  Requires a download path to re-scan, the ``Album release not requested``
  signal, and the absence of any hard blocker (fuzzy match, no match at all,
  destination clash) that release switching cannot resolve.
  """
  if not item.output_path:
    return False
  lowered = [m.lower() for m in item.messages]
  if not any(RECLAIM_SIGNAL in m for m in lowered):
    return False
  return not any(any(b in m for b in HARD_BLOCKERS) for m in lowered)


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


def _build_import_item(file_info: dict) -> dict | None:
  """Turn a /manualimport scan entry into a /command import payload item.

  Returns None when the entry lacks the artist/album/track ids needed to
  import. ``disableReleaseSwitching: false`` is the whole point — it lets
  Lidarr re-point the monitored release to the edition on disk.
  """
  artist = file_info.get("artist") or {}
  album = file_info.get("album") or {}
  tracks = file_info.get("tracks") or []
  if not artist.get("id") or not album.get("id") or not tracks:
    return None
  track_ids = [t["id"] for t in tracks if t.get("id")]
  if not track_ids:
    return None
  return {
    "path": file_info["path"],
    "artistId": artist["id"],
    "albumId": album["id"],
    "albumReleaseId": file_info.get("albumReleaseId", 0),
    "trackIds": track_ids,
    "quality": file_info.get("quality", {}),
    "replaceExistingFiles": False,
    "disableReleaseSwitching": False,
  }


def _scan_for_import(host: str, api_key: str, folder: str) -> list[dict]:
  """GET /manualimport for a folder; returns importable payload items."""
  params = urllib.parse.urlencode({"folder": folder, "filterExistingFiles": "false"})
  url = f"{host}/api/v1/manualimport?{params}"
  status, body = _request("GET", url, api_key, timeout=MANUAL_IMPORT_TIMEOUT)
  if not (200 <= status < 300):
    raise RuntimeError(f"GET /api/v1/manualimport returned HTTP {status}")
  try:
    entries = json.loads(body)
  except (TypeError, ValueError, json.JSONDecodeError) as exc:
    raise RuntimeError(f"manualimport response was not JSON: {exc}") from exc
  items: list[dict] = []
  for entry in entries:
    if entry.get("additionalFile"):
      continue
    built = _build_import_item(entry)
    if built:
      items.append(built)
  return items


def _wait_command(host: str, api_key: str, command_id: int) -> str:
  """Poll a command until it settles; returns its final ``status`` string."""
  deadline = time.monotonic() + RECLAIM_POLL_TIMEOUT
  while time.monotonic() < deadline:
    status, body = _request("GET", f"{host}/api/v1/command/{command_id}", api_key)
    if not (200 <= status < 300):
      return "unknown"
    try:
      state = str(json.loads(body).get("status", ""))
    except (TypeError, ValueError, json.JSONDecodeError):
      return "unknown"
    if state in ("completed", "failed", "aborted"):
      return state
    time.sleep(RECLAIM_POLL_INTERVAL)
  return "timeout"


def _trackfile_count(host: str, api_key: str, album_id: int) -> int:
  """Ground-truth count of imported track files for an album (0 on error)."""
  status, body = _request("GET", f"{host}/api/v1/trackFile?albumId={album_id}", api_key)
  if not (200 <= status < 300):
    return 0
  try:
    return len(json.loads(body))
  except (TypeError, ValueError, json.JSONDecodeError):
    return 0


def _artist_path(host: str, api_key: str, artist_id: int) -> str | None:
  """Library path for an artist, used to re-scan for orphaned files."""
  status, body = _request("GET", f"{host}/api/v1/artist/{artist_id}", api_key)
  if not (200 <= status < 300):
    return None
  try:
    return json.loads(body).get("path") or None
  except (TypeError, ValueError, json.JSONDecodeError):
    return None


def _submit_import(host: str, api_key: str, items: list[dict], import_mode: str) -> bool:
  """POST a ManualImport command and wait for it to settle. True if completed."""
  payload = json.dumps(
    {"name": "ManualImport", "importMode": import_mode, "files": items}
  ).encode()
  status, body = _request(
    "POST", f"{host}/api/v1/command", api_key, data=payload, timeout=60
  )
  if not (200 <= status < 300):
    print(f"  reclaim import POST failed: HTTP {status}", file=sys.stderr)
    return False
  try:
    command_id = json.loads(body).get("id")
  except (TypeError, ValueError, json.JSONDecodeError):
    command_id = None
  if not isinstance(command_id, int):
    return False
  return _wait_command(host, api_key, command_id) == "completed"


def reclaim_item(
  host: str,
  api_key: str,
  item: WedgedItem,
  *,
  import_mode: str = DEFAULT_IMPORT_MODE,
) -> bool:
  """Re-import a wedged download with release switching enabled.

  Success is verified against the album's track-file count, not the command's
  status — a ManualImport that imports nothing (e.g. files already orphaned in
  the library from a prior ``albumImportIncomplete``) still reports
  ``completed``. So we measure before/after:

  1. Import the download folder (release switching on). If the album's track
     files increase, done.
  2. Otherwise the files were likely copied into the library already but never
     registered. Re-scan the artist folder and import those orphans in place
     (``move``), then re-measure.

  Returns True only when track files actually appear, so the caller never
  clears a queue row for an import that didn't happen.
  """
  try:
    items = _scan_for_import(host, api_key, item.output_path)
  except (urllib.error.URLError, RuntimeError) as exc:
    print(f"  reclaim scan failed for #{item.queue_id}: {exc}", file=sys.stderr)
    return False
  if not items:
    return False

  album_ids = sorted({it["albumId"] for it in items})
  artist_ids = sorted({it["artistId"] for it in items})
  before = sum(_trackfile_count(host, api_key, a) for a in album_ids)

  _submit_import(host, api_key, items, import_mode)
  if sum(_trackfile_count(host, api_key, a) for a in album_ids) > before:
    return True

  # No-op import: register orphaned in-library files in place.
  wanted = set(album_ids)
  for artist_id in artist_ids:
    path = _artist_path(host, api_key, artist_id)
    if not path:
      continue
    try:
      in_place = [it for it in _scan_for_import(host, api_key, path) if it["albumId"] in wanted]
    except (urllib.error.URLError, RuntimeError):
      continue
    if in_place:
      _submit_import(host, api_key, in_place, "move")

  return sum(_trackfile_count(host, api_key, a) for a in album_ids) > before


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
    "--redownload",
    action="store_true",
    help=(
      "After blocklisting a bad release, immediately fire a replacement search. "
      "Off by default: an immediate per-row search burst contributes to Soulseek "
      "flood bans, and the paced backlog drip re-finds these albums anyway."
    ),
  )
  parser.add_argument(
    "--no-reclaim",
    action="store_true",
    help=(
      "Disable the release-switch reclaim pass; send every importFailed row "
      "straight to delete+blocklist+redownload (legacy behaviour)."
    ),
  )
  parser.add_argument(
    "--import-mode",
    default=DEFAULT_IMPORT_MODE,
    choices=("copy", "move"),
    help=f"Manual-import mode for reclaimed rows (default {DEFAULT_IMPORT_MODE}).",
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

  if args.no_reclaim:
    reclaimable: list[WedgedItem] = []
    to_delete = list(eligible)
  else:
    reclaimable = [i for i in eligible if is_reclaimable(i)]
    to_delete = [i for i in eligible if not is_reclaimable(i)]

  skip_redownload = not args.redownload
  print(
    f"plan: reclaim {len(reclaimable)} (release switch), "
    f"remove {len(to_delete)} importFailed item(s) "
    f"(skipping {len(skipped)} younger than {args.min_age_hours}h) "
    f"[blocklist={'no' if args.no_blocklist else 'yes'}, "
    f"skipRedownload={'yes' if skip_redownload else 'no'}]"
  )

  if args.dry_run:
    for item in reclaimable[:10]:
      print(f"  DRY reclaim #{item.queue_id} [release switch] {item.title[:80]}")
    for item in to_delete[:10]:
      print(f"  DRY remove #{item.queue_id} [{item.tracked_state}] {item.title[:80]}")
    extra = len(reclaimable[10:]) + len(to_delete[10:])
    if extra:
      print(f"  ... and {extra} more")
    return 0

  # Reclaim pass: re-import valid albums that just need a release switch.
  reclaimed = 0
  for item in reclaimable:
    if reclaim_item(host, api_key, item, import_mode=args.import_mode):
      reclaimed += 1
      # Album is satisfied — drop the row without blocklist or re-search.
      if not delete_item(host, api_key, item, blocklist=False, skip_redownload=True):
        print(
          f"  reclaimed #{item.queue_id} but row cleanup failed "
          "(Lidarr will clear it next cycle)",
          file=sys.stderr,
        )
    else:
      print(f"  reclaim failed for #{item.queue_id}; falling through to delete")
      to_delete.append(item)
  if reclaimable:
    print(f"reclaimed {reclaimed}/{len(reclaimable)} via release switch")

  # Destructive pass: genuine bad matches + failed reclaims.
  deleted = 0
  failed: list[WedgedItem] = []
  for item in to_delete:
    if delete_item(
      host,
      api_key,
      item,
      blocklist=not args.no_blocklist,
      skip_redownload=skip_redownload,
    ):
      deleted += 1
    else:
      failed.append(item)

  print(f"removed {deleted}/{len(to_delete)} importFailed item(s)")
  if failed:
    print(
      f"WARNING: {len(failed)} delete(s) failed; first id={failed[0].queue_id}",
      file=sys.stderr,
    )
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
