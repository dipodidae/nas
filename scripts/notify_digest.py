#!/usr/bin/env python3
"""One markdown message a day, replacing everything that used to be chatter.

Why this exists
---------------
The six-lane split (ADR-0033) works by *not* sending things. A cron job that
succeeded, a `*/5` sweep that changed nothing, an enforcer that found the
settings already correct, an image with a newer tag you have not applied yet —
none of those are worth a push, and sending them is what made the old
`nas-alerts` topic unreadable.

But "not sent" and "not happening" look identical from a phone, and an alerting
system you cannot distinguish from a broken one is not trustworthy. So the
suppressed traffic gets aggregated instead of dropped: one `nas-infra` message
at 09:00 that says what the stack did, including **how many notifications the
cooldowns swallowed**. If that number is zero every day, the cooldowns are not
doing anything and should be shortened; if it is enormous, something is flapping
and the digest is where you find out.

Deliberately reports state, not events. Every line answers "is this still true
right now", so a digest that arrives late is still correct.

Exit codes
----------
  0  digest built and published
  1  digest built, but at least one section could not be collected, or the
     publish did not land. Partial by design — a digest that refuses to send
     because Prowlarr was restarting is worse than one with a gap in it.
  2  fatal (cannot reach docker at all, so there is nothing to report)

Environment
-----------
  NTFY_TOKEN_SCRIPTS   ntfy token; unset = print only
  API_KEY_SONARR / API_KEY_RADARR / API_KEY_LIDARR   for the import counts
  CONFIG_DIRECTORY     to find diun's state and the *arr databases
  SHARE_DIRECTORY      the media mount, for the free-space line

Usage
-----
  python scripts/notify_digest.py            # collect and publish
  python scripts/notify_digest.py --dry-run  # print the markdown, publish nothing
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

if "NTFY_TOKEN_SCRIPTS" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify as notifier  # noqa: E402, I001


REPO_ROOT = Path(__file__).resolve().parent.parent
CRON_STATE_DIR = REPO_ROOT / "logs" / "cron-state"
DEFAULT_MOUNT = "/mnt/drive"
DIGEST_WINDOW_H = 24
# `/history/since?date=` returns EVERY record since a date as a flat list, with
# no paging. The paged `/history` endpoint cannot answer "how many imports in
# 24h" without walking pages: 200 rows of Sonarr history here reached back only
# a few hours, because 112 of them were grabs and 70 were renames -- so a single
# page produced "0 imports" for a day that had some, and the miscount looked
# exactly like a quiet day.
#
# The event names are READ OFF THE LIVE APIS, not remembered. Sonarr's import
# event is `downloadFolderImported`, NOT `episodeFileImported` -- which is what
# this file was first written with, and which silently counted zero forever
# because no such event type exists.
ARR_APPS = {
  "sonarr": ("http://localhost:8989", "v3", "API_KEY_SONARR", "downloadFolderImported"),
  "radarr": ("http://localhost:7878", "v3", "API_KEY_RADARR", "downloadFolderImported"),
  "lidarr": ("http://localhost:8686", "v1", "API_KEY_LIDARR", "trackFileImported"),
}
FAILED_EVENT = "downloadFailed"


@dataclass
class Section:
  """One block of the digest. `problems` is what makes the exit code 1."""

  heading: str
  lines: list[str] = field(default_factory=list)
  failed: bool = False


def _run(cmd: list[str], timeout: float = 30.0) -> tuple[int, str]:
  try:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
  except (OSError, subprocess.TimeoutExpired):
    return 1, ""
  return proc.returncode, proc.stdout


def _get_json(url: str, headers: dict[str, str], timeout: float = 10.0):
  req = urllib.request.Request(url, headers=headers)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, ValueError, urllib.error.HTTPError):
    return None


# --------------------------------------------------------------------------
# Collectors. Each returns a Section and never raises.
# --------------------------------------------------------------------------


def containers_section() -> Section:
  """Up / unhealthy, from compose's own view rather than `docker ps`.

  Compose's view is the one that can report a service which has NO container,
  and that is the failure mode worth a daily line (ADR-0006).
  """
  section = Section("Containers")
  code, out = _run(["docker", "compose", "ps", "-a", "--format", "{{.Name}}\t{{.Status}}"])
  if code != 0:
    section.failed = True
    section.lines.append("could not read `docker compose ps`")
    return section
  rows = [line.split("\t", 1) for line in out.splitlines() if "\t" in line]
  code, svc_out = _run(["docker", "compose", "config", "--services"])
  defined = {s.strip() for s in svc_out.splitlines() if s.strip()} if code == 0 else set()
  present = {name for name, _status in rows}
  missing = sorted(defined - present)
  unhealthy = sorted(n for n, s in rows if "unhealthy" in s.lower())
  stopped = sorted(n for n, s in rows if not s.lower().startswith("up"))

  section.lines.append(f"**{len(present)} of {len(defined) or len(present)}** containers present")
  if missing:
    section.lines.append(f"⛔ no container at all: {', '.join(missing)}")
    section.failed = True
  if stopped:
    section.lines.append(f"⛔ not running: {', '.join(stopped)}")
    section.failed = True
  if unhealthy:
    section.lines.append(f"⚠️ unhealthy: {', '.join(unhealthy)}")
  if not (missing or stopped or unhealthy):
    section.lines.append("all running, none unhealthy")
  return section


def disk_section(mount: str) -> Section:
  """Free space and the ext4 error counter, for the disk with no redundancy."""
  section = Section("Disk")
  try:
    usage = shutil.disk_usage(mount)
  except OSError as exc:
    section.failed = True
    section.lines.append(f"{mount}: unreadable ({exc})")
    return section
  pct = usage.used / usage.total * 100 if usage.total else 0.0
  free_tb = usage.free / 1024**4
  flag = "⚠️ " if pct >= 90 else ""
  section.lines.append(f"{flag}`{mount}` {pct:.0f}% used, **{free_tb:.2f} TB** free")
  # The durable channel: ext4's own superblock counter survives the reboot and
  # the log rotation that hide the kernel-log version (AGENTS.md, ADR-0023).
  code, out = _run(["findmnt", "-no", "SOURCE", mount])
  if code == 0 and out.strip():
    code, tune = _run(["sudo", "-n", "tune2fs", "-l", out.strip()])
    if code == 0:
      # tune2fs OMITS `FS Error count` entirely when it is zero: absence is the
      # healthy state, not unknown. And `Filesystem state` must be compared for
      # EQUALITY -- "clean with errors" contains "clean".
      errors = next(
        (ln.split(":", 1)[1].strip() for ln in tune.splitlines() if "FS Error count" in ln),
        "0",
      )
      state = next(
        (ln.split(":", 1)[1].strip() for ln in tune.splitlines() if ln.startswith("Filesystem state")),
        "",
      )
      bad = errors != "0" or (state and state != "clean")
      section.lines.append(
        f"{'⛔ ' if bad else ''}ext4 state `{state or 'unknown'}`, error count {errors}"
      )
      section.failed = section.failed or bool(bad)
  return section


def oom_section(window_h: int = DIGEST_WINDOW_H) -> Section:
  """Kernel OOM kills. The journal holds ~3 days here, so 24h always fits."""
  section = Section("OOM kills")
  code, out = _run(["journalctl", "-k", "--since", f"-{window_h}h", "--no-pager", "-o", "short-iso"])
  if code != 0:
    section.failed = True
    section.lines.append("cannot read `journalctl -k`")
    return section
  hits = [ln for ln in out.splitlines() if "Out of memory: Killed process" in ln or "oom-kill:" in ln]
  section.lines.append(
    f"⛔ **{len(hits)}** in {window_h}h" if hits else f"none in {window_h}h"
  )
  section.failed = bool(hits)
  return section


def cron_section(state_dir: Path = CRON_STATE_DIR, window_h: int = DIGEST_WINDOW_H) -> Section:
  """Which wrapped jobs failed, and which have gone quiet."""
  section = Section("Cron")
  if not state_dir.is_dir():
    section.failed = True
    section.lines.append(f"no cron state at `{state_dir}`")
    return section
  now = time.time()
  failing: list[str] = []
  stale: list[str] = []
  total = 0
  for path in sorted(state_dir.glob("*.json")):
    total += 1
    try:
      state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
      stale.append(f"{path.stem} (unreadable)")
      continue
    if state.get("failing_since"):
      n = int(state.get("consecutive_failures") or 1)
      failing.append(f"{path.stem} (exit {state.get('last_exit')}, ×{n})")
    max_age = float(state.get("max_age_min") or 0)
    last_ok = float(state.get("last_success") or 0)
    if max_age and last_ok and (now - last_ok) / 60.0 > max_age:
      stale.append(f"{path.stem} ({(now - last_ok) / 3600.0:.0f}h)")
  section.lines.append(f"**{total}** wrapped jobs tracked")
  if failing:
    section.lines.append(f"⛔ failing: {', '.join(failing)}")
    section.failed = True
  if stale:
    section.lines.append(f"⚠️ overdue: {', '.join(stale)}")
  if not (failing or stale):
    section.lines.append(f"no failures or overdue jobs in {window_h}h")
  return section


def updates_section(config_dir: str | None) -> Section:
  """Images with a newer tag, read from diun's own state.

  Read rather than re-derived: diun already did the registry work at 04:10, and
  a digest that re-queries every registry every morning is a digest that can be
  rate-limited into lying.
  """
  section = Section("Image updates")
  manifest = REPO_ROOT / "diun" / "manifest.yml"
  if manifest.is_file():
    watched = sum(1 for ln in manifest.read_text().splitlines() if ln.strip().startswith("- name:"))
    section.lines.append(f"**{watched}** images watched by diun")
  if not config_dir:
    section.lines.append("`CONFIG_DIRECTORY` unset — cannot read diun's state")
    section.failed = True
    return section
  db = Path(config_dir) / "diun" / "diun.db"
  if not db.is_file():
    section.lines.append("diun has no state yet (first run pending)")
    return section
  age_h = (time.time() - db.stat().st_mtime) / 3600.0
  section.lines.append(
    f"{'⚠️ ' if age_h > 30 else ''}diun last wrote its state {age_h:.0f}h ago"
  )
  section.lines.append("pending updates are pushed to `nas-updates` at 04:10")
  return section


def _since_stamp(window_h: int) -> str:
  """UTC ISO timestamp `window_h` hours ago, in the form the *arr APIs accept."""
  import datetime as dt  # noqa: PLC0415

  moment = dt.datetime.now(dt.UTC) - dt.timedelta(hours=window_h)
  return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def count_events(records: list[dict], event: str) -> int:
  """How many history rows are of this event type. Pure."""
  return sum(1 for r in records if r.get("eventType") == event)


def imports_section(window_h: int = DIGEST_WINDOW_H) -> Section:
  """Imports and download failures per *arr, from each app's own history API."""
  section = Section("Imports")
  since = _since_stamp(window_h)
  for app, (base, api, key_env, event) in sorted(ARR_APPS.items()):
    key = os.getenv(key_env, "")
    if not key:
      section.lines.append(f"{app}: `{key_env}` unset")
      section.failed = True
      continue
    records = _get_json(f"{base}/api/{api}/history/since?date={since}", {"X-Api-Key": key})
    if not isinstance(records, list):
      section.lines.append(f"{app}: history unreadable")
      section.failed = True
      continue
    imported = count_events(records, event)
    failed = count_events(records, FAILED_EVENT)
    suffix = f", {failed} failed" if failed else ""
    section.lines.append(f"{app}: **{imported}** imported{suffix}")
  return section


def slskd_section() -> Section:
  """Soulseek login state — the thing that is up and dead at the same time."""
  section = Section("slskd")
  key = os.getenv("API_KEY_SLSKD", "")
  if not key:
    section.lines.append("`API_KEY_SLSKD` unset")
    section.failed = True
    return section
  host = os.getenv("SLSKD_HOST", "http://localhost:5030").rstrip("/")
  data = _get_json(f"{host}/api/v0/server", {"X-API-Key": key})
  if not isinstance(data, dict):
    # Expected for ~2h after a cold start: while slskd rescans its shares it has
    # no HTTP listener at all (ADR-0026). Not a digest failure.
    section.lines.append("no login state yet (share rescan, or down)")
    return section
  logged_in = bool(data.get("isLoggedIn"))
  section.lines.append(
    f"{'' if logged_in else '⚠️ '}{'logged in' if logged_in else 'LOGGED OUT'} "
    f"(`{data.get('state', '?')}`)"
  )
  return section


def backup_section() -> Section:
  """When the config backup last succeeded — the line that replaces its push."""
  section = Section("Config backup")
  path = CRON_STATE_DIR / "config-backup.json"
  try:
    state = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError):
    section.failed = True
    section.lines.append("no `config-backup` state file — is the job registered?")
    return section
  last_ok = float(state.get("last_success") or 0)
  if not last_ok:
    section.failed = True
    section.lines.append("⛔ has never succeeded")
    return section
  age_h = (time.time() - last_ok) / 3600.0
  section.lines.append(
    f"{'⛔ ' if age_h > 30 else ''}last good run **{age_h:.0f}h ago**"
  )
  section.failed = age_h > 30
  return section


def suppressed_section() -> Section:
  """How much the cooldowns swallowed. The number that keeps them honest."""
  section = Section("Suppressed")
  state = notifier.load_state()
  by_lane: dict[str, int] = {}
  for entry in state.cooldowns.values():
    n = int(entry.get("suppressed") or 0)
    if n:
      by_lane[str(entry.get("lane") or "?")] = by_lane.get(str(entry.get("lane") or "?"), 0) + n
  if not state.suppressed_total:
    section.lines.append(
      "0 — nothing hit a cooldown. If that holds for a week the windows are too short to matter."
    )
    return section
  detail = ", ".join(f"nas-{lane} ×{n}" for lane, n in sorted(by_lane.items()))
  section.lines.append(f"**{state.suppressed_total}** messages held back by a cooldown ({detail})")
  return section


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render(sections: list[Section]) -> str:
  """Markdown body. Pure, so the layout is testable without collecting."""
  out: list[str] = []
  for section in sections:
    out.append(f"**{section.heading}**")
    out.extend(f"- {line}" for line in section.lines or ["(nothing collected)"])
    out.append("")
  return "\n".join(out).rstrip() + "\n"


def collect() -> list[Section]:
  """Every section, in reading order: worst news first."""
  return [
    containers_section(),
    disk_section(os.getenv("SHARE_DIRECTORY") or DEFAULT_MOUNT),
    oom_section(),
    cron_section(),
    backup_section(),
    imports_section(),
    slskd_section(),
    updates_section(os.getenv("CONFIG_DIRECTORY")),
    suppressed_section(),
  ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Publish the daily stack digest to nas-infra.")
  parser.add_argument(
    "--dry-run", action="store_true", help="Print the markdown; publish nothing.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  code, _ = _run(["docker", "version", "--format", "{{.Server.Version}}"])
  if code != 0:
    print("ERROR: cannot reach docker; there is nothing to report", file=sys.stderr)
    return 2

  sections = collect()
  body = render(sections)
  print(body)
  if args.dry_run:
    return 1 if any(s.failed for s in sections) else 0

  # No cooldown and no dedup key: this is one message a day by construction, and
  # a cooldown on it could only ever suppress the whole digest.
  result = notifier.notify(
    notifier.Lane.INFRA,
    f"🗒 NAS digest · {time.strftime('%a %d %b')}",
    body,
    markdown=True,
    tags=(notifier.TAG_INFRA,),
  )
  if any(s.failed for s in sections):
    return 1
  return 0 if result else 1


if __name__ == "__main__":
  sys.exit(main())
