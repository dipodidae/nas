#!/usr/bin/env python3
"""Enforce qBittorrent Auto Torrent Management so categories drive save paths.

Background
----------
The *arr apps tag torrents with categories (arr-sonarr / arr-radarr) whose save
paths are correct (/downloads/complete/{sonarr,radarr}), but qBittorrent's Auto
Torrent Management (TMM) is OFF — so the category never drives the save path and
every torrent lands in the global default /downloads/complete/manual. This
script turns TMM on and flips existing torrents to auto-managed so qBittorrent
relocates each into its category folder (an instant same-filesystem rename;
hardlinks into the library are preserved). It also points qBittorrent's temp
(incomplete) path at /downloads/incomplete/qbittorrent so it stops sharing one
flat incomplete dir with slskd.

Idempotent: a run with TMM already on and all torrents managed is a no-op.

Exit codes
----------
  0 success (or dry-run / nothing to change)
  1 partial (some API calls failed; details on stderr)
  2 fatal (config missing, qBittorrent unreachable, auth failed)

Environment
-----------
  QBITTORRENT_USER   (required) WebUI username
  QBITTORRENT_PASS   (required) WebUI password
  QBITTORRENT_HOST   (default: http://localhost:8080)

Usage
-----
  python scripts/qbittorrent_settings_enforce.py            # ACT
  python scripts/qbittorrent_settings_enforce.py --dry-run  # preview, exit 0
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

if "QBITTORRENT_USER" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_QBT_HOST = "http://localhost:8080"
# 15 Mbps, in bytes/s. The upload cap was 4194304 (33.55 Mbps) -- **108% of
# this connection's entire measured upstream of ~31 Mbps**, i.e. effectively
# uncapped. With 50 upload slots and no headroom, BitTorrent kept the uplink
# queue permanently full: measured 5% packet loss, 127ms latency spikes and
# 25ms jitter to 1.1.1.1, versus 0% / 18ms / 1.8ms with it throttled. That
# collapses TCP throughput for anything else sharing the link -- which is why
# remote Jellyfin playback stuttered while LAN playback was fine. See
# docs/jellyfin-playback-audit.md.
#
# If the connection ever changes, re-measure before changing this: throttle
# qBittorrent, then watch /sys/class/net/<wan>/statistics/tx_bytes during a
# multi-stream upload. Keep the cap near half of what you measure.
# Two caps, because one number cannot do two jobs.
#
# SHAPED: what qBittorrent may use while scripts/wan_shaper.sh is in place. Its
# egress is marked DSCP CS1, so CAKE's Bulk tin makes it collapse to 1.76 Mbps
# (the 6.25% threshold) the instant any other flow appears. Yielding is
# automatic, so this is a pure capacity number, not a reservation.
UPLOAD_LIMIT_BYTES_PER_SEC = 3_124_224  # 3051 KiB/s = 25 Mbps
#
# DEGRADED: what it may use when the shaper is GONE. `tc` state is lost on
# link-down, and nothing then stands between BitTorrent and the modem queue.
# 25 Mbps unshaped would land in the same range as the original defect -- the
# 33.55 Mbps cap produced 23.4 Mbps of real upload and 5% packet loss. 15 Mbps
# unshaped was measured at 16.1 Mbps upload with 0% loss and 37 ms max RTT, so
# it is a validated fallback rather than a guess.
UPLOAD_LIMIT_DEGRADED_BYTES_PER_SEC = 1_874_944  # 1831 KiB/s = 15 Mbps

# 50 upload slots on a ~31 Mbps uplink is ~0.6 Mbps each: the queue depth
# problem is the *number of concurrent flows* competing for one bottleneck, not
# only the aggregate rate. 6 slots is ~4 Mbps each and leaves a far shallower
# queue for CAKE to manage.
MAX_UPLOAD_SLOTS = 6

WAN_IF = "enp88s0"

BASE_PREFS = {
  "auto_tmm_enabled": True,
  "category_changed_tmm_enabled": True,
  "save_path_changed_tmm_enabled": True,
  "temp_path_enabled": True,
  "temp_path": "/downloads/incomplete/qbittorrent",
  "max_uploads": MAX_UPLOAD_SLOTS,
}


def shaper_present(wan_if: str = WAN_IF) -> bool:
  """True when the CAKE egress shaper is installed. Reading tc needs no root."""
  try:
    out = subprocess.run(
      ["tc", "qdisc", "show", "dev", wan_if], capture_output=True, text=True, timeout=15, check=False
    )
  except (OSError, subprocess.TimeoutExpired):
    return False
  return out.returncode == 0 and "cake" in out.stdout


def desired_prefs(shaped: bool) -> dict:
  """Settings to enforce, with the upload cap chosen by the shaper's state."""
  return {
    **BASE_PREFS,
    "up_limit": UPLOAD_LIMIT_BYTES_PER_SEC if shaped else UPLOAD_LIMIT_DEGRADED_BYTES_PER_SEC,
  }


def plan_pref_changes(current: dict, desired: dict) -> dict:
  """Return the subset of ``desired`` whose value differs from ``current``.

  Pure. Keys absent from ``current`` count as differing (will be set).
  """
  return {k: v for k, v in desired.items() if current.get(k) != v}


def describe_drift(current: dict, changes: dict) -> str:
  """Render what each drifted setting was found at, not just what it will become.

  The upload cap reverted once with the repo pin intact and the cause was never
  established -- the container had not restarted in the window and alt-speed was
  off. Logging the *observed* value timestamps the next occurrence and says what
  it reverted to, which is the difference between diagnosing it and guessing.
  """
  return ", ".join(f"{k}: found {current.get(k)!r} -> setting {v!r}" for k, v in sorted(changes.items()))


def collect_unmanaged_hashes(torrents: list[dict]) -> list[str]:
  """Hashes of torrents not already auto-managed (TMM off / missing).

  Pure over ``GET /api/v2/torrents/info``. Order preserved.
  """
  return [t["hash"] for t in torrents if t.get("hash") and not t.get("auto_tmm", False)]


def summarize_targets(torrents: list[dict], categories: dict) -> dict[str, int]:
  """Count where each torrent will land once auto-managed.

  Pure. A torrent's target is its category's ``savePath``; an empty/missing
  category or empty savePath falls back to the qBittorrent default save path
  (reported as the literal ``"(default save path)"``).
  """
  out: dict[str, int] = {}
  for t in torrents:
    cat = t.get("category") or ""
    save = categories.get(cat, {}).get("savePath") or ""
    key = save if save else "(default save path)"
    out[key] = out.get(key, 0) + 1
  return out


class QbtClient:
  """Minimal qBittorrent WebUI API v2 client (cookie-jar session)."""

  def __init__(self, host: str):
    self.host = host
    self._opener = urllib.request.build_opener(
      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

  def _post(self, path: str, data: dict, timeout: int = 30) -> tuple[int, bytes]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
      f"{self.host}{path}", data=body, headers={"Referer": self.host}
    )
    try:
      with self._opener.open(req, timeout=timeout) as resp:  # noqa: S310 - localhost
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
      return exc.code, exc.read()

  def _get_json(self, path: str, timeout: int = 30):
    req = urllib.request.Request(f"{self.host}{path}", headers={"Referer": self.host})
    with self._opener.open(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read())

  def login(self, user: str, pw: str) -> bool:
    status, body = self._post("/api/v2/auth/login", {"username": user, "password": pw})
    # Success is 200 + "Ok." (older qBit) or 204 No Content (v5.x); bad creds give
    # 200 + "Fails."; localhost auth-bypass returns 200/empty. 403 == temporarily banned.
    return status in (200, 204) and b"Fails" not in body

  def get_preferences(self) -> dict:
    return self._get_json("/api/v2/app/preferences")

  def set_preferences(self, changes: dict) -> bool:
    status, _ = self._post("/api/v2/app/setPreferences", {"json": json.dumps(changes)})
    return status == 200

  def get_torrents(self) -> list[dict]:
    return self._get_json("/api/v2/torrents/info")

  def get_categories(self) -> dict:
    return self._get_json("/api/v2/torrents/categories")

  def set_auto_management(self, hashes: list[str], enable: bool = True) -> bool:
    status, _ = self._post(
      "/api/v2/torrents/setAutoManagement",
      {"hashes": "|".join(hashes), "enable": "true" if enable else "false"},
    )
    return status == 200


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Enable qBittorrent Auto TMM and relocate existing torrents into category folders."
  )
  parser.add_argument("--dry-run", action="store_true", help="Report the plan and exit 0.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("QBITTORRENT_HOST", DEFAULT_QBT_HOST).rstrip("/")
  user = os.environ.get("QBITTORRENT_USER")
  pw = os.environ.get("QBITTORRENT_PASS")
  if not user or not pw:
    print("ERROR: QBITTORRENT_USER / QBITTORRENT_PASS not set (check .env)", file=sys.stderr)
    return 2

  client = QbtClient(host)
  if not client.login(user, pw):
    print(f"ERROR: qBittorrent auth failed at {host}", file=sys.stderr)
    return 2
  try:
    prefs = client.get_preferences()
    torrents = client.get_torrents()
    categories = client.get_categories()
  except (urllib.error.URLError, json.JSONDecodeError) as exc:
    print(f"ERROR: cannot read qBittorrent state: {exc}", file=sys.stderr)
    return 2

  shaped = shaper_present()
  pref_changes = plan_pref_changes(prefs, desired_prefs(shaped))
  unmanaged = collect_unmanaged_hashes(torrents)
  unmanaged_set = set(unmanaged)
  targets = summarize_targets([t for t in torrents if t.get("hash") in unmanaged_set], categories)

  print("=== qBittorrent settings enforce ===" + ("  [DRY RUN]" if args.dry_run else ""))
  print(f"wan shaper: {'present' if shaped else 'MISSING -> degraded upload cap'}")
  print(f"pref changes: {describe_drift(prefs, pref_changes) if pref_changes else 'none'}")
  print(f"torrents to auto-manage: {len(unmanaged)} of {len(torrents)}")
  for path, n in sorted(targets.items()):
    print(f"  -> {path}: {n}")

  if args.dry_run:
    return 0

  failures = 0
  if pref_changes:
    if client.set_preferences(pref_changes):
      print(f"applied {len(pref_changes)} pref change(s)")
    else:
      failures += 1
      print("WARNING: setPreferences failed", file=sys.stderr)

  if unmanaged:
    # Chunk to keep the request body sane on large libraries.
    chunk = 200
    done = 0
    for i in range(0, len(unmanaged), chunk):
      batch = unmanaged[i : i + chunk]
      if client.set_auto_management(batch, enable=True):
        done += len(batch)
      else:
        failures += 1
        print(f"WARNING: setAutoManagement failed for {len(batch)} torrents", file=sys.stderr)
    print(f"auto-managed {done}/{len(unmanaged)} torrent(s) (relocating into category folders)")

  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
