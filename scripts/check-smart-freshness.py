#!/usr/bin/env python3
"""Assert scrutiny's SMART collector is still reporting. Used by `make verify-runtime`.

A stale collector is a RUNTIME fact, not a config one, which is why this lives
here and not in scripts/check-invariants.sh: the compose model can be perfectly
correct while the collector has been silently failing for a week, and a
monitoring tool that has stopped monitoring is worse than none because the
dashboard still looks green.

One device is the CORRECT answer on this host. The 9.1 TB USB disk holding
${SHARE_DIRECTORY} passes no SMART under any `smartctl -d` type, so scrutiny
covers the NVMe only and is not coverage of the media disk -- that disk's
channels are stack_watchdog.py's kernel-log sweep and ext4 superblock counter.
See docs/decisions/0023-smart-monitoring.md.

Exit codes
----------
  0  every known device reported within --max-age-hours
  1  a device is stale, or scrutiny knows about no devices at all
  2  scrutiny's API is unreachable
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8086/api/summary"
DEFAULT_MAX_AGE_H = 24.0


def parse_summary(payload: str) -> list[tuple[str, str | None]]:
    """Extract [(device_name, collector_date)] from scrutiny's /api/summary.

    Pure. Raises ValueError on anything that is not that document.
    """
    try:
        doc = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"not JSON: {exc}") from exc
    summary = (doc.get("data") or {}).get("summary")
    if summary is None:
        raise ValueError("no data.summary in response")
    return [
        (
            (entry.get("device") or {}).get("device_name") or "?",
            (entry.get("smart") or {}).get("collector_date"),
        )
        for entry in summary.values()
    ]


def stale_devices(
    rows: list[tuple[str, str | None]],
    now: dt.datetime,
    max_age_h: float,
) -> list[str]:
    """Return a description for each device that has not reported recently. Pure."""
    stale: list[str] = []
    for name, collected in rows:
        if not collected:
            stale.append(f"{name}: never collected")
            continue
        try:
            seen = dt.datetime.fromisoformat(collected.replace("Z", "+00:00"))
        except ValueError:
            stale.append(f"{name}: unparseable collector_date {collected!r}")
            continue
        age_h = (now - seen).total_seconds() / 3600
        if age_h > max_age_h:
            stale.append(f"{name}: last collected {age_h:.1f}h ago")
    return stale


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_H)
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with urllib.request.urlopen(args.url, timeout=10) as resp:  # noqa: S310
            rows = parse_summary(resp.read().decode("utf-8", "replace"))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"    !!! scrutiny API unreachable at {args.url}: {exc}", file=sys.stderr)
        return 2

    if not rows:
        print(
            "    !!! scrutiny knows about NO devices. Its collector is finding "
            "nothing -- check that /dev/nvme0 (the CONTROLLER char device, not "
            "the nvme0n1 namespace) is still passed through; `smartctl --scan` "
            "returns empty for a namespace and the UI looks fine either way.",
            file=sys.stderr,
        )
        return 1

    stale = stale_devices(rows, dt.datetime.now(dt.UTC), args.max_age_hours)
    if stale:
        print(f"    !!! stale SMART data: {'; '.join(stale)}", file=sys.stderr)
        return 1

    print(f"    ok: {len(rows)} device(s), all collected <{args.max_age_hours:g}h ago")
    print("    note: 1 device is CORRECT -- the 9.1T USB media disk answers no")
    print("          SMART at all, so this is not coverage of it. ADR-0023.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
