#!/usr/bin/env python3
"""Trigger a slskd shared-library rescan.

slskd scans `SLSKD_SHARED_DIR` (`/music` in this stack) once at startup. If the
directory was empty or unmounted at that moment, slskd advertises zero files to
Soulseek peers — which on Soulseek means aggressive rate-limiting and
`Transfer rejected: Overwhelmed with requests` responses from busy peers.

This script PUT-s `/api/v0/shares` to force a fresh scan, optionally waits for
completion, and reports the resulting directory/file counts.

Exit codes:
  0 success (scan triggered, or finished with non-empty share when --wait)
  1 partial (scan completed but shares are empty — likely a perms or mount issue)
  2 fatal (config missing, slskd unreachable, or HTTP error)

Environment:
  API_KEY_SLSKD   (required) administrator key for /api/v0
  SLSKD_HOST      (default: http://localhost:5030)

Usage:
  python scripts/slskd_rescan.py              # fire and forget
  python scripts/slskd_rescan.py --wait       # block until scan finishes
  python scripts/slskd_rescan.py --dry-run    # print what would happen
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

if "API_KEY_SLSKD" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_HOST = "http://localhost:5030"
WAIT_TIMEOUT_SECONDS = 600
WAIT_POLL_SECONDS = 5


def _request(method: str, url: str, api_key: str, timeout: int = 10) -> tuple[int, bytes]:
  req = urllib.request.Request(url, method=method, headers={"X-API-Key": api_key})
  with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
    return resp.status, resp.read()


def trigger_rescan(host: str, api_key: str) -> None:
  status, _ = _request("PUT", f"{host}/api/v0/shares", api_key)
  if status >= 400:
    raise RuntimeError(f"PUT /api/v0/shares returned HTTP {status}")


def get_share_state(host: str, api_key: str) -> dict:
  status, body = _request("GET", f"{host}/api/v0/application", api_key)
  if status >= 400:
    raise RuntimeError(f"GET /api/v0/application returned HTTP {status}")
  return json.loads(body).get("shares", {})


def wait_for_scan(host: str, api_key: str, timeout: int) -> dict:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    state = get_share_state(host, api_key)
    if not state.get("scanning", False) and state.get("scanProgress", 0) >= 1:
      return state
    time.sleep(WAIT_POLL_SECONDS)
  raise TimeoutError(f"slskd scan did not finish within {timeout}s")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Trigger a slskd shared-library rescan.")
  parser.add_argument("--wait", action="store_true", help="Block until the scan finishes.")
  parser.add_argument(
    "--timeout",
    type=int,
    default=WAIT_TIMEOUT_SECONDS,
    help=f"Seconds to wait when --wait is given (default {WAIT_TIMEOUT_SECONDS}).",
  )
  parser.add_argument("--dry-run", action="store_true", help="Print intended action and exit 0.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("SLSKD_HOST", DEFAULT_HOST).rstrip("/")
  api_key = os.environ.get("API_KEY_SLSKD")
  if not api_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2

  if args.dry_run:
    print(f"DRY-RUN: would PUT {host}/api/v0/shares")
    if args.wait:
      print(f"DRY-RUN: would then poll up to {args.timeout}s for scan completion")
    return 0

  try:
    trigger_rescan(host, api_key)
  except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
    print(f"ERROR: failed to trigger slskd rescan: {exc}", file=sys.stderr)
    return 2

  print(f"slskd rescan triggered at {host}")
  if not args.wait:
    return 0

  try:
    state = wait_for_scan(host, api_key, args.timeout)
  except (urllib.error.URLError, RuntimeError, TimeoutError) as exc:
    print(f"ERROR: scan polling failed: {exc}", file=sys.stderr)
    return 2

  dirs = state.get("directories", 0)
  files = state.get("files", 0)
  faulted = state.get("faulted", False)
  print(f"scan done: directories={dirs} files={files} faulted={faulted}")

  if faulted:
    print("ERROR: scan reported faulted state", file=sys.stderr)
    return 2
  if files == 0:
    print(
      "WARNING: scan finished but shares are empty — check SLSKD_SHARED_DIR mount and perms",
      file=sys.stderr,
    )
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
