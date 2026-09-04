#!/usr/bin/env python3
"""Pin slskd's EFFECTIVE config, including the values nobody configured.

Used by `make verify-runtime`. This is a RUNTIME fact and cannot live in
`scripts/check-invariants.sh`: `.docker-config/slskd/slskd.yml` is gitignored,
and -- more importantly -- the values that matter most are not in it at all.

Why it exists
-------------
Two findings on 2026-09-04, both from reading `GET /api/v0/options` rather than
the file on disk:

1. **`transfers.download.retry` is load-bearing and nobody set it.** slskd
   supplies `attempts=3, delay=5000, maxDelay=60000, partial=resume` as
   built-in defaults; `slskd.yml` has no `retry:` block. Every measurement of
   `lidarr_stuck_download_reaper.py` -- the 13-marked-failed-in-2-days baseline
   and any tuning done against it -- silently assumes them. Defaults change on
   upgrade, and that change would look like the reaper getting worse.

2. **`retention.files.*` is configured and inert.** 75 dirs in
   `/downloads/complete/slskd` exceeded the 14-day `files.complete` threshold
   (oldest 23.8 d), 1299 incomplete dirs exceeded the 30-day one (oldest
   77.4 d), and the container log contains zero occurrences of "retention"
   across a full startup. `retention.transfers.*` demonstrably DOES work.
   Pinning the block means the day the file half starts working -- an upgrade,
   an upstream fix -- we find out from this assertion rather than from disk
   usage quietly dropping. That is the same tripwire pattern as the Lidarr
   mapFrom/mapTo check.

Neither value is asserted because it is right. They are asserted because they
are *assumed*, and an assumption nothing checks is how this stack loses data.

Exit codes
----------
  0  effective config matches every pinned value
  1  drift: a pinned value changed (the reaper baseline is no longer comparable)
  2  slskd unreachable, or API_KEY_SLSKD unset
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_SLSKD_HOST = "http://localhost:5030"

# Effective values observed 2026-09-04 against slskd 0.26.0.0
# (sha256:ecd4026d4f8fb504e2cc55323efa2c1f5b56d20d3686b018249cc36b48ea17a6).
# Dotted path -> expected value. Paths use the API's camelCase spelling.
PINNED: dict[str, object] = {
  # slskd's own defaults. Absent from slskd.yml; assumed by the stuck reaper.
  "transfers.download.retry.attempts": 3,
  "transfers.download.retry.delay": 5000,
  "transfers.download.retry.maxDelay": 60000,
  "transfers.download.retry.partial": "resume",
  # Ours, and working: the transfer-record half of retention.
  "retention.transfers.download.succeeded": 1440,
  "retention.transfers.download.errored": 10080,
  "retention.transfers.download.cancelled": 60,
  "retention.transfers.download.failed": 10080,
  # Ours, and INERT: the file half. See the docstring.
  "retention.files.complete": 20160,
  "retention.files.incomplete": 43200,
  # Set deliberately so a Lidarr import can never fail on a permission surprise.
  "transfers.download.destination.permissions.mode": "644",
  # Disk, not memory: a memory cache was rescanned at every start, a 45-min
  # window during which slskd does not bind :5030 at all (autoheal loop,
  # 2026-09-02).
  "shares.cache.storageMode": "disk",
}


def fetch_options(host: str, api_key: str) -> dict | None:
  """slskd's effective options, or None if it is unreachable."""
  req = urllib.request.Request(
    f"{host.rstrip('/')}/api/v0/options",
    headers={"X-API-Key": api_key},
  )
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def dig(options: dict, dotted: str) -> object:
  """Value at a dotted path, or the sentinel string `<absent>`."""
  node: object = options
  for part in dotted.split("."):
    if not isinstance(node, dict) or part not in node:
      return "<absent>"
    node = node[part]
  return node


def drifted(options: dict, pinned: dict[str, object]) -> list[tuple[str, object, object]]:
  """(path, expected, actual) for every pin that no longer matches."""
  out: list[tuple[str, object, object]] = []
  for path, expected in pinned.items():
    actual = dig(options, path)
    if actual != expected:
      out.append((path, expected, actual))
  return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--host", default=os.getenv("SLSKD_HOST", DEFAULT_SLSKD_HOST))
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  api_key = os.getenv("API_KEY_SLSKD")
  if not api_key:
    print("    !!! API_KEY_SLSKD is not set", file=sys.stderr)
    return 2

  options = fetch_options(args.host, api_key)
  if options is None:
    print(f"    !!! slskd unreachable at {args.host}", file=sys.stderr)
    return 2

  bad = drifted(options, PINNED)
  if bad:
    print(
      f"    !!! {len(bad)} pinned slskd value(s) drifted. These are ASSUMED by\n"
      "        lidarr_stuck_download_reaper.py's tuning and by the retention\n"
      "        findings in docs/music-pipeline-integration.md:",
      file=sys.stderr,
    )
    for path, expected, actual in bad:
      print(f"        {path}: expected {expected!r}, got {actual!r}", file=sys.stderr)
    return 1

  print(f"    ok: {len(PINNED)} effective slskd values match (incl. 4 upstream defaults)")
  return 0


if __name__ == "__main__":
  sys.exit(main())
