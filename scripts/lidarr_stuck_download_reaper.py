#!/usr/bin/env python3
"""Reap Lidarr grabs wedged forever at 0 bytes in slskd, and re-source them.

Background
----------
When Tubifarry hands a release to slskd, the files sit in ``Queued, Remotely``
(waiting in the remote peer's upload queue) until that peer starts uploading.
If the peer is offline, gone, or has frozen us at queue position N, those files
*never* start — slskd has no timeout for remotely-queued downloads, and Lidarr
keeps the matching queue row in ``downloading`` indefinitely. These
"stuck-at-0-bytes" rows are NOT handled by ``lidarr_queue_unstick.py`` (which
only drains ``importFailed``) nor by ``slskd_cleanup.py`` (which only removes
``Completed,*`` records). They accumulate, pin slskd's in-flight count high
(starving ``lidarr_backlog_drip.py``), and the album never arrives.

What this script does
---------------------
1. GET ``/api/v0/transfers/downloads`` from slskd and find every file that is
   still ``Queued, *`` with **zero bytes transferred** whose ``enqueuedAt`` is
   older than ``--stuck-hours`` (default 12). A started-then-paused transfer
   (``bytesTransferred`` > 0) is treated as alive and left alone.
2. Map each stuck file's album dir basename to an active Lidarr queue row
   (by ``outputPath`` / ``title`` basename — the same matching
   ``slskd_cleanup.py`` uses).
3. For a matched row: ``DELETE /api/v1/queue/{id}`` with
   ``removeFromClient=true&blocklist=true&skipRedownload=true`` — Tubifarry
   cancels the slskd transfer, Lidarr blocklists that dead release, and
   ``lidarr_monitor_sweep.py`` / ``lidarr_backlog_drip.py`` re-source it from a
   live peer on their own throttled schedule (``skipRedownload`` avoids a
   re-search storm / slskd flood-ban). This mirrors ``lidarr_queue_unstick``.
4. For a stuck slskd transfer with no Lidarr match (orphaned grab): cancel it
   directly via ``DELETE /api/v0/transfers/downloads/{user}/{id}?remove=true``.

Safety rails
------------
- Only zero-byte transfers are touched — no download progress is ever lost.
- ``--stuck-hours`` (default 12) keeps legitimately-slow peer queues alive; a
  grab must be dead for this long before it is reaped.
- ``--max-actions`` (default 40) caps the blast radius per run so a backlog of
  dead grabs drains over several hourly runs rather than hammering Lidarr.
- Transfers with no ``enqueuedAt`` are skipped conservatively.
- ``--dry-run`` prints the plan and exits 0.

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some deletes failed; details on stderr)
  2 fatal (config missing, slskd/Lidarr unreachable, HTTP error)

Environment
-----------
  API_KEY_SLSKD    (required) administrator key for slskd ``/api/v0``
  API_KEY_LIDARR   (required) Lidarr API key
  SLSKD_HOST       (default: http://localhost:5030)
  LIDARR_HOST      (default: http://localhost:8686)

Usage
-----
  python scripts/lidarr_stuck_download_reaper.py --dry-run
  python scripts/lidarr_stuck_download_reaper.py --stuck-hours 12 --max-actions 40
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
from dataclasses import dataclass, field

if "API_KEY_SLSKD" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_SLSKD_HOST = "http://localhost:5030"
DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_STUCK_HOURS = 12.0
DEFAULT_MAX_ACTIONS = 40
UTC = _dt.UTC


@dataclass(frozen=True)
class StuckTransfer:
  username: str
  transfer_id: str
  state: str
  slskd_dir: str
  local_dirname: str
  enqueued_at: _dt.datetime | None
  bytes_transferred: int


@dataclass
class ReapPlan:
  lidarr_deletes: list[int] = field(default_factory=list)
  slskd_cancels: list[StuckTransfer] = field(default_factory=list)
  capped: int = 0


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
  """Parse slskd's ISO-8601 timestamps as tz-aware UTC.

  slskd emits naive UTC values like ``2026-06-05T09:37:37.6886499``; we attach
  UTC so age math is correct regardless of the host's local timezone.
  """
  if not value:
    return None
  try:
    cleaned = value.rstrip("Z")
    if "." in cleaned:
      head, frac = cleaned.split(".", 1)
      frac = frac.split("+", 1)[0].split("-", 1)[0]
      cleaned = f"{head}.{frac[:6]}"
    dt = _dt.datetime.fromisoformat(cleaned)
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
  except (TypeError, ValueError):
    return None


def _trailing_segment(slskd_path: str) -> str:
  """Return the last path component of a slskd directory string."""
  if not slskd_path:
    return ""
  normalized = slskd_path.replace("/", "\\").rstrip("\\")
  if "\\" not in normalized:
    return normalized
  return normalized.rsplit("\\", 1)[-1]


def collect_stuck(
  downloads: object, *, stuck_hours: float, now: _dt.datetime | None = None
) -> list[StuckTransfer]:
  """Find zero-byte ``Queued, *`` transfers older than ``stuck_hours``.

  Pure over the ``/transfers/downloads`` payload. A transfer is "stuck" only
  if it is still queued (never started), has transferred zero bytes, and has a
  parseable ``enqueuedAt`` (or ``requestedAt``) older than the threshold.
  """
  if not isinstance(downloads, list):
    return []
  now = now or _dt.datetime.now(UTC)
  threshold = _dt.timedelta(hours=stuck_hours)
  out: list[StuckTransfer] = []
  for user in downloads:
    username = user.get("username", "")
    for directory in user.get("directories", []):
      slskd_dir = directory.get("directory", "")
      local = _trailing_segment(slskd_dir)
      for file in directory.get("files", []):
        state = str(file.get("state", ""))
        if not state.startswith("Queued"):
          continue
        if (file.get("bytesTransferred") or 0) > 0:
          continue
        enq = _parse_iso(file.get("enqueuedAt") or file.get("requestedAt"))
        if enq is None or (now - enq) < threshold:
          continue
        out.append(
          StuckTransfer(
            username=username,
            transfer_id=file.get("id", ""),
            state=state,
            slskd_dir=slskd_dir,
            local_dirname=local,
            enqueued_at=enq,
            bytes_transferred=0,
          )
        )
  return out


def build_lidarr_map(records: list[dict]) -> dict[str, int]:
  """Map dir/release basenames -> Lidarr queue id for active rows.

  Reduces every path-like field to its basename so it can be compared against
  :class:`StuckTransfer.local_dirname`.
  """
  out: dict[str, int] = {}
  for r in records:
    qid = r.get("id")
    if not isinstance(qid, int):
      continue
    for key in ("outputPath", "downloadForcedClientPath", "title"):
      val = r.get(key)
      if isinstance(val, str) and val:
        name = os.path.basename(val.rstrip("/").rstrip("\\"))
        if name:
          out.setdefault(name, qid)
  return out


def plan_reap(
  stuck: list[StuckTransfer], lidarr_map: dict[str, int], *, max_actions: int
) -> ReapPlan:
  """Route stuck transfers: matched -> Lidarr delete, orphaned -> slskd cancel.

  Lidarr rows are de-duplicated (one album = many files), and the total number
  of actions is capped at ``max_actions`` so a large backlog drains over
  several runs. The number of stuck items left for a later run is recorded in
  ``ReapPlan.capped``.
  """
  plan = ReapPlan()
  seen_qids: set[int] = set()
  actions = 0
  for t in stuck:
    if actions >= max_actions:
      plan.capped += 1
      continue
    qid = lidarr_map.get(t.local_dirname)
    if qid is not None:
      if qid in seen_qids:
        continue  # already scheduled this album's row (doesn't consume budget)
      seen_qids.add(qid)
      plan.lidarr_deletes.append(qid)
      actions += 1
    else:
      plan.slskd_cancels.append(t)
      actions += 1
  return plan


def delete_lidarr_item(
  host: str, api_key: str, queue_id: int, *, blocklist: bool = True
) -> bool:
  params = urllib.parse.urlencode(
    {
      "removeFromClient": "true",
      "blocklist": "true" if blocklist else "false",
      "skipRedownload": "true",
    }
  )
  url = f"{host}/api/v1/queue/{queue_id}?{params}"
  status, _ = _request("DELETE", url, api_key, header="X-Api-Key")
  return status in (200, 204)


def cancel_slskd_transfer(host: str, api_key: str, transfer: StuckTransfer) -> bool:
  user = urllib.parse.quote(transfer.username, safe="")
  url = f"{host}/api/v0/transfers/downloads/{user}/{transfer.transfer_id}?remove=true"
  status, _ = _request("DELETE", url, api_key)
  # 404 == already gone (e.g. Tubifarry beat us to it) — treat as success.
  return status in (200, 204, 404)


def fetch_slskd_downloads(host: str, api_key: str) -> list[dict]:
  status, body = _request("GET", f"{host}/api/v0/transfers/downloads", api_key)
  if status >= 400:
    raise RuntimeError(f"GET /api/v0/transfers/downloads returned HTTP {status}")
  return json.loads(body) if body else []


def fetch_lidarr_queue(host: str, api_key: str) -> list[dict]:
  url = f"{host}/api/v1/queue?pageSize=1000&includeUnknownArtistItems=true"
  status, body = _request("GET", url, api_key, header="X-Api-Key")
  if status >= 400:
    raise RuntimeError(f"GET /api/v1/queue returned HTTP {status}")
  return json.loads(body).get("records", [])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Reap Lidarr grabs wedged at 0 bytes in slskd and re-source them."
  )
  parser.add_argument(
    "--stuck-hours", type=float, default=DEFAULT_STUCK_HOURS,
    help=f"Reap zero-byte queued transfers older than this (default {DEFAULT_STUCK_HOURS}).",
  )
  parser.add_argument(
    "--max-actions", type=int, default=DEFAULT_MAX_ACTIONS,
    help=f"Cap actions per run to drain gradually (default {DEFAULT_MAX_ACTIONS}).",
  )
  parser.add_argument(
    "--no-blocklist", action="store_true",
    help="Remove without blocklisting (NOT recommended — Lidarr may re-grab the dead release).",
  )
  parser.add_argument(
    "--dry-run", action="store_true", help="Report the plan and exit 0."
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  slskd_key = os.environ.get("API_KEY_SLSKD")
  lidarr_host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  lidarr_key = os.environ.get("API_KEY_LIDARR")
  if not slskd_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2
  if not lidarr_key:
    print("ERROR: API_KEY_LIDARR not set (check .env)", file=sys.stderr)
    return 2

  try:
    downloads = fetch_slskd_downloads(slskd_host, slskd_key)
  except (urllib.error.URLError, RuntimeError) as exc:
    print(f"ERROR: cannot reach slskd: {exc}", file=sys.stderr)
    return 2

  stuck = collect_stuck(downloads, stuck_hours=args.stuck_hours)
  if not stuck:
    print(f"nothing stuck: 0 zero-byte queued transfers older than {args.stuck_hours}h")
    return 0

  try:
    records = fetch_lidarr_queue(lidarr_host, lidarr_key)
  except (urllib.error.URLError, RuntimeError) as exc:
    print(f"ERROR: cannot reach Lidarr: {exc}", file=sys.stderr)
    return 2

  lidarr_map = build_lidarr_map(records)
  plan = plan_reap(stuck, lidarr_map, max_actions=args.max_actions)

  print(
    f"plan: {len(stuck)} stuck transfer(s) -> "
    f"reap {len(plan.lidarr_deletes)} Lidarr row(s) "
    f"(blocklist={'no' if args.no_blocklist else 'yes'}, re-source via sweep/drip), "
    f"cancel {len(plan.slskd_cancels)} orphan slskd transfer(s)"
    + (f"; {plan.capped} deferred to next run (--max-actions {args.max_actions})"
       if plan.capped else "")
  )

  if args.dry_run:
    for qid in plan.lidarr_deletes[:10]:
      print(f"  DRY lidarr-delete queue/{qid}")
    for t in plan.slskd_cancels[:10]:
      print(f"  DRY slskd-cancel {t.username}/{t.transfer_id} -> {t.local_dirname}")
    return 0

  failures = 0
  reaped_l = 0
  for qid in plan.lidarr_deletes:
    if delete_lidarr_item(lidarr_host, lidarr_key, qid, blocklist=not args.no_blocklist):
      reaped_l += 1
    else:
      failures += 1
      print(f"WARNING: failed to delete Lidarr queue/{qid}", file=sys.stderr)

  cancelled_s = 0
  for t in plan.slskd_cancels:
    if cancel_slskd_transfer(slskd_host, slskd_key, t):
      cancelled_s += 1
    else:
      failures += 1
      print(
        f"WARNING: failed to cancel slskd {t.username}/{t.transfer_id}",
        file=sys.stderr,
      )

  print(
    f"reaped {reaped_l}/{len(plan.lidarr_deletes)} Lidarr row(s), "
    f"cancelled {cancelled_s}/{len(plan.slskd_cancels)} orphan slskd transfer(s)"
  )
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
