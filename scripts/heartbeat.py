#!/usr/bin/env python3
"""Ping an off-box dead-man's switch, so a dead host is noticed by something else.

Why this exists
---------------
`stack_watchdog.py` and the ntfy instance it pushes to both run on this host.
That covers everything except the one failure that matters most: the host
itself. A powered-off box, a wedged kernel, a dead NIC or a stopped cron
daemon all produce exactly the same thing from the alerting stack — silence,
which is indistinguishable from "all fine".

The fix is inversion: instead of this box reporting problems, it reports that
it is alive, and something *elsewhere* raises the alarm when the reports stop.
That is a dead-man's switch, and healthchecks.io's free tier is one.

**A missed heartbeat cannot tell you what broke** — host down, network down,
cron down, or this script itself broken all look identical from the outside.
That is fine and it is the point: the external service's job is to notice
silence, and yours is to go and look. Everything that *can* be attributed is
already attributed by `stack_watchdog.py` on the inside.

This does slightly more than a bare `curl`: before reporting "alive" it checks
that the watchdog itself has run recently. A box that is up, networked, and
running cron while its own monitoring is dead would otherwise keep the
heartbeat green forever — the circular gap that made `autoheal` invisible for
a month. If the local check fails this pings the `/fail` endpoint instead, so
the external service alerts immediately rather than after the grace period.

Failure to ping is itself loud: a non-2xx response or a network error exits 2,
which `cron_job.py` turns into an ntfy push. A silently-failing heartbeat would
be worse than no heartbeat, because the dashboard elsewhere would stay green.

Exit codes
----------
  0  pinged "alive"
  1  a reported condition, not a script failure: either no URL is configured yet,
     or the local health check failed and /fail was pinged. `cron_job.py` treats
     1 as a successful run, so an unconfigured heartbeat nags once an hour from
     stack_watchdog rather than pushing every ten minutes.
  2  fatal — the ping could not be delivered at all

Environment
-----------
  NAS_HEARTBEAT_URL   base ping URL, e.g. https://hc-ping.com/<uuid>
                      `/fail` and `/start` are appended as needed, which is the
                      healthchecks.io convention that Cronitor and most others
                      also accept.

Usage
-----
  python scripts/heartbeat.py
  python scripts/heartbeat.py --watchdog-stale-min 20
  python scripts/heartbeat.py --dry-run
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

if "NAS_HEARTBEAT_URL" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CRON_STATE = REPO_ROOT / "logs" / "cron-state"
# The watchdog runs every 5 minutes; 20 means four consecutive misses before
# this stops vouching for the box.
DEFAULT_WATCHDOG_STALE_MIN = 20.0
PING_TIMEOUT = 20


def watchdog_age_minutes(state_dir: Path) -> float | None:
  """Minutes since stack_watchdog last completed. None if it never has."""
  try:
    state = json.loads((state_dir / "stack-watchdog.json").read_text())
    last = float(state["last_success"])
  except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
    return None
  return (time.time() - last) / 60.0


def ping(url: str, suffix: str = "", body: str = "") -> tuple[bool, str]:
  """Ping the dead-man's switch. Returns (delivered, detail)."""
  target = url.rstrip("/") + suffix
  data = body.encode("utf-8") if body else None
  req = urllib.request.Request(target, data=data, method="POST" if data else "GET")
  try:
    with urllib.request.urlopen(req, timeout=PING_TIMEOUT) as resp:  # noqa: S310 - operator URL
      return True, f"HTTP {resp.status}"
  except urllib.error.HTTPError as exc:
    return False, f"HTTP {exc.code} — is the ping URL right?"
  except OSError as exc:
    return False, f"{type(exc).__name__}: {exc}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Report this host as alive to an off-box dead-man's switch.",
  )
  parser.add_argument(
    "--cron-state-dir", type=Path, default=DEFAULT_CRON_STATE, help="cron_job.py state directory."
  )
  parser.add_argument(
    "--watchdog-stale-min",
    type=float,
    default=DEFAULT_WATCHDOG_STALE_MIN,
    help=(
      "Report /fail if stack_watchdog has not succeeded within this many minutes "
      f"(default {DEFAULT_WATCHDOG_STALE_MIN:.0f}). A live box with dead monitoring "
      "must not keep the heartbeat green."
    ),
  )
  parser.add_argument("--dry-run", action="store_true", help="Decide, print, ping nothing.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  url = os.getenv("NAS_HEARTBEAT_URL", "").strip()
  if not url:
    print(
      "NAS_HEARTBEAT_URL not set — there is no off-box check on this host. "
      "Create a check at https://healthchecks.io and put its ping URL in .env; "
      "stack_watchdog raises this as an alert until it is set.",
      file=sys.stderr,
    )
    return 1

  age = watchdog_age_minutes(args.cron_state_dir)
  if age is None:
    healthy, reason = False, "stack_watchdog has never recorded a successful run"
  elif age > args.watchdog_stale_min:
    healthy, reason = False, f"stack_watchdog last succeeded {age:.0f} min ago"
  else:
    healthy, reason = True, f"stack_watchdog succeeded {age:.1f} min ago"

  if args.dry_run:
    print(f"DRY-RUN would ping {'alive' if healthy else '/fail'}: {reason}")
    return 0 if healthy else 1

  if healthy:
    delivered, detail = ping(url)
  else:
    delivered, detail = ping(url, "/fail", reason)

  if not delivered:
    # Loud on purpose: a heartbeat that fails quietly leaves a green dashboard
    # somewhere else while this box is unmonitored.
    print(f"ERROR: heartbeat ping failed ({detail})", file=sys.stderr)
    return 2

  print(f"heartbeat {'alive' if healthy else 'FAIL'} delivered ({detail}): {reason}")
  return 0 if healthy else 1


if __name__ == "__main__":
  sys.exit(main())
