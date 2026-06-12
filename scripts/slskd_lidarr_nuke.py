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
import json  # noqa: F401
import os
import shutil  # noqa: F401
import sys
import urllib.error
import urllib.parse  # noqa: F401
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
  args = parse_args(argv)  # noqa: F841
  lidarr_host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")  # noqa: F841
  lidarr_key = os.environ.get("API_KEY_LIDARR")
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")  # noqa: F841
  slskd_key = os.environ.get("API_KEY_SLSKD")
  if not lidarr_key:
    print("ERROR: API_KEY_LIDARR not set (check .env)", file=sys.stderr)
    return 2
  if not slskd_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2
  # Phases wired in later tasks.
  return 0


if __name__ == "__main__":
  sys.exit(main())
