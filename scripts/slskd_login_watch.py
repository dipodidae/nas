#!/usr/bin/env python3
"""Alert when slskd is logged out of Soulseek — WITHOUT restarting it.

Why this exists
---------------
slskd's web server can be up while its Soulseek login is dead. It is tempting
to wire that into the container healthcheck + autoheal so a logged-out slskd
gets restarted — but that is actively harmful. The login handshake times out
after a hardcoded 5000ms when slsknet's central server still holds a stale
session for the username (the classic "ghost session" after a fast restart or
a VPN blip). Restarting re-presents the same username immediately and
re-collides with the ghost session, perpetuating the 32->64->128s backoff
spiral. The only cure is to leave slskd DOWN for 15-30 min so slsknet reaps
its own stale session, then cold-start.

So the container healthcheck is deliberately liveness-only (web UI spider) and
autoheal never restarts slskd for a login drop. This script is the separate,
*alert-only* path: it polls slskd's login state and reports (and optionally
pushes a webhook) when slskd has been logged out longer than a grace period —
so a dropped login announces itself instead of being discovered after a batch
of dead searches. It NEVER restarts or stops slskd.

State is tracked across runs in a small JSON file so the script can tell
"logged out for 2 minutes (probably a normal reconnect)" from "logged out for
40 minutes (needs the stop-wait-coldstart cure)".

Exit codes
----------
  0  logged in, or logged out but still within the grace period
  1  logged out longer than the grace period (alert raised)
  2  fatal (config missing, slskd API unreachable/unparseable)

Environment
-----------
  API_KEY_SLSKD          (required) slskd administrator API key
  SLSKD_HOST             (default: http://localhost:5030)
  SLSKD_ALERT_WEBHOOK    (optional) URL to POST a plain-text alert to
                         (e.g. an ntfy topic: https://ntfy.sh/my-slskd)

Usage
-----
  python scripts/slskd_login_watch.py                 # check, print status
  python scripts/slskd_login_watch.py --grace-min 15  # alert after 15 min out
  python scripts/slskd_login_watch.py --state logs/slskd_login_watch.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if "API_KEY_SLSKD" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_SLSKD_HOST = "http://localhost:5030"
DEFAULT_GRACE_MINUTES = 15.0


@dataclass(frozen=True)
class LoginState:
  logged_in: bool
  connected: bool
  raw_state: str


def fetch_login_state(host: str, api_key: str) -> LoginState | None:
  """Query slskd's server endpoint. Returns None on network/parse error."""
  url = f"{host}/api/v0/server"
  req = urllib.request.Request(url, headers={"X-API-Key": api_key})
  try:
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - localhost
      data = json.loads(resp.read())
  except (OSError, json.JSONDecodeError):
    # OSError covers urllib.error.URLError, TimeoutError, and a bare
    # ConnectionResetError when slskd is down but the port is still published.
    return None
  return LoginState(
    logged_in=bool(data.get("isLoggedIn")),
    connected=bool(data.get("isConnected")),
    raw_state=str(data.get("state", "")),
  )


def load_since(state_path: Path | None) -> float | None:
  """Return the epoch when slskd was first seen logged out, or None."""
  if state_path is None or not state_path.exists():
    return None
  try:
    return float(json.loads(state_path.read_text()).get("logged_out_since"))
  except (json.JSONDecodeError, TypeError, ValueError, OSError):
    return None


def save_since(state_path: Path | None, since: float | None) -> None:
  """Persist the logged-out-since epoch (or clear it when logged in)."""
  if state_path is None:
    return
  try:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"logged_out_since": since}))
  except OSError as exc:
    print(f"WARNING: could not write state file {state_path}: {exc}", file=sys.stderr)


def post_webhook(url: str, message: str) -> None:
  """Best-effort plain-text webhook POST (e.g. ntfy). Never raises."""
  try:
    req = urllib.request.Request(url, data=message.encode("utf-8"), method="POST")
    urllib.request.urlopen(req, timeout=10).close()  # noqa: S310 - user-provided
  except (OSError, ValueError) as exc:
    print(f"WARNING: webhook POST to {url} failed: {exc}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Alert (never restart) when slskd is logged out of Soulseek."
  )
  parser.add_argument(
    "--grace-min",
    type=float,
    default=DEFAULT_GRACE_MINUTES,
    help=(
      "Minutes slskd may be logged out before this raises an alert / exits 1 "
      f"(default {DEFAULT_GRACE_MINUTES}). Rides out normal reconnect blips."
    ),
  )
  parser.add_argument(
    "--state",
    type=Path,
    default=None,
    help="JSON file tracking how long slskd has been logged out across runs.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  api_key = os.environ.get("API_KEY_SLSKD")
  if not api_key:
    print("ERROR: API_KEY_SLSKD not set", file=sys.stderr)
    return 2

  state = fetch_login_state(host, api_key)
  if state is None:
    print(f"ERROR: could not read slskd login state from {host}/api/v0/server", file=sys.stderr)
    return 2

  now = time.time()

  if state.logged_in:
    save_since(args.state, None)
    print(f"OK: slskd logged in (state={state.raw_state})")
    return 0

  # Logged out — figure out for how long.
  since = load_since(args.state)
  if since is None:
    since = now
    save_since(args.state, since)
  out_minutes = (now - since) / 60.0

  if out_minutes < args.grace_min:
    print(
      f"NOTICE: slskd logged out for {out_minutes:.1f} min "
      f"(connected={state.connected}, state={state.raw_state}); "
      f"within {args.grace_min:.0f} min grace — likely a normal reconnect"
    )
    return 0

  message = (
    f"slskd logged out of Soulseek for {out_minutes:.0f} min "
    f"(state={state.raw_state}). Likely a stale slsknet session — the cure is "
    f"to STOP slskd and leave it down 15-30 min, then cold-start. Do NOT just "
    f"restart (it re-collides with the ghost session)."
  )
  print(f"ALERT: {message}", file=sys.stderr)
  webhook = os.environ.get("SLSKD_ALERT_WEBHOOK")
  if webhook:
    post_webhook(webhook, message)
  return 1


if __name__ == "__main__":
  sys.exit(main())
