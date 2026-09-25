#!/usr/bin/env python3
"""Push AudioMuse-AI's configuration from .env into its database (ADR-0053).

AudioMuse reads only Postgres + TZ from the environment. Everything else --
the Navidrome connection, its own admin account, the API token the Navidrome
plugin authenticates with, and the analysis knobs -- lives in its `app_config`
table and is normally typed into a browser Setup Wizard. This drives the same
endpoint (`POST /api/setup`) so a fresh `${CONFIG_DIRECTORY}/audiomuse-db` can
be brought back to the same state from the repo, not from memory.

Idempotent. Secrets left blank on a re-run keep their stored value (that is
the endpoint's own contract), and the admin account is only created when none
exists -- AudioMuse refuses to overwrite one through setup.

A save restarts AudioMuse's Flask process and its workers. Do not run this in
the middle of an analysis you care about; it re-queues, it does not resume.

Exit codes
----------
  0  configuration and cron schedule saved (or --dry-run printed them)
  2  unreachable, rejected, or a required env var is unset
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HOST = "http://localhost:8010"
# Container name on nas-network. AudioMuse streams every track from here, so
# it must never go through SWAG: that is the whole library over the WAN hop.
NAVIDROME_URL = "http://navidrome:4533"

# Analysis knobs, each one a measured decision in ADR-0053. Kept here rather
# than as compose env vars because past first boot AudioMuse ignores those.
TUNING = {
  # Whisper-small ASR over 169k tracks is the one stage that turns a
  # days-long first pass into a months-long one. Lyrics still come from the
  # LRCLIB slot below; tracks it misses get the instrumental sentinel.
  "LYRICS_ASR_ENABLE": False,
  "LYRICS_API_ENABLE": True,
  # LRCLIB, the same source nd-lyrics tries first. A BARE URL: AudioMuse
  # appends ?artist_name=&track_name= itself from the slot's param fields
  # (whose defaults are already LRCLIB's names). A template may only use the
  # literal {artist}/{title} -- `{artist_param}` raised KeyError('artist_param')
  # on every track, logged as a lyrics MISS, not an error. Measured.
  "LYRICS_API_1_URL_TEMPLATE": "https://lrclib.net/api/get",
  # 0 = do not ask Navidrome for lyrics during analysis. Navidrome answers a
  # cache miss by walking nd-lyrics' whole provider list (five sites), which
  # measured ~3 s of pure wait per track -- ~30% of the bulk pass -- and ~5
  # third-party requests per track across 169k tracks. The LRCLIB slot above
  # is one request, to the provider nd-lyrics would have hit first anyway.
  "MUSICSERVER_LYRICS_TIMEOUT": 0,
  # Default true reloads MusiCNN and CLAP for EVERY track (a GPU-VRAM guard;
  # this box has no GPU in the container). ADR-0053 has the before/after.
  "PER_SONG_MODEL_RELOAD": False,
}

# Scheduled tasks, upserted by task_type (the endpoint's own semantics).
# Nightly analysis picks up whatever Lidarr imported since the last run;
# it skips analysed tracks, and the one-live-main-task gate makes a tick
# that lands on a running pass a no-op rather than a second pass. 03:30 is
# clear of the 04:xx backup/diun window.
CRON = [
  {"name": "Nightly analysis", "task_type": "analysis", "cron_expr": "30 3 * * *", "enabled": True},
]

REQUIRED_ENV = (
  "NAVIDROME_AUDIOMUSE_USER", "NAVIDROME_AUDIOMUSE_PASSWORD",
  "AUDIOMUSE_ADMIN_USER", "AUDIOMUSE_ADMIN_PASSWORD", "AUDIOMUSE_API_TOKEN",
)


def _request(url: str, payload: dict | None = None, token: str = "") -> tuple[int, dict]:
  """(status, body). The bearer token is admin-equivalent once auth is on;
  before the first save the wizard is open and ignores it."""
  data = json.dumps(payload).encode() if payload is not None else None
  headers = {"Content-Type": "application/json"}
  if token:
    headers["Authorization"] = f"Bearer {token}"
  req = urllib.request.Request(url, data=data, headers=headers)
  try:
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - localhost
      return resp.status, json.loads(resp.read() or b"{}")
  except urllib.error.HTTPError as exc:
    try:
      return exc.code, json.loads(exc.read() or b"{}")
    except json.JSONDecodeError:
      return exc.code, {}


def build_config(env: dict[str, str], has_admin: bool) -> dict:
  """The /api/setup `config` body. Pure, for testing."""
  cfg = {
    "MEDIASERVER_TYPE": "navidrome",
    "NAVIDROME_URL": NAVIDROME_URL,
    "NAVIDROME_USER": env["NAVIDROME_AUDIOMUSE_USER"],
    "NAVIDROME_PASSWORD": env["NAVIDROME_AUDIOMUSE_PASSWORD"],
    "AUTH_ENABLED": True,
    "API_TOKEN": env["AUDIOMUSE_API_TOKEN"],
    **TUNING,
  }
  if not has_admin:
    cfg["AUDIOMUSE_USER"] = env["AUDIOMUSE_ADMIN_USER"]
    cfg["AUDIOMUSE_PASSWORD"] = env["AUDIOMUSE_ADMIN_PASSWORD"]
  return cfg


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
  ap.add_argument("--host", default=os.environ.get("AUDIOMUSE_HOST", DEFAULT_HOST))
  ap.add_argument("--dry-run", action="store_true", help="print the body, save nothing")
  args = ap.parse_args()

  missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
  if missing:
    print(f"FATAL: unset: {', '.join(missing)}", file=sys.stderr)
    return 2

  token = os.environ["AUDIOMUSE_API_TOKEN"]
  status, current = _request(f"{args.host}/api/setup", token=token)
  if status != 200:
    print(f"FATAL: GET /api/setup answered {status}", file=sys.stderr)
    return 2
  cfg = build_config(dict(os.environ), bool(current.get("has_admin_user")))

  if args.dry_run:
    shown = {k: ("***" if "PASSWORD" in k or "TOKEN" in k else v) for k, v in cfg.items()}
    print(json.dumps(shown, indent=2))
    return 0

  # Prove the Navidrome credentials first: a save with a bad password is
  # accepted and then fails every analysis batch instead.
  status, body = _request(
    f"{args.host}/api/setup",
    {"config": cfg, "test_connection": True, "navidrome_auth_mode": "password"},
    token,
  )
  if status != 200:
    print(f"FATAL: Navidrome connection test failed ({status}): {body}", file=sys.stderr)
    return 2
  print(f"navidrome connection ok (probe_count={body.get('probe_count')})")

  status, body = _request(
    f"{args.host}/api/setup", {"config": cfg, "navidrome_auth_mode": "password"}, token,
  )
  if status != 200:
    print(f"FATAL: save rejected ({status}): {body}", file=sys.stderr)
    return 2
  print(f"saved {len(body.get('saved_keys', []))} keys; status={body.get('status')}")
  rc = 0
  # The save restarts Flask; wait for it before touching /api/cron.
  for _ in range(60):
    if _request(f"{args.host}/api/health")[0] == 200:
      break
    time.sleep(3)
  for entry in CRON:
    status, _ = _request(f"{args.host}/api/cron", entry, token)
    print(f"cron {entry['task_type']} '{entry['cron_expr']}': {status}")
    if status != 200:
      rc = 2
  if body.get("status") != "ok":
    # "partial" is EVERY save here, and is not a failure: the ack budget is
    # shorter than a worker restart. Measured 2026-09-25: both workers logged
    # `Control request ... received: restart` and a fresh `starting` line
    # ~2 s after the save that answered "partial".
    print("note: worker restart not yet acknowledged (normal; they restart in ~2 s)")
  return rc


if __name__ == "__main__":
  sys.exit(main())
