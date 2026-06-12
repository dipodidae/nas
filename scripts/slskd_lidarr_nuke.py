#!/usr/bin/env python3
"""Clean-slate the slskd<->Lidarr pipeline: nuke the Lidarr queue, wipe slskd
transfers, and sweep the slskd completed-downloads folder.

This is the aggressive on-demand counterpart to the gated/throttled reapers
(lidarr_stuck_download_reaper.py, slskd_cleanup.py, slskd_complete_sweep.py).
It resets the whole pipeline to zero, GRACEFULLY:

  Phase 1 (Lidarr, first): DELETE every queue row with
    removeFromClient=true&blocklist=true&skipRedownload=true so Lidarr cancels
    the slskd-side transfer via Tubifarry (nothing orphaned), blocklists the
    dead release (album stays monitored), and does NOT auto re-search.
  Phase 2 (slskd, mop-up): cancel every still-active transfer and clear all
    terminal records -> empty transfer manager.
  Phase 3 (disk, last): rmtree every dir under SLSKD_COMPLETE_DIR except those
    an active Lidarr import still references.

Lidarr on this host uses slskd (Tubifarry) as its ONLY download client, so the
entire queue is slskd-sourced and is wiped wholesale.

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some deletes/cancels/rmtrees failed; details on stderr)
  2 fatal (config missing, slskd/Lidarr unreachable, containment violation)

Environment
-----------
  API_KEY_LIDARR       (required) Lidarr API key
  API_KEY_SLSKD        (required) administrator key for slskd /api/v0
  LIDARR_HOST          (default: http://localhost:8686)
  SLSKD_HOST           (default: http://localhost:5030)
  SLSKD_COMPLETE_DIR   (default: /mnt/drive/downloads/complete/slskd)

Usage
-----
  python scripts/slskd_lidarr_nuke.py            # ACT: full clean slate
  python scripts/slskd_lidarr_nuke.py --dry-run  # preview, exit 0
  python scripts/slskd_lidarr_nuke.py --skip-folder-sweep
  python scripts/slskd_lidarr_nuke.py --skip-lidarr --skip-slskd  # folder only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
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

DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_SLSKD_HOST = "http://localhost:5030"
DEFAULT_SLSKD_COMPLETE_DIR = "/mnt/drive/downloads/complete/slskd"
TERMINAL_PREFIX = "Completed"


@dataclass(frozen=True)
class SlskdTransfer:
  username: str
  transfer_id: str
  state: str


def _request(
  method: str,
  url: str,
  api_key: str,
  *,
  header: str = "X-API-Key",
  data: bytes | None = None,
  timeout: int = 20,
) -> tuple[int, bytes]:
  headers = {header: api_key}
  if data is not None:
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, method=method, headers=headers, data=data)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return resp.status, resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read()


def plan_lidarr_nuke(records: list[dict]) -> list[int]:
  """Return every Lidarr queue id to delete (the whole queue).

  Pure over a ``/api/v1/queue`` records list. Rows without an integer ``id``
  are skipped defensively. Order is preserved; ids are unique within a queue.
  """
  return [r["id"] for r in records if isinstance(r.get("id"), int)]


def collect_slskd_transfers(downloads: object) -> tuple[list[SlskdTransfer], int]:
  """Partition slskd downloads into (active-to-cancel, terminal_record_count).

  Pure over the ``/api/v0/transfers/downloads`` payload. Any transfer whose
  state does NOT start with ``Completed`` is "active" and must be cancelled;
  terminal ``Completed,*`` rows are counted (they are cleared in bulk).
  """
  if not isinstance(downloads, list):
    return [], 0
  active: list[SlskdTransfer] = []
  terminal = 0
  for user in downloads:
    username = user.get("username", "")
    for directory in user.get("directories", []):
      for file in directory.get("files", []):
        state = str(file.get("state", ""))
        if state.startswith(TERMINAL_PREFIX):
          terminal += 1
        else:
          active.append(
            SlskdTransfer(
              username=username,
              transfer_id=file.get("id", ""),
              state=state,
            )
          )
  return active, terminal


def spare_basenames(records: list[dict]) -> set[str]:
  """Path basenames any active Lidarr import still references.

  Mirrors slskd_complete_sweep.active_queue_paths: reduces each path-like field
  to its basename so it can be matched against a completed-dir name.
  Handles both POSIX (``/``) and Windows (``\\``) path separators.
  """
  out: set[str] = set()
  for r in records:
    for key in ("outputPath", "downloadForcedClientPath", "title"):
      val = r.get(key)
      if isinstance(val, str) and val:
        # Normalise to forward-slashes first so os.path.basename works on both
        # POSIX paths (/data/.../Album) and Windows-style paths (music\\Artist\\Album\\)
        normalized = val.replace("\\", "/").rstrip("/")
        name = os.path.basename(normalized)
        if name:
          out.add(name)
  return out


def plan_folder_sweep(complete_dir: Path, spare: set[str]) -> list[Path]:
  """Direct-child dirs under ``complete_dir`` to delete (not in ``spare``).

  Containment guard: every candidate's resolved parent must equal the resolved
  ``complete_dir``; a child that escapes (e.g. a symlink to elsewhere) raises
  ``ValueError`` so the caller aborts rather than deleting outside the folder.
  """
  resolved_root = complete_dir.resolve()
  targets: list[Path] = []
  for child in sorted(complete_dir.iterdir()):
    if not child.is_dir():
      continue
    if child.resolve().parent != resolved_root:
      raise ValueError(f"{child} escapes {complete_dir} — refusing to sweep")
    if child.name in spare:
      continue
    targets.append(child)
  return targets


def fetch_lidarr_queue(host: str, api_key: str) -> list[dict]:
  url = f"{host}/api/v1/queue?pageSize=1000&includeUnknownArtistItems=true"
  status, body = _request("GET", url, api_key, header="X-Api-Key")
  if status >= 400:
    raise RuntimeError(f"GET /api/v1/queue returned HTTP {status}")
  if not body:
    return []
  try:
    return json.loads(body).get("records", [])
  except json.JSONDecodeError as exc:
    raise RuntimeError(f"GET /api/v1/queue returned malformed JSON: {exc}") from exc


def bulk_delete_lidarr(host: str, api_key: str, ids: list[int]) -> bool:
  """DELETE /api/v1/queue/bulk with the graceful teardown params.

  removeFromClient cancels the slskd transfer via Tubifarry; blocklist marks
  the dead release (album stays monitored); skipRedownload suppresses auto
  re-search. Returns True on 200/204.
  """
  params = urllib.parse.urlencode(
    {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"}
  )
  url = f"{host}/api/v1/queue/bulk?{params}"
  payload = json.dumps({"ids": ids}).encode()
  status, _ = _request("DELETE", url, api_key, header="X-Api-Key", data=payload)
  return status in (200, 204)


def delete_lidarr_item(host: str, api_key: str, queue_id: int) -> bool:
  params = urllib.parse.urlencode(
    {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"}
  )
  url = f"{host}/api/v1/queue/{queue_id}?{params}"
  status, _ = _request("DELETE", url, api_key, header="X-Api-Key")
  return status in (200, 204)


def fetch_slskd_downloads(host: str, api_key: str) -> list[dict]:
  status, body = _request("GET", f"{host}/api/v0/transfers/downloads", api_key)
  if status >= 400:
    raise RuntimeError(f"GET /api/v0/transfers/downloads returned HTTP {status}")
  if not body:
    return []
  try:
    return json.loads(body)
  except json.JSONDecodeError as exc:
    raise RuntimeError(
      f"GET /api/v0/transfers/downloads returned malformed JSON: {exc}"
    ) from exc


def cancel_slskd_transfer(host: str, api_key: str, t: SlskdTransfer) -> bool:
  user = urllib.parse.quote(t.username, safe="")
  url = f"{host}/api/v0/transfers/downloads/{user}/{t.transfer_id}?remove=true"
  status, _ = _request("DELETE", url, api_key)
  return status in (200, 204, 404)  # 404 == already gone


def clear_slskd_completed(host: str, api_key: str) -> bool:
  """Bulk-clear all terminal slskd download records.

  Returns True on 200/204. Returns False if the endpoint is unavailable (404 /
  405 / >=400) so main() can fall back to per-transfer cleanup.
  """
  url = f"{host}/api/v0/transfers/downloads/all/completed"
  status, _ = _request("DELETE", url, api_key)
  return status in (200, 204)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Clean-slate the slskd<->Lidarr pipeline (nuke queue + transfers + folder)."
  )
  parser.add_argument("--dry-run", action="store_true", help="Report the plan and exit 0.")
  parser.add_argument("--skip-lidarr", action="store_true", help="Skip Phase 1 (Lidarr queue).")
  parser.add_argument("--skip-slskd", action="store_true", help="Skip Phase 2 (slskd wipe).")
  parser.add_argument(
    "--skip-folder-sweep", action="store_true", help="Skip Phase 3 (completed-folder sweep)."
  )
  parser.add_argument(
    "--slskd-complete-dir", type=Path, default=None,
    help=f"Override SLSKD_COMPLETE_DIR (env or {DEFAULT_SLSKD_COMPLETE_DIR}).",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  lidarr_host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  lidarr_key = os.environ.get("API_KEY_LIDARR")
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  slskd_key = os.environ.get("API_KEY_SLSKD")
  if not lidarr_key:
    print("ERROR: API_KEY_LIDARR not set (check .env)", file=sys.stderr)
    return 2
  if not slskd_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2

  complete_dir = args.slskd_complete_dir or Path(
    os.environ.get("SLSKD_COMPLETE_DIR", DEFAULT_SLSKD_COMPLETE_DIR)
  )

  print("=== slskd<->Lidarr CLEAN SLATE ===" + ("  [DRY RUN]" if args.dry_run else ""))
  failures = 0

  # --- Phase 1: Lidarr queue teardown (graceful, first) ---
  if not args.skip_lidarr:
    try:
      records = fetch_lidarr_queue(lidarr_host, lidarr_key)
    except (urllib.error.URLError, RuntimeError) as exc:
      print(f"ERROR: cannot reach Lidarr: {exc}", file=sys.stderr)
      return 2
    ids = plan_lidarr_nuke(records)
    print(f"Phase 1 (Lidarr): {len(ids)} queue row(s) -> remove+blocklist+skipRedownload")
    if ids and not args.dry_run:
      if not bulk_delete_lidarr(lidarr_host, lidarr_key, ids):
        # Fall back to per-id deletes if the bulk endpoint failed.
        ok = 0
        for qid in ids:
          if delete_lidarr_item(lidarr_host, lidarr_key, qid):
            ok += 1
          else:
            failures += 1
            print(f"WARNING: failed to delete Lidarr queue/{qid}", file=sys.stderr)
        print(f"  deleted {ok}/{len(ids)} row(s) (per-id fallback)")
      else:
        print(f"  deleted {len(ids)} row(s) (bulk)")

  # --- Phase 2: slskd full wipe (mop-up) ---
  if not args.skip_slskd:
    try:
      downloads = fetch_slskd_downloads(slskd_host, slskd_key)
    except (urllib.error.URLError, RuntimeError) as exc:
      print(f"ERROR: cannot reach slskd: {exc}", file=sys.stderr)
      return 2
    active, terminal = collect_slskd_transfers(downloads)
    print(f"Phase 2 (slskd): cancel {len(active)} active transfer(s), clear {terminal} record(s)")
    if not args.dry_run:
      cancelled = 0
      for t in active:
        if cancel_slskd_transfer(slskd_host, slskd_key, t):
          cancelled += 1
        else:
          failures += 1
          print(f"WARNING: failed to cancel slskd {t.username}/{t.transfer_id}", file=sys.stderr)
      if active:
        print(f"  cancelled {cancelled}/{len(active)} active transfer(s)")
      if not clear_slskd_completed(slskd_host, slskd_key):
        # Endpoint unavailable: re-fetch and remove terminal records per-transfer.
        try:
          leftovers = fetch_slskd_downloads(slskd_host, slskd_key)
        except (urllib.error.URLError, RuntimeError) as exc:
          print(f"WARNING: per-transfer fallback re-fetch failed: {exc}", file=sys.stderr)
          leftovers = []
          failures += 1
        for user in leftovers if isinstance(leftovers, list) else []:
          for directory in user.get("directories", []):
            for file in directory.get("files", []):
              if str(file.get("state", "")).startswith(TERMINAL_PREFIX):
                t = SlskdTransfer(user.get("username", ""), file.get("id", ""), file.get("state", ""))
                if not cancel_slskd_transfer(slskd_host, slskd_key, t):
                  failures += 1
        print("  cleared terminal records (per-transfer fallback)")
      else:
        print("  cleared all terminal records (bulk)")

  # --- Phase 3: completed-folder sweep (disk, last) ---
  if not args.skip_folder_sweep:
    if not complete_dir.is_dir():
      print(f"Phase 3 (folder): {complete_dir} not found — skipping", file=sys.stderr)
    else:
      spare: set[str] = set()
      if not args.skip_lidarr:
        try:
          spare = spare_basenames(fetch_lidarr_queue(lidarr_host, lidarr_key))
        except (urllib.error.URLError, RuntimeError):
          spare = set()  # queue already drained / unreachable -> nothing to spare
      try:
        targets = plan_folder_sweep(complete_dir, spare)
      except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
      bytes_to_free = 0
      for d in targets:
        for dp, _dirs, files in os.walk(d):
          for f in files:
            try:
              bytes_to_free += os.path.getsize(os.path.join(dp, f))
            except OSError:
              continue
      print(
        f"Phase 3 (folder): delete {len(targets)} dir(s) (~{bytes_to_free / 1e9:.2f} GB), "
        f"sparing {len(spare)} active import(s)"
      )
      if args.dry_run:
        for d in targets[:15]:
          print(f"  DRY rmtree {d.name}")
        if len(targets) > 15:
          print(f"  ... and {len(targets) - 15} more")
      else:
        for d in targets:
          try:
            shutil.rmtree(d)
          except OSError as exc:
            print(f"WARNING: rmtree {d}: {exc}", file=sys.stderr)
            failures += 1
        print(f"  deleted {len(targets)} dir(s) (~{bytes_to_free / 1e9:.2f} GB if all succeeded)")

  if args.dry_run:
    return 0
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
