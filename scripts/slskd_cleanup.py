#!/usr/bin/env python3
"""Clear stale slskd transfer records + matching orphan incomplete dirs.

Background
----------
Tubifarry (Lidarr's slskd plugin) only removes slskd transfer records while the
item is still in Lidarr's queue. Once Lidarr finishes import and drops the
queue entry, slskd keeps the transfer in `Completed, Succeeded` (or `Errored`
/ `Rejected`) state forever. Over time this clogs slskd's transfer manager and
peer connection state, manifesting as slow / timed-out searches between
Lidarr and slskd.

What this script does
---------------------
1. GET `/api/v0/transfers/downloads` and collect every row whose `state`
   starts with `Completed`.
2. DELETE each such transfer (`?remove=true`).
3. For each removed transfer, compute the trailing segment of the slskd
   directory path (e.g. ``albums\\Killing Joke\\Democracy`` -> ``Democracy``)
   and remove the matching dir under ``INCOMPLETE_DIR`` — but ONLY if slskd itself
   reported that dir name. We never touch a path slskd did not list, because
   qBittorrent shares the same `/downloads/incomplete` and indiscriminate
   deletion would destroy in-progress torrents.

Safety rails (avoid racing the Tubifarry / Lidarr import flow)
--------------------------------------------------------------
- **Lidarr-quiet gate** (selective): if any Lidarr queue item is in
  `downloading` / `importPending` / `importing` / `importBlocked` /
  `importFailed`, defer deletion of `Completed, Succeeded` records — Tubifarry
  may still be importing them, OR `lidarr_queue_unstick.py` is about to issue
  a DELETE through Tubifarry to clear the matching slskd record. Either way,
  we let Lidarr's side win that race. Terminal-failure states (`Completed,
  Errored / Rejected / Cancelled`) are still cleaned — they will never trigger
  a Tubifarry callback, so gating them just lets them pile up indefinitely.
- **Per-record age gate**: only delete a slskd record whose `endedAt` is
  older than `--min-age-hours` (default 1). Records with no `endedAt` (very
  old slskd schemas) are skipped conservatively.
- **Per-dir age gate**: only remove `/downloads/incomplete/<name>` whose
  mtime is older than `--min-age-hours` too.
- `--dry-run` prints the plan and exits 0 without making changes.
- Only names the slskd API itself returned are ever removed from disk; we
  never sweep `/downloads/incomplete` blindly because qBittorrent uses it
  too (`Session\\TempPath`).

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some deletes failed; details on stderr)
  2 fatal (config missing, slskd unreachable, HTTP error)

Environment
-----------
  API_KEY_SLSKD    (required) administrator key for `/api/v0`
  API_KEY_LIDARR   (required) Lidarr API key, used for the quiet-gate check
  SLSKD_HOST       (default: http://localhost:5030)
  LIDARR_HOST      (default: http://localhost:8686)
  INCOMPLETE_DIR   (default: /mnt/drive/downloads/incomplete)

Usage
-----
  python scripts/slskd_cleanup.py                 # clean now
  python scripts/slskd_cleanup.py --dry-run       # report only
  python scripts/slskd_cleanup.py --min-age-hours 6
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if "API_KEY_SLSKD" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_HOST = "http://localhost:5030"
DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_INCOMPLETE_DIR = "/mnt/drive/downloads/incomplete"
DEFAULT_MIN_AGE_HOURS = 1
COMPLETED_PREFIX = "Completed"
# Only `Completed, Succeeded` could plausibly still be mid-import by Tubifarry;
# the other terminal states will never trigger a Tubifarry callback, so they
# are safe to delete regardless of Lidarr's queue activity.
GATED_COMPLETED_STATE = "Completed, Succeeded"
# Lidarr states whose backing slskd transfer is "Completed, Succeeded" AND
# which a peer script (`lidarr_queue_unstick.py`) or Lidarr itself is going
# to act on. We must not race them by deleting the slskd record first:
#   - downloading / importPending / importing / importBlocked: Tubifarry's
#     own import flow may still touch the slskd record.
#   - importFailed: `lidarr_queue_unstick.py` will DELETE the queue item with
#     removeFromClient=true, which routes through Tubifarry to delete the
#     slskd record. Deleting it from under it would race.
ACTIVE_LIDARR_STATES = frozenset(
  {"downloading", "importPending", "importing", "importBlocked", "importFailed"}
)


@dataclass(frozen=True)
class StaleTransfer:
  username: str
  transfer_id: str
  state: str
  slskd_dir: str
  local_dirname: str
  ended_at: _dt.datetime | None


def _request(
  method: str,
  url: str,
  api_key: str,
  *,
  header: str = "X-API-Key",
  timeout: int = 15,
) -> tuple[int, bytes]:
  req = urllib.request.Request(url, method=method, headers={header: api_key})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return resp.status, resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read()


def _parse_iso(value: str | None) -> _dt.datetime | None:
  """Parse slskd's ISO-8601 timestamps (with optional sub-second precision).

  slskd emits values like ``2026-05-24T09:19:21.7392604`` (no timezone).
  We treat them as naive UTC for comparison purposes.
  """
  if not value:
    return None
  try:
    cleaned = value.rstrip("Z")
    # Python's fromisoformat (3.11+) accepts most variants but caps fractional
    # seconds at 6 digits; truncate anything longer.
    if "." in cleaned:
      head, frac = cleaned.split(".", 1)
      frac = frac.split("+", 1)[0].split("-", 1)[0]
      cleaned = f"{head}.{frac[:6]}"
    return _dt.datetime.fromisoformat(cleaned)
  except (TypeError, ValueError):
    return None


def _trailing_segment(slskd_path: str) -> str:
  """Return the last path component of a slskd directory string.

  slskd reports directories using backslashes (and sometimes forward slashes
  from Linux peers). Normalize and take the last non-empty segment.
  """
  if not slskd_path:
    return ""
  normalized = slskd_path.replace("/", "\\").rstrip("\\")
  if "\\" not in normalized:
    return normalized
  return normalized.rsplit("\\", 1)[-1]


def fetch_downloads(host: str, api_key: str) -> list[dict]:
  status, body = _request("GET", f"{host}/api/v0/transfers/downloads", api_key)
  if status >= 400:
    raise RuntimeError(f"GET /api/v0/transfers/downloads returned HTTP {status}")
  return json.loads(body)


def collect_stale(downloads: list[dict]) -> list[StaleTransfer]:
  out: list[StaleTransfer] = []
  for user in downloads:
    username = user.get("username", "")
    for directory in user.get("directories", []):
      slskd_dir = directory.get("directory", "")
      local = _trailing_segment(slskd_dir)
      for file in directory.get("files", []):
        state = file.get("state", "")
        if state.startswith(COMPLETED_PREFIX):
          out.append(
            StaleTransfer(
              username=username,
              transfer_id=file.get("id", ""),
              state=state,
              slskd_dir=slskd_dir,
              local_dirname=local,
              ended_at=_parse_iso(file.get("endedAt")),
            )
          )
  return out


def filter_old_enough(
  transfers: list[StaleTransfer], min_age_hours: float, now: _dt.datetime | None = None
) -> tuple[list[StaleTransfer], list[StaleTransfer]]:
  """Split transfers into (eligible, skipped) by `endedAt` age.

  Transfers with no `endedAt` are skipped conservatively — without a
  reliable timestamp we cannot prove they are safe to delete.
  """
  now = now or _dt.datetime.now()
  threshold = _dt.timedelta(hours=min_age_hours)
  eligible: list[StaleTransfer] = []
  skipped: list[StaleTransfer] = []
  for transfer in transfers:
    if transfer.ended_at is None:
      skipped.append(transfer)
      continue
    if (now - transfer.ended_at) >= threshold:
      eligible.append(transfer)
    else:
      skipped.append(transfer)
  return eligible, skipped


def partition_by_gate(
  stale: list[StaleTransfer], *, lidarr_busy: bool
) -> tuple[list[StaleTransfer], list[StaleTransfer]]:
  """Split stale transfers into (deletable_now, deferred) by the Lidarr gate.

  When Lidarr has items mid-flow, `Completed, Succeeded` records are deferred
  (Tubifarry may still be importing them). All other terminal-failure
  `Completed,*` states are returned as deletable regardless.
  """
  if not lidarr_busy:
    return stale, []
  deletable: list[StaleTransfer] = []
  deferred: list[StaleTransfer] = []
  for transfer in stale:
    if transfer.state == GATED_COMPLETED_STATE:
      deferred.append(transfer)
    else:
      deletable.append(transfer)
  return deletable, deferred


def lidarr_active_imports(host: str, api_key: str) -> int:
  """Return how many Lidarr queue items are mid-flow (downloading/importing).

  Returns -1 if Lidarr is unreachable — caller should treat that as
  "unknown" and decline to delete anything.
  """
  url = f"{host}/api/v1/queue?pageSize=200&includeUnknownArtistItems=true"
  try:
    status, body = _request("GET", url, api_key, header="X-Api-Key")
  except urllib.error.URLError:
    return -1
  if status >= 400:
    return -1
  try:
    records = json.loads(body).get("records", [])
  except (TypeError, ValueError, json.JSONDecodeError):
    return -1
  return sum(
    1 for r in records if r.get("trackedDownloadState") in ACTIVE_LIDARR_STATES
  )


def delete_transfer(host: str, api_key: str, transfer: StaleTransfer) -> bool:
  user = urllib.parse.quote(transfer.username, safe="")
  url = f"{host}/api/v0/transfers/downloads/{user}/{transfer.transfer_id}?remove=true"
  status, _ = _request("DELETE", url, api_key)
  return status == 204


def remove_orphan_dirs(
  incomplete_root: Path, names: set[str], min_age_hours: float
) -> tuple[int, int, list[str]]:
  """Remove only dirs whose name appears in `names`.

  Returns (removed_count, skipped_count, errors).
  """
  removed = 0
  skipped = 0
  errors: list[str] = []
  threshold_seconds = min_age_hours * 3600
  now = time.time()

  for name in names:
    if not name:
      continue
    target = incomplete_root / name
    if not target.exists() or not target.is_dir():
      continue
    try:
      mtime = target.stat().st_mtime
    except OSError as exc:
      errors.append(f"stat {target}: {exc}")
      continue
    if (now - mtime) < threshold_seconds:
      skipped += 1
      continue
    try:
      shutil.rmtree(target)
      removed += 1
    except OSError as exc:
      errors.append(f"rmtree {target}: {exc}")
  return removed, skipped, errors


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Clear stale slskd Completed,* transfers and matching orphan dirs."
  )
  parser.add_argument(
    "--min-age-hours",
    type=float,
    default=DEFAULT_MIN_AGE_HOURS,
    help=f"Only delete dirs whose mtime is older than this (default {DEFAULT_MIN_AGE_HOURS}).",
  )
  parser.add_argument(
    "--incomplete-dir",
    type=Path,
    default=None,
    help=f"Override INCOMPLETE_DIR (default env or {DEFAULT_INCOMPLETE_DIR}).",
  )
  parser.add_argument(
    "--dry-run", action="store_true", help="Report planned actions and exit 0."
  )
  parser.add_argument(
    "--keep-dirs",
    action="store_true",
    help="Only clear API records; do not touch the filesystem.",
  )
  parser.add_argument(
    "--skip-lidarr-check",
    action="store_true",
    help="Skip the Lidarr-queue idle check (use only for manual one-offs).",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("SLSKD_HOST", DEFAULT_HOST).rstrip("/")
  api_key = os.environ.get("API_KEY_SLSKD")
  if not api_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2

  lidarr_host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  lidarr_key = os.environ.get("API_KEY_LIDARR")
  if not lidarr_key:
    print(
      "ERROR: API_KEY_LIDARR not set; required for the quiet-gate "
      "(use --skip-lidarr-check only if you really mean it)",
      file=sys.stderr,
    )
    if not args.skip_lidarr_check:
      return 2

  incomplete_root = args.incomplete_dir or Path(
    os.environ.get("INCOMPLETE_DIR", DEFAULT_INCOMPLETE_DIR)
  )

  lidarr_busy = 0
  if lidarr_key and not args.skip_lidarr_check:
    lidarr_busy = lidarr_active_imports(lidarr_host, lidarr_key)
    if lidarr_busy < 0:
      print(
        "ERROR: cannot reach Lidarr to verify the queue is idle; "
        "refusing to touch slskd records",
        file=sys.stderr,
      )
      return 2

  try:
    downloads = fetch_downloads(host, api_key)
  except (urllib.error.URLError, RuntimeError) as exc:
    print(f"ERROR: cannot reach slskd: {exc}", file=sys.stderr)
    return 2

  all_stale = collect_stale(downloads)
  if not all_stale:
    print("nothing to clean: 0 Completed transfers, 0 dirs")
    return 0

  if lidarr_busy > 0 and not args.skip_lidarr_check:
    all_stale, deferred = partition_by_gate(all_stale, lidarr_busy=True)
    print(
      f"Lidarr has {lidarr_busy} item(s) downloading/importing — "
      f"deferring {len(deferred)} 'Completed, Succeeded' record(s); "
      f"will still clean {len(all_stale)} terminal-failure record(s)"
    )
    if not all_stale:
      return 0

  stale, too_recent = filter_old_enough(all_stale, args.min_age_hours)
  if not stale:
    print(
      f"nothing eligible: {len(all_stale)} Completed transfers all younger "
      f"than {args.min_age_hours}h (or missing endedAt) — skipping"
    )
    return 0

  unique_dirs = {t.local_dirname for t in stale if t.local_dirname}
  print(
    f"plan: remove {len(stale)} transfer records "
    f"(skipping {len(too_recent)} younger than {args.min_age_hours}h) "
    f"and up to {len(unique_dirs)} dirs in {incomplete_root}"
  )

  if args.dry_run:
    for t in stale[:10]:
      print(f"  DRY transfer: {t.username}/{t.transfer_id} [{t.state}] -> {t.local_dirname}")
    if len(stale) > 10:
      print(f"  ... and {len(stale) - 10} more")
    return 0

  deleted = 0
  failed_transfers: list[StaleTransfer] = []
  for transfer in stale:
    if delete_transfer(host, api_key, transfer):
      deleted += 1
    else:
      failed_transfers.append(transfer)

  if failed_transfers:
    print(
      f"WARNING: {len(failed_transfers)} transfer DELETEs failed; "
      f"first: {failed_transfers[0].username}/{failed_transfers[0].transfer_id}",
      file=sys.stderr,
    )

  if args.keep_dirs:
    print(f"removed {deleted}/{len(stale)} transfer records; filesystem untouched")
    return 0 if not failed_transfers else 1

  if not incomplete_root.exists():
    print(
      f"WARNING: incomplete dir {incomplete_root} not found; skipping fs cleanup",
      file=sys.stderr,
    )
    return 1 if failed_transfers else 0

  # Only consider names from transfers we successfully removed (no point
  # deleting a dir whose record still exists in slskd).
  succeeded_names = {
    t.local_dirname
    for t in stale
    if t.local_dirname and t not in failed_transfers
  }
  removed_dirs, skipped_dirs, fs_errors = remove_orphan_dirs(
    incomplete_root, succeeded_names, args.min_age_hours
  )
  for err in fs_errors:
    print(f"WARNING: {err}", file=sys.stderr)

  print(
    f"removed {deleted}/{len(stale)} transfer records, "
    f"{removed_dirs} dirs (skipped {skipped_dirs} too-recent, "
    f"{len(fs_errors)} fs errors)"
  )

  if failed_transfers or fs_errors:
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
