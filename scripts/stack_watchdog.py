#!/usr/bin/env python3
"""Watch the whole compose stack and shout when something breaks.

Why this exists
---------------
Nothing on this box reported failure. Two incidents made that concrete:

  * Jellyfin was OOM-killed by the kernel five times in 48 hours and it only
    surfaced because a TV episode stuttered (docs/jellyfin-playback-audit.md).
  * qBittorrent sat dead for fourteen hours — silently blocking every download
    in the stack — and was found by accident during unrelated work. Twice.

Both were invisible until a human went looking. This is the minimal thing that
looks instead: one cron-driven script, four checks, one push notification.

What it checks
--------------
1. Every service defined in the compose files exists as a container, is
   running, and is not `unhealthy`. A service defined but *never created* is
   caught too — `autoheal` was absent from this stack for over a month and
   nothing noticed, because "not unhealthy" and "not there at all" look
   identical to anything that only inspects running containers.
2. Restart churn: a container whose restart count climbs between runs is
   flapping even if it happens to be "up" at the moment the check fires.
3. Jellyfin anonymous memory, read from the sampler's log. `anon` is the
   OOM-relevant figure; `memory.current`/`mem_peak` include page cache and
   routinely read high for entirely benign reasons. A stale or failing sampler
   is itself an alert — a monitor that quietly stops is worse than none.
4. Kernel OOM kills, from `journalctl -k` (readable unprivileged here; `dmesg`
   is not, under kernel.dmesg_restrict). Catches kills of *any* process,
   including ones that leave no trace in a container's own logs.
5. `autoheal` specifically — running, actually supervising something, and its
   restarts succeeding. The supervisor going quiet is invisible by
   construction: everything it watches stays healthy, so the only symptom is
   an absence. It was stopped for over a month before anyone noticed.
6. That an off-box heartbeat is configured at all (`heartbeat.py`). Nothing
   running on this host can report that this host is down.
   And that the CAKE egress shaper is still installed — `tc` state does not
   survive a link-down, and without it the uplink is an unmanaged FIFO again.
7. The crontab itself, textually: a line using a relative path without a `cd`
   into the repo cannot work from cron's `$HOME`, and a line naming a script
   that does not exist never will. Both are invisible in the job's *output*
   because there is none.
8. Every cron job wrapped by `cron_job.py` has succeeded within the window its
   own cron line declares. A job that runs and fails pushes its own alert
   immediately; this catches the other half — a job whose cron line is broken
   and which therefore produces nothing at all to notice.
9. Prowlarr indexers that have stayed failed past a threshold — *not* ones that
   flapped. Public trackers flap all day and that is their normal condition;
   treating each cycle as an incident produced 103 ntfy messages in 48 hours
   describing two actually-dead indexers.
10. Every other *arr health warning (Prowlarr/Sonarr/Radarr/Lidarr `/health`),
   deduped per app and repeated daily. Together, 9 and 10 let the three apps'
   own `onHealthIssue` + `onHealthRestored` Ntfy connections be switched OFF
   without losing anything: those have no filter, fire on every transition, and
   all three fire for the same indexer. Indexer state is reported once, by 9.

Delivery
--------
Through `scripts/notify.py`, the lane router — this file holds no topic name,
no credential and no priority mapping. ntfy was chosen over Gotify because a
plain `POST <topic-url>` with a text body is the whole publish contract and
needs no server-side application setup before the first alert can land.

Which lane each alert goes to is one table, `LANE_BY_KEY_PREFIX`, plus one
escalation rule. The escalation is the part worth knowing: an unhealthy
container starts in `nas-infra` because most of them are blips, moves to
`nas-attention` after 15 minutes, and moves to `nas-critical` after 5 minutes
if it is one of the four services whose failure a human notices unaided
(jellyfin, nextcloud, swag, qbittorrent). "No container at all" — ADR-0006's
failure mode, which cost qBittorrent 13 hours — skips the ladder and is
critical from the first tick. An escalation is sent immediately rather than
waiting for `--repeat-min`, or the alert would spend the outage in the quietest
lane. ADR-0033.

State lives in a small JSON file so a continuing problem notifies once and then
at a slow repeat interval rather than every five minutes, and so recoveries are
announced. Without the state file the script still works — it just re-notifies
every run.

Exit codes
----------
  0  everything healthy
  1  at least one alert is active (notification sent, if a webhook is set)
  2  fatal (cannot reach docker at all)

Environment
-----------
  NTFY_TOKEN_SCRIPTS     access token for the write-only `nas-scripts` ntfy
                         account. Unset = print only (still useful in cron
                         mail); the router says so rather than failing.
  NTFY_URL               where to publish (default http://127.0.0.1:8410,
                         loopback so alert contents never leave the box).
  NTFY_TOPIC_<LANE>      optional per-lane topic override.

Usage
-----
  python scripts/stack_watchdog.py
  python scripts/stack_watchdog.py --state logs/stack_watchdog.json
  python scripts/stack_watchdog.py --jellyfin-anon-mb 4096 --repeat-min 60
  python scripts/stack_watchdog.py --self-test    # send one test notification
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

if "NTFY_TOKEN_SCRIPTS" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

# Delivery goes through the lane router, which is the only thing that knows a
# topic name. Imported as `notifier` because this module already exports a
# function called `notify()` -- cron_job.py imports that name, and renaming it
# would be a change to a contract for no gain. ADR-0033.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify as notifier  # noqa: E402, I001


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE = REPO_ROOT / "logs" / "stack_watchdog.json"
DEFAULT_MEM_LOG = REPO_ROOT / "logs" / "jellyfin-mem.log"
DEFAULT_CRON_STATE = REPO_ROOT / "logs" / "cron-state"

# Jellyfin's own compose mem_limit is 10g. Healthy `anon` sits at 0.4-1.3GB and
# the heaviest routine event (a full library scan) peaked at 1.56GB, so 4GB is
# comfortably above anything normal and still leaves headroom to react before
# the cgroup limit does the reacting for us.
DEFAULT_JELLYFIN_ANON_MB = 4096.0
# The sampler is a per-minute cron job; 15 minutes of silence means it is broken.
DEFAULT_SAMPLER_STALE_MIN = 15.0
DEFAULT_REPEAT_MINUTES = 60.0
# Kernel OOM lines look like:
#   Out of memory: Killed process 12345 (jellyfin) total-vm:...,anon-rss:...
OOM_PATTERN = re.compile(r"Out of memory: Killed process|oom-kill:|oom_reaper:")
# Severity is now carried by the ntfy priority the ROUTER derives from the lane,
# not by a per-severity string this file picks. What is left here is the mapping
# used only to *override* a lane's default when a message is unusually severe
# for its lane -- info->3, warning->4, critical->5, notice->2 (ADR-0033).
SEVERITY_PRIORITY = {"critical": 5, "warning": 4, "notice": 2, "info": 3}

# The services whose failure a human notices without being told. Anything here
# that stays down past USER_VISIBLE_CRITICAL_MIN escalates to nas-critical even
# though a merely-unhealthy container starts life in nas-infra.
#
# tinyauth is on this list as of 2026-09-04 and belongs here for a reason worth
# spelling out: it is the single forward-auth door in front of THIRTEEN routes
# (ADR-0034), so losing it does not degrade one service, it closes every
# protected one at once with a 502. The unprotected ones -- jellyfin, nextcloud,
# ntfy, the apex -- keep serving, which is exactly what makes this failure easy
# to miss: the box looks fine and the sysadmin surface is what is gone.
USER_VISIBLE_SERVICES = frozenset({
    "jellyfin", "nextcloud", "swag", "qbittorrent", "tinyauth",
})
# An unhealthy container is usually a blip. Fifteen minutes of it is not.
ESCALATE_ATTENTION_MIN = 15.0
USER_VISIBLE_CRITICAL_MIN = 5.0

# Services whose `unhealthy` is a KNOWN, self-clearing consequence of the work
# they are doing, so age alone must not escalate them out of nas-infra. Same
# shape as ADR-0026's slskd start_period: the container is not broken, it is
# busy, and an alerter that cries wolf during normal operation trains you to
# swipe it away.
#
#   playlist-generator: its CPU-bound enrichment stages (embeddings, profiles,
#   clusters, banger-flags, genre-manifold, audio) block the single-worker
#   backend's event loop for the duration -- measured CPU 101.63% with an
#   unhealthy streak of 23 consecutive checks. Only the three I/O-bound stages
#   are in the crontab for exactly this reason, but running a CPU-bound one by
#   hand is a legitimate thing to do and must not page.
#
# They still ALERT -- in nas-infra, and the daily digest counts them. This
# exempts them from the ladder, not from being watched. Note it does not exempt
# `:missing` or `:down`: a service that is absent or exited is not busy.
SLOW_UNHEALTHY_SERVICES = frozenset({"playlist-generator"})
# A standing configuration gap is not an incident: it cannot resolve on its own
# and re-reading it every hour teaches you to ignore the topic. Once a day is
# enough to keep it from being forgotten, which is the only job it has.
CONFIG_GAP_REPEAT_MIN = 1440.0
# Ceiling for the exponential backoff below. Six hours still surfaces a genuine
# outage four times a day; beyond that the alert reads as forgotten rather than
# quiet.
BACKOFF_CAP_MIN = 360.0
# A job that has NEVER succeeded since it was registered is a configuration bug,
# not an outage: it will not fix itself, and nothing about it changes hour to
# hour. It gets one loud page and then a daily reminder. A job that used to work
# and has now gone stale is the opposite -- something changed, it may well come
# back, and it earns the escalating cadence.
NEVER_SUCCEEDED_REPEAT_MIN = CONFIG_GAP_REPEAT_MIN
# How long an in-flight job's heartbeat may go unrefreshed before we stop
# believing it. cron_job.py rewrites it every IN_FLIGHT_HEARTBEAT_SEC (60s), so
# 15min is 15 missed beats -- long enough to survive a loaded box, short enough
# that a wedged or SIGKILLed job stops looking alive. The point of checking the
# heartbeat rather than in_flight_since alone: a job killed with -9 never gets
# to clear its own state, and a permanently "in flight" marker would silence
# the staleness check forever.
IN_FLIGHT_STALE_MIN = 15.0
# How long an indexer must stay failed before it counts as down rather than
# flapping. 6h deliberately matches Prowlarr's own IndexerLongTermStatusCheck,
# which was the single accurate line in the 2026-09-03 feed.
PROWLARR_INDEXER_DOWN_MIN = 360.0
DEFAULT_PROWLARR_URL = "http://localhost:9696"
# The *arr apps whose /health this polls, and the API version each speaks.
# Sonarr/Radarr are v3; Prowlarr/Lidarr are v1 (measured -- lidarr /api/v3/health
# returns 404). Keys come from API_KEY_<APP> in .env.
ARR_HEALTH_APPS = {
  "prowlarr": ("http://localhost:9696", "v1"),
  "sonarr": ("http://localhost:8989", "v3"),
  "radarr": ("http://localhost:7878", "v3"),
  "lidarr": ("http://localhost:8686", "v1"),
}
# INVARIANT: indexer state is owned by check_indexer_failures and reported ONCE.
# Both of these are dropped here, for different reasons:
#
#  * IndexerStatusCheck is the SHORT-term check and the whole source of the flap
#    churn -- it appears and clears within minutes as public trackers bounce.
#  * IndexerLongTermStatusCheck is accurate but redundant. All three apps raise
#    it for the same two indexers, so keeping it turned 2 dead indexers into 6
#    alerts. check_indexer_failures says the same thing better: one key per
#    indexer, the actual outage duration, and backoff.
#
# Anything else an *arr reports is kept -- that is the point of this check.
ARR_HEALTH_IGNORE = frozenset({"IndexerStatusCheck", "IndexerLongTermStatusCheck"})


@dataclass(frozen=True)
class Alert:
  """One problem worth waking someone for.

  `key` is the dedupe identity across runs — it must be stable for the same
  underlying problem and different for a different one.

  `repeat_min` overrides the global `--repeat-min` for this one alert. It
  exists because the alerts in this file are not all the same *kind* of thing:
  a container that died is an incident and should keep nagging hourly, while a
  missing config value is a standing gap that nags once a day. Without the
  override the noisiest setting wins for everything.

  `lane` pins the audience when the key-prefix routing in `lane_for()` would
  get it wrong. Leave it None for anything the table already covers -- a lane
  chosen at a construction site is a lane that drifts from the table.
  """

  key: str
  severity: str
  message: str
  repeat_min: float | None = None
  lane: str | None = None


def backoff_repeat_min(
  active_min: float,
  base: float = DEFAULT_REPEAT_MINUTES,
  cap: float = BACKOFF_CAP_MIN,
) -> float:
  """Re-notify interval for a problem that has been true for `active_min`.

  Doubles once per elapsed interval and then holds at `cap`: with the 60min
  default a still-broken thing pushes at roughly 0h, 1h, 3h, 7h, 13h, 19h...
  rather than every single hour forever.

  This exists because `playlist-sync` sent 20 byte-identical p5 messages in 43
  hours. Nothing in those 20 was new after the first, and an alert that repeats
  unchanged trains you to swipe the topic away -- which is the exact failure
  this whole file was written to prevent. Backing off keeps the signal without
  spending the attention.

  `cap` is a floor on how often you still hear about it, not a silence: a real
  outage still surfaces four times a day.
  """
  interval = base
  elapsed = 0.0
  while interval < cap and elapsed + interval <= active_min:
    elapsed += interval
    interval = min(interval * 2, cap)
  return interval


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
  """Run a command, returning (returncode, stdout). stderr is folded in."""
  try:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
  except (OSError, subprocess.TimeoutExpired) as exc:
    return 127, f"{exc}"
  return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stdout + proc.stderr)


def defined_services() -> list[str] | None:
  """Service names from the merged compose model. None if compose can't be read."""
  code, out = _run(["docker", "compose", "config", "--services"], timeout=120)
  if code != 0:
    return None
  return sorted(line.strip() for line in out.splitlines() if line.strip())


def inspect_containers(project: str) -> dict[str, dict] | None:
  """Inspect every container in the compose project, keyed by service name."""
  code, out = _run(
    ["docker", "ps", "-a", "--filter", f"label=com.docker.compose.project={project}", "-q"],
  )
  if code != 0:
    return None
  ids = [line.strip() for line in out.splitlines() if line.strip()]
  if not ids:
    return {}
  code, out = _run(["docker", "inspect", *ids], timeout=120)
  if code != 0:
    return None
  try:
    raw = json.loads(out)
  except json.JSONDecodeError:
    return None
  result: dict[str, dict] = {}
  for entry in raw:
    labels = (entry.get("Config") or {}).get("Labels") or {}
    service = labels.get("com.docker.compose.service") or entry.get("Name", "").lstrip("/")
    result[service] = entry
  return result


def check_containers(
  services: list[str] | None,
  containers: dict[str, dict],
  prev_restarts: dict[str, int],
  ignore: set[str],
) -> tuple[list[Alert], dict[str, int]]:
  """Alert on missing, stopped, unhealthy and flapping containers."""
  alerts: list[Alert] = []
  restarts: dict[str, int] = {}

  for service in services or sorted(containers):
    if service in ignore:
      continue
    entry = containers.get(service)
    if entry is None:
      alerts.append(
        Alert(
          f"container:{service}:missing",
          "critical",
          f"{service}: defined in the compose files but no container exists",
        )
      )
      continue

    state = entry.get("State") or {}
    name = entry.get("Name", "").lstrip("/") or service
    status = str(state.get("Status", "unknown"))
    restarts[service] = int(state.get("RestartCount") or 0)

    if status != "running":
      exit_code = state.get("ExitCode")
      oom = " (OOM-killed by the kernel)" if state.get("OOMKilled") else ""
      alerts.append(
        Alert(
          f"container:{service}:down",
          "critical",
          f"{name}: {status}, exit={exit_code}{oom}",
        )
      )
      continue

    health = str(((state.get("Health") or {}).get("Status")) or "")
    if health == "unhealthy":
      streak = (state.get("Health") or {}).get("FailingStreak")
      alerts.append(
        Alert(
          f"container:{service}:unhealthy",
          "warning",
          f"{name}: healthcheck failing (streak {streak})",
        )
      )

    before = prev_restarts.get(service)
    if before is not None and restarts[service] > before:
      alerts.append(
        Alert(
          f"container:{service}:restarting",
          "warning",
          f"{name}: restart count {before} -> {restarts[service]} since last check",
        )
      )

  return alerts, restarts


def check_cron_jobs(state_dir: Path) -> list[Alert]:
  """Alert on any wrapped cron job that has gone quiet for too long.

  This is the half that catches a job which never runs at all. A job that runs
  and fails pushes its own alert from `cron_job.py` immediately; a job whose
  cron line is broken produces nothing — no output, no stderr, no exit code —
  and the only evidence is a state file that stops being updated.
  `media_ops_status.py` was in exactly that state for three months.

  Freshness is measured from the last *success*, falling back to the
  registration time so a job that has never once succeeded is still caught.
  The window comes from the cron line's own `--max-age-min`, because the cron
  line is the only place that knows the schedule.
  """
  if not state_dir.is_dir():
    return []
  alerts: list[Alert] = []
  now = time.time()
  for path in sorted(state_dir.glob("*.json")):
    try:
      state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
      alerts.append(
        Alert(f"cron:{path.stem}:unreadable", "warning", f"unreadable cron state file {path.name}")
      )
      continue
    max_age = float(state.get("max_age_min") or 0)
    if max_age <= 0:
      continue
    reference = state.get("last_success") or state.get("registered")
    if not reference:
      continue
    age_min = (now - float(reference)) / 60.0
    # A job that is CURRENTLY RUNNING has not gone quiet -- it is just slow.
    # cron_job.py only writes last_success after the child exits, so a job whose
    # single pass outlives its own window can never refresh the clock while it
    # is working. `playlist-sync` was 21h into a genuinely progressing run,
    # holding its flock (so every 6h tick correctly no-op'd), and was reported
    # stale the whole time. An in-flight heartbeat is proof of life; trust it
    # over the success clock, but only while it is being refreshed.
    in_flight = state.get("in_flight_since")
    heartbeat = state.get("in_flight_heartbeat")
    if in_flight and heartbeat and (now - float(heartbeat)) / 60.0 <= IN_FLIGHT_STALE_MIN:
      continue
    if age_min <= max_age:
      continue
    never = not state.get("last_success")
    if never:
      what = f"has never succeeded (registered {age_min / 60:.1f}h ago)"
    else:
      what = f"last succeeded {age_min / 60:.1f}h ago"
    detail = f"; last exit {state['last_exit']}" if "last_exit" in state else "; it has not run at all"
    if in_flight and heartbeat:
      detail += f"; a run has been in flight {(now - float(in_flight)) / 3600:.1f}h but stopped reporting"
    alerts.append(
      Alert(
        f"cron:{path.stem}:stale",
        "critical",
        f"cron job {path.stem} {what}, allowed {max_age / 60:.1f}h{detail}",
        # Never-succeeded is a config bug: one loud page, then daily. Went-stale
        # escalates, because it may still recover and the age is the news.
        repeat_min=(
          NEVER_SUCCEEDED_REPEAT_MIN if never else backoff_repeat_min(age_min - max_age)
        ),
      )
    )
  return alerts


def lint_crontab(crontab: str, repo_root: Path) -> list[Alert]:
  """Catch the class of crontab bug that cost three months, at install time.

  `media_ops_status.py` ran `.venv/bin/python …` with no `cd`. From cron's
  `$HOME` (/home/tom, which has no .venv) that resolves to nothing, fails
  instantly, produces no output because the line also redirected to /dev/null,
  and looks exactly like a job that is working. Nothing about the *result* of
  such a job is observable — but the defect is plainly visible in the line
  itself, so check the line.

  Two rules, both purely textual:
    * a line using a relative path must `cd` into the repo first;
    * a script named on a line must actually exist.
  """
  alerts: list[Alert] = []
  for raw in crontab.splitlines():
    line = raw.strip()
    if not line or line.startswith("#"):
      continue
    # Cron accepts both "m h dom mon dow CMD" and the "@daily CMD" shorthands.
    # Splitting on a fixed field count silently skips the shorthand form, which
    # would give this lint a blind spot exactly where someone is most likely to
    # hand-write a line.
    if line.startswith("@"):
      parts = line.split(None, 1)
      body = parts[1] if len(parts) > 1 else ""
    else:
      parts = line.split(None, 5)
      body = parts[5] if len(parts) > 5 else ""
    uses_relative = any(tok in body for tok in (".venv/", "scripts/", "logs/"))
    if uses_relative and f"cd {repo_root}" not in body:
      alerts.append(
        Alert(
          f"crontab:no-cd:{zlib.crc32(body.encode()):08x}",
          "critical",
          f"crontab line uses a relative path without `cd {repo_root}` — it "
          f"cannot work from cron's $HOME: {body[:160]}",
        )
      )
    for token in body.split():
      name = token.strip("\"';")
      if name.startswith("scripts/") and name.endswith(".py") and not (repo_root / name).is_file():
        alerts.append(
          Alert(
            f"crontab:missing-script:{name}",
            "critical",
            f"crontab references {name}, which does not exist",
          )
        )
  return alerts


def read_crontab() -> str:
  code, out = _run(["crontab", "-l"])
  return out if code == 0 else ""


def check_media_storage(
  mount: str = "/mnt/drive",
  min_free_gb: float = 100.0,
  fs_device: str = "/dev/sda1",
) -> list[Alert]:
  """Watch the media drive, because nothing else can.

  All ~4.7 TB of media lives on a single USB external disk with no redundancy,
  and its bridge does not pass SMART through under any `smartctl -d` type
  (sat/sat,12/sat,16/usbjmicron/usbsunplus/usbcypress/scsi all refuse). So the
  usual "is the disk dying" signal simply does not exist here.

  What does exist is the kernel's view, and it is the earliest warning available:
  a failing USB disk logs I/O errors and link resets long before anything higher
  up notices, and ext4's default on error is to remount read-only — at which
  point every *arr import fails silently and the stack looks healthy. Both are
  invisible without looking, which is the whole reason this file exists.

  Plus one channel the kernel log cannot give you: ext4's superblock error
  counter. The kernel-log sweep below covers 6h and this host's journal retains
  about 3 days, so an error older than that is invisible to both — while
  `tune2fs -l` still reports it, because ext4 writes it to the superblock.
  ADR-0023.
  """
  alerts: list[Alert] = []

  code, opts = _run(["findmnt", "-no", "OPTIONS", mount])
  if code != 0:
    return [Alert("media:unmounted", "critical", f"{mount} is not mounted — the media drive is gone")]
  if "rw" not in opts.split(","):
    alerts.append(
      Alert(
        "media:readonly",
        "critical",
        f"{mount} is mounted READ-ONLY — ext4 remounts ro on error, so this is a "
        "failing disk, and every *arr import will fail while looking healthy",
      )
    )

  code, out = _run(["df", "-B1", "--output=avail", mount])
  if code == 0:
    try:
      free_gb = int(out.splitlines()[-1].strip()) / 1e9
      if free_gb < min_free_gb:
        alerts.append(
          Alert("media:low-space", "warning", f"{mount} has {free_gb:.0f} GB free (below {min_free_gb:.0f})")
        )
    except (ValueError, IndexError):
      pass

  # ext4's own superblock error counter. The kernel-log check below reaches
  # back 6h and the journal on this host retains ~3 days, so a disk error that
  # happened before either window is invisible to both -- but ext4 records it
  # in the superblock, where it survives the reboot and the rotation. This is
  # the ONLY durable health signal this drive has: its USB bridge answers no
  # SMART under any `smartctl -d` type, so `scrutiny` cannot see it. ADR-0023.
  #
  # tune2fs omits "FS Error count" entirely when it is zero, so absence is the
  # healthy state. "Filesystem state" must be compared for EQUALITY with
  # "clean": "clean with errors" contains "clean" and would pass a substring
  # test during the exact failure this guards against.
  code, t2fs = _run(["sudo", "-n", "tune2fs", "-l", fs_device])
  if code != 0:
    alerts.append(
      Alert(
        "media:ext4-unreadable",
        "warning",
        f"cannot read the ext4 superblock of {fs_device} via `sudo -n tune2fs -l` — "
        "the media drive's only durable error counter is unreadable, so a past "
        "disk error would leave no trace anywhere",
      )
    )
  else:
    t2fs_fields = {
      k.strip(): v.strip()
      for k, _, v in (ln.partition(":") for ln in t2fs.splitlines())
      if _ and not k.startswith(" ")
    }
    state = t2fs_fields.get("Filesystem state", "").strip()
    if state and state != "clean":
      alerts.append(
        Alert(
          "media:ext4-state",
          "critical",
          f"{fs_device} filesystem state is {state!r}, not 'clean' — ext4 sets this "
          "on error and it persists across reboots",
        )
      )
    errs = t2fs_fields.get("FS Error count")
    if errs and errs.strip() not in ("", "0"):
      alerts.append(
        Alert(
          "media:ext4-errors",
          "critical",
          f"{errs} ext4 error(s) recorded in {fs_device}'s superblock "
          f"(first {t2fs_fields.get('First error time', '?')}, "
          f"last {t2fs_fields.get('Last error time', '?')}) — this disk has no SMART, "
          "so this counter is the only lasting evidence it is failing",
        )
      )

  code, kern = _run(["journalctl", "-k", "--since", "-6h", "--no-pager", "-o", "cat"])
  if code == 0:
    hits = [
      ln for ln in kern.splitlines()
      if re.search(r"I/O error|EXT4-fs error|remounting filesystem read-only|reset high-speed USB", ln)
    ]
    if hits:
      alerts.append(
        Alert(
          "media:kernel-errors",
          "critical",
          f"{len(hits)} disk/USB error(s) in the kernel log in 6h — the only early "
          f"warning available on this drive: {hits[-1][:160]}",
        )
      )
  return alerts


def check_stuck_starting(containers: dict[str, dict], max_min: float = 150.0) -> list[Alert]:
  """Alert when a container has been health=starting for too long.

  This exists because of a real trade-off, not for completeness. `slskd`'s
  `start_period` has to exceed a full cold share scan (over 2 h at 177k files)
  or autoheal restarts it mid-scan, the scan is marked suspect, the next start
  force-rescans, and slskd is never up again -- a loop the compose comment for
  that healthcheck describes at length (ADR-0009).

  But a long `start_period` is a long BLIND window: Docker does not count
  failures inside it, so a genuinely dead web server looks identical to a slow
  one, and `autoheal` and `make verify-runtime` both stay quiet.

  So the window is watched instead of shortened. This never restarts anything --
  a restart is the one thing that makes the slskd case worse. It tells a human
  that something has been starting for longer than starting should take, which
  is the ADR-0009 pattern: observe the thing a restart cannot fix.
  """
  alerts: list[Alert] = []
  now = _dt.datetime.now(_dt.UTC)
  for name, info in sorted(containers.items()):
    state = info.get("State") or {}
    health = ((state.get("Health") or {}).get("Status") or "").lower()
    if health != "starting":
      continue
    started = state.get("StartedAt") or ""
    try:
      began = _dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
      continue
    mins = (now - began).total_seconds() / 60
    if mins < max_min:
      continue
    alerts.append(
      Alert(
        f"container:{name}:stuck-starting",
        "warning",
        f"{name} has been health=starting for {mins:.0f} min (over {max_min:.0f}). "
        "Docker counts no failures inside start_period, so nothing else will "
        "report this -- and for slskd a restart makes it strictly worse "
        "(ADR-0009/ADR-0026). Check whether it is still making progress.",
      )
    )
  return alerts


def _arr_get(base: str, api_key: str, path: str, timeout: int = 15) -> object | None:
  """GET one *arr API path as JSON. None on any failure — never raises."""
  req = urllib.request.Request(f"{base}{path}", headers={"X-Api-Key": api_key})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read())
  except (OSError, ValueError):
    return None


def fetch_indexer_failures(base: str = "http://localhost:9696", api_key: str = "") -> list[dict] | None:
  """Prowlarr's own view of which indexers are failing, with names attached.

  Returns [{name, initial_failure, disabled_till}, ...], or None if Prowlarr
  could not be reached (which is not itself an indexer problem).

  `/indexerstatus` returns a row ONLY for an indexer Prowlarr currently
  considers failing, so an empty list genuinely means "all fine" — that is the
  authority this check is built on, rather than on notification traffic.
  """
  status = _arr_get(base, api_key, "/api/v1/indexerstatus")
  if not isinstance(status, list):
    return None
  names: dict[int, str] = {}
  indexers = _arr_get(base, api_key, "/api/v1/indexer")
  if isinstance(indexers, list):
    names = {int(i["id"]): str(i.get("name", i["id"])) for i in indexers if "id" in i}
  rows = []
  for row in status:
    if not isinstance(row, dict) or "indexerId" not in row:
      continue
    idx = int(row["indexerId"])
    rows.append({
      "name": names.get(idx, f"id {idx}"),
      "initial_failure": row.get("initialFailure"),
      "disabled_till": row.get("disabledTill"),
    })
  return rows


def _parse_arr_time(value: object) -> float | None:
  """Parse an *arr UTC timestamp ('2026-08-27T07:11:44Z') to an epoch."""
  if not isinstance(value, str) or not value:
    return None
  text = value.replace("Z", "+00:00")
  try:
    return _dt.datetime.fromisoformat(text).timestamp()
  except ValueError:
    return None


def check_indexer_failures(
  rows: list[dict] | None,
  min_down_min: float = PROWLARR_INDEXER_DOWN_MIN,
) -> list[Alert]:
  """Alert only on indexers that have stayed failed, not on ones that flapped.

  Public trackers flap all day; that is their normal condition, not an incident.
  Prowlarr/Sonarr/Radarr each hold an Ntfy connection on `onHealthIssue` +
  `onHealthRestored` with no filter, and Prowlarr's SHORT-term IndexerStatusCheck
  appears and clears on every flap, so one indexer bouncing fans out across
  three apps. Measured from ntfy's own cache over 48h: 22+22 Sonarr, 19+17
  Prowlarr, 12+11 Radarr = 103 messages, describing what the long-term check
  summarises as two indexers.

  So this damps on OUR side of the webhook, which is also the only side that
  works: Prowlarr's tag-based notification filtering is unreliable
  (Prowlarr#1977).

  The damping is `initialFailure` age, which is the same signal as Prowlarr's
  own `IndexerLongTermStatusCheck` ("unavailable due to failures for more than 6
  hours") and was the one accurate line in the 2026-09-03 feed: it named 1337x
  and Torrent[CORE], the two that really were down, while Knaben, Uindex and
  TorrentDownload -- reported as "currently failing, no restore" -- had no
  `/indexerstatus` row at all.

  A flap therefore produces nothing, and a genuine outage produces one alert
  and one recovery. `disabledTill` is deliberately NOT the trigger: Prowlarr
  sets it on the first failure, so it is true of a flap too.
  """
  if rows is None:
    return []
  now = time.time()
  alerts = []
  for row in sorted(rows, key=lambda r: str(r.get("name"))):
    started = _parse_arr_time(row.get("initial_failure"))
    if started is None:
      continue
    down_min = (now - started) / 60.0
    if down_min < min_down_min:
      continue
    name = str(row.get("name"))
    till = row.get("disabled_till") or "unknown"
    alerts.append(
      Alert(
        f"prowlarr:indexer:{name}:down",
        "warning",
        f"indexer {name} has been failing for {down_min / 60:.1f}h "
        f"(disabled till {till}) — past the {min_down_min / 60:.0f}h mark, so "
        f"this is not flapping. Dead site, rate limit or Cloudflare?",
        repeat_min=backoff_repeat_min(down_min - min_down_min),
      )
    )
  return alerts


def fetch_arr_health(apps: dict[str, tuple[str, str]] | None = None) -> list[dict] | None:
  """Every *arr health warning, as [{app, source, type, message}, ...].

  Returns None only if NO app could be reached at all, which is a docker
  problem rather than an application one and is already `check_containers`'
  job. An app that individually fails to answer is skipped: its container
  being down is reported elsewhere, and inventing a health warning for it here
  would double-report the same outage.

  Note the API versions differ: Sonarr and Radarr are v3, Prowlarr and Lidarr
  are v1. Measured -- lidarr /api/v3/health returns 404.
  """
  apps = apps or ARR_HEALTH_APPS
  rows: list[dict] = []
  reached = False
  for app, (base, ver) in sorted(apps.items()):
    key = os.getenv(f"API_KEY_{app.upper()}", "")
    if not key:
      continue
    payload = _arr_get(base, key, f"/api/{ver}/health")
    if not isinstance(payload, list):
      continue
    reached = True
    for item in payload:
      if not isinstance(item, dict):
        continue
      rows.append({
        "app": app,
        "source": str(item.get("source", "")),
        "type": str(item.get("type", "")),
        "message": str(item.get("message", "")),
      })
  return rows if reached else None


def check_arr_health(
  rows: list[dict] | None,
  ignore_sources: frozenset[str] = ARR_HEALTH_IGNORE,
) -> list[Alert]:
  """*arr health warnings, deduped by (app, source) and slow to repeat.

  This exists so the three `onHealthIssue` + `onHealthRestored` Ntfy
  connections in Prowlarr, Sonarr and Radarr can be switched OFF without losing
  the warnings that matter. Those connections have no filter and fire on every
  transition, so one flapping public tracker fanned out into 103 messages in
  48h (counted from ntfy's cache.db). Prowlarr's own tag-based filtering is
  unreliable (Prowlarr#1977), so the filtering has to live here.

  `IndexerStatusCheck` is dropped on purpose. It is the SHORT-term check and the
  entire source of the churn: it appears and clears within minutes as trackers
  bounce. `check_indexer_failures` owns indexer state instead, keyed on how long
  an indexer has actually been failing. `IndexerLongTermStatusCheck` is kept --
  it is the >6h signal, and it was the one accurate line in the 2026-09-03 feed.

  Cadence is deliberately daily. Almost every remaining warning is a standing
  condition (root folder missing, no download client, an update available); none
  of them is more urgent at 03:00 than at 09:00, and the watchdog announces the
  recovery on its own when the warning clears.
  """
  if rows is None:
    return []
  alerts = []
  seen = set()
  # Filter before sorting: the sort key itself would raise on a non-dict, and a
  # watchdog that dies on malformed input stops watching everything else.
  usable = [r for r in rows if isinstance(r, dict) and r.get("source")]
  for row in sorted(usable, key=lambda r: (str(r.get("app")), str(r.get("source")))):
    source = str(row.get("source", ""))
    app = str(row.get("app", ""))
    if source in ignore_sources or (app, source) in seen:
      continue
    seen.add((app, source))
    severity = "critical" if str(row.get("type", "")).lower() == "error" else "warning"
    alerts.append(
      Alert(
        f"arr:{app}:{source}",
        severity,
        f"{app}: {row.get('message', '')}".strip(),
        repeat_min=CONFIG_GAP_REPEAT_MIN,
      )
    )
  return alerts


def check_wan_shaper(wan_if: str = "enp88s0") -> list[Alert]:
  """Ask the shaper whether it is doing its job, not whether it exists.

  `tc qdisc show` proves CAKE is loaded. It does not prove the DSCP bulk marks
  are still installed, nor that the shaped rate still matches the line — and
  both are load-bearing. Marks gone means torrents no longer yield; a stale rate
  after the ISP re-provisions means the modem is the bottleneck again and CAKE
  never queues anything. Either way the component is present and the property is
  not, so `wan_shaper.sh check` is the thing to ask.
  """
  code, out = _run(["sudo", "-n", "/home/tom/nas/scripts/wan_shaper.sh", "check"])
  if code == 0:
    return []
  detail = " ".join(line.strip() for line in out.splitlines() if "FAIL" in line) or out.strip()[:200]
  return [
    Alert(
      "wan:shaper:degraded",
      "critical",
      f"internet egress is not being shaped and prioritised: {detail} "
      "Restore: sudo /home/tom/nas/scripts/wan_shaper.sh apply",
    )
  ]


def check_heartbeat_configured() -> list[Alert]:
  """Nag until the off-box dead-man's switch actually has a URL.

  Everything else in this file runs on the same host it watches, so none of it
  can report that the host is down. `heartbeat.py` closes that, but only once
  someone creates the external check — an account action no script can do. An
  unconfigured heartbeat is therefore a real, standing gap in coverage, and it
  should keep saying so rather than being quietly forgotten.

  It says so *once a day*, at low priority. This is a configuration warning,
  not an incident: it describes a gap that has been there since the box was
  built, it cannot clear itself, and nothing about it is more urgent at 03:00
  than at any other time. At the hourly default it produced 14 identical pushes
  in one night, which is how a topic stops being read.
  """
  if os.getenv("NAS_HEARTBEAT_URL", "").strip():
    return []
  return [
    Alert(
      "heartbeat:unconfigured",
      "notice",
      "NAS_HEARTBEAT_URL is unset — nothing off this box would notice if the host "
      "died. Create a check at healthchecks.io and put its ping URL in .env",
      repeat_min=CONFIG_GAP_REPEAT_MIN,
    )
  ]


def check_autoheal(containers: dict[str, dict], recent_logs: str) -> list[Alert]:
  """Check the supervisor itself, not just the things it supervises.

  `autoheal` sat stopped from 2026-07-29 to 2026-09-01 and nothing noticed:
  every container it watches was healthy the whole time, so the only symptom
  was an absence. Three ways it can be useless while looking fine:

  * not running at all — nothing restarts an unhealthy container;
  * running but no container carries the `autoheal=true` label, so it
    supervises nothing;
  * running, supervising, and its restarts are failing. That last one is not
    hypothetical: with `CURL_TIMEOUT` shorter than
    `AUTOHEAL_DEFAULT_STOP_TIMEOUT` the restart call is cut off mid-stop,
    logged as a failure, and re-issued every interval on top of the one still
    in flight.
  """
  entry = containers.get("autoheal")
  if entry is None or str((entry.get("State") or {}).get("Status")) != "running":
    return [
      Alert(
        "autoheal:down",
        "critical",
        "autoheal is not running — nothing is restarting unhealthy containers",
      )
    ]

  alerts: list[Alert] = []
  supervised = [
    name
    for name, c in containers.items()
    if ((c.get("Config") or {}).get("Labels") or {}).get("autoheal") == "true"
  ]
  if not supervised:
    alerts.append(
      Alert(
        "autoheal:supervising-nothing",
        "warning",
        "autoheal is running but no container carries the autoheal=true label",
      )
    )

  failures = [ln for ln in recent_logs.splitlines() if "failed" in ln.lower()]
  if failures:
    alerts.append(
      Alert(
        "autoheal:restart-failing",
        "warning",
        f"autoheal restart failures ({len(failures)}): {failures[-1].strip()[:200]}",
      )
    )
  return alerts


def autoheal_logs(minutes: int = 15) -> str:
  """Recent autoheal output. Empty string if it cannot be read."""
  code, out = _run(["docker", "logs", "--since", f"{minutes}m", "autoheal"])
  return out if code == 0 else ""


def _parse_mem_line(line: str) -> dict[str, str]:
  """Split one sampler line into its key=value fields (timestamp excluded)."""
  fields = {}
  for part in line.strip().split("\t")[1:]:
    if "=" in part:
      key, _, value = part.partition("=")
      fields[key] = value
  return fields


def _last_log_line(path: Path) -> str | None:
  """Last non-comment, non-empty line of a text file, or None."""
  try:
    lines = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
  except OSError:
    return None
  for line in reversed(lines):
    if not line.lstrip().startswith("#"):
      return line
  return None


def check_jellyfin_memory(mem_log: Path, anon_limit_mb: float, stale_min: float) -> list[Alert]:
  """Alert on high Jellyfin anon-RSS, and on a sampler that has gone quiet."""
  line = _last_log_line(mem_log)
  if line is None:
    return [
      Alert(
        "jellyfin:sampler:missing",
        "warning",
        f"jellyfin memory sampler has written nothing to {mem_log.name}",
      )
    ]

  age_min = (time.time() - mem_log.stat().st_mtime) / 60.0
  if age_min > stale_min:
    return [
      Alert(
        "jellyfin:sampler:stale",
        "warning",
        f"jellyfin memory sampler is {age_min:.0f} min stale (per-minute cron not running?)",
      )
    ]

  if "SAMPLE_FAILED" in line:
    return [Alert("jellyfin:sampler:failing", "warning", f"jellyfin memory sampler: {line[:200]}")]

  raw = _parse_mem_line(line).get("anon", "")
  match = re.match(r"([0-9.]+)MB$", raw)
  if match is None:
    return [
      Alert("jellyfin:sampler:unparsed", "warning", f"cannot parse anon= from sampler: {line[:200]}")
    ]

  anon_mb = float(match.group(1))
  if anon_mb >= anon_limit_mb:
    return [
      Alert(
        "jellyfin:memory:high",
        "critical",
        f"jellyfin anon-RSS {anon_mb / 1024:.2f}GB (threshold {anon_limit_mb / 1024:.2f}GB, "
        f"container mem_limit 10GB) — the OOM pattern from the playback audit",
      )
    ]
  return []


def check_kernel_oom(since_epoch: float | None) -> tuple[list[Alert], float]:
  """Scan the kernel ring buffer for OOM kills newer than the last run."""
  now = time.time()
  # Ask for a slightly wider window than strictly needed; dedupe by line below.
  since = since_epoch or (now - 900)
  stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since))
  code, out = _run(["journalctl", "-k", "--since", stamp, "--no-pager", "-o", "short-iso"])
  if code != 0:
    return (
      [Alert("kernel:oom:unreadable", "warning", "cannot read kernel log via journalctl -k")],
      since,
    )

  hits = [ln for ln in out.splitlines() if OOM_PATTERN.search(ln)]
  if not hits:
    return [], now
  detail = "; ".join(ln.split("kernel:", 1)[-1].strip()[:160] for ln in hits[-3:])
  return (
    [
      Alert(
        f"kernel:oom:{hits[-1][:19]}",
        "critical",
        f"kernel OOM-killed {len(hits)} process(es): {detail}",
      )
    ],
    now,
  )


# Key prefix -> lane. One table, so the routing is auditable in one read and a
# new check cannot quietly invent a seventh destination. Longest match wins, so
# `media:low-space` can differ from `media:` without reordering anything.
#
# The rule behind the assignments: nas-critical is "the box or something a human
# is looking at right now is broken"; nas-attention is "needs a human today";
# nas-infra is "routine, self-healing, or a recovery". ADR-0033.
LANE_BY_KEY_PREFIX: tuple[tuple[str, str], ...] = (
  # --- the box itself ---
  ("kernel:oom:unreadable", "attention"),  # the OOM detector is blind, not the box
  ("kernel:oom:", "critical"),
  ("media:unmounted", "critical"),
  ("media:readonly", "critical"),  # ext4 remounted ro: every *arr import now fails
  ("media:ext4-errors", "critical"),
  ("media:ext4-state", "critical"),
  ("media:ext4-unreadable", "infra"),
  ("media:kernel-errors", "critical"),
  ("media:low-space", "attention"),
  # --- containers. `missing` is ADR-0006's failure mode: not unhealthy, ABSENT.
  # The others start in nas-infra and escalate by age; see escalate_lane().
  ("container:", "infra"),
  # --- supervision ---
  ("autoheal:supervising-nothing", "infra"),
  ("autoheal:", "attention"),  # nothing is restarting anything, and that is quiet
  # --- scheduling. A job that failed once is cron_job.py's business (nas-infra);
  # a job that stopped running AT ALL is this file's, and is a today problem.
  ("cron:", "attention"),
  ("crontab:", "attention"),
  # --- the *arr suite ---
  ("arr:", "attention"),
  ("prowlarr:indexer:", "attention"),
  # --- jellyfin ---
  ("jellyfin:memory:high", "attention"),
  ("jellyfin:sampler:unparsed", "infra"),
  ("jellyfin:sampler:", "attention"),  # a dead sampler blinds the OOM check
  # --- standing configuration gaps: they cannot resolve on their own, so they
  # nag once a day in the lane you read rather than the one that wakes you.
  ("heartbeat:unconfigured", "infra"),
  ("wan:shaper:degraded", "attention"),  # degraded uplink = 5% loss (AGENTS.md)
  ("watchdog:self-test", "infra"),
)


def _service_of(key: str) -> str:
  """`container:jellyfin:unhealthy` -> `jellyfin`. Empty for anything else."""
  parts = key.split(":")
  return parts[1] if len(parts) >= 3 and parts[0] == "container" else ""


def escalate_lane(key: str, base_lane: str, active_min: float) -> str:
  """Raise a container alert's lane as the outage ages. Pure.

  A container that has been unhealthy for ninety seconds is a blip and belongs
  in nas-infra; the same container ninety minutes later is not. Two thresholds,
  both from the routing table:

  * any container still unhealthy after ESCALATE_ATTENTION_MIN -> nas-attention
  * a USER_VISIBLE_SERVICES container down past USER_VISIBLE_CRITICAL_MIN ->
    nas-critical, because by then someone has already noticed and is asking

  `missing` skips the ladder entirely: no container at all is the ADR-0006
  failure mode that cost qBittorrent thirteen hours, and it is critical from the
  first tick.
  """
  if not key.startswith("container:"):
    return base_lane
  if key.endswith(":missing"):
    return "critical"
  service = _service_of(key)
  if key.endswith(":unhealthy") and service in SLOW_UNHEALTHY_SERVICES:
    return base_lane
  if service in USER_VISIBLE_SERVICES and active_min >= USER_VISIBLE_CRITICAL_MIN:
    return "critical"
  if active_min >= ESCALATE_ATTENTION_MIN:
    return "attention"
  return base_lane


def lane_for(alert: Alert, active_min: float = 0.0) -> str:
  """The lane this alert belongs in, given how long it has been active. Pure."""
  if alert.lane:
    return alert.lane
  base = "attention"
  best = -1
  for prefix, lane in LANE_BY_KEY_PREFIX:
    if alert.key.startswith(prefix) and len(prefix) > best:
      base, best = lane, len(prefix)
  return escalate_lane(alert.key, base, active_min)


def notify(alert: Alert, resolved: bool = False, active_min: float = 0.0) -> bool:
  """Publish one alert through the lane router. Never raises.

  Was a direct `POST <topic-url>` against `NAS_ALERT_WEBHOOK`. It now goes
  through `scripts/notify.py` so this file holds no topic name, no credential
  and no priority mapping -- and so a recovery lands in nas-infra at priority 2
  instead of shouting as loudly as the failure did (ADR-0033).

  Cooldown is deliberately NOT used here: this file already has its own dedupe
  (`active` in the state file, plus `--repeat-min` and per-alert `repeat_min`),
  and stacking a second suppression window on top of it would make "why did I
  not get told" un-debuggable.
  """
  if resolved:
    return bool(notifier.resolved(alert.key, alert.message).sent)
  lane = lane_for(alert, active_min)
  priority = SEVERITY_PRIORITY.get(alert.severity)
  # Only override the lane's own priority when the severity is *worse* than the
  # lane's default. A `notice` in nas-critical must still be priority 5.
  lane_default = notifier.LANES[notifier.Lane(lane)].priority
  if priority is None or priority < lane_default:
    priority = None
  return bool(notifier.notify(lane, alert.key, alert.message, priority=priority).sent)


def load_state(path: Path | None) -> dict:
  if path is None or not path.exists():
    return {}
  try:
    data = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError):
    return {}
  return data if isinstance(data, dict) else {}


def save_state(path: Path | None, state: dict) -> None:
  if path is None:
    return
  try:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=1, sort_keys=True))
  except OSError as exc:
    print(f"WARNING: could not write state file {path}: {exc}", file=sys.stderr)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Alert on container failures, Jellyfin memory growth and kernel OOM kills.",
  )
  parser.add_argument("--project", default="nas", help="Compose project name (default: nas).")
  parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="State file path.")
  parser.add_argument("--mem-log", type=Path, default=DEFAULT_MEM_LOG, help="Sampler log path.")
  parser.add_argument(
    "--cron-state-dir",
    type=Path,
    default=DEFAULT_CRON_STATE,
    help="Directory of cron_job.py state files to check for staleness.",
  )
  parser.add_argument(
    "--jellyfin-anon-mb",
    type=float,
    default=DEFAULT_JELLYFIN_ANON_MB,
    help=f"Alert when jellyfin anon-RSS reaches this (default {DEFAULT_JELLYFIN_ANON_MB:.0f}MB).",
  )
  parser.add_argument(
    "--sampler-stale-min",
    type=float,
    default=DEFAULT_SAMPLER_STALE_MIN,
    help=f"Alert if the sampler log is older than this (default {DEFAULT_SAMPLER_STALE_MIN:.0f}min).",
  )
  parser.add_argument(
    "--repeat-min",
    type=float,
    default=DEFAULT_REPEAT_MINUTES,
    help=f"Re-notify a still-active alert this often (default {DEFAULT_REPEAT_MINUTES:.0f}min).",
  )
  parser.add_argument(
    "--ignore",
    action="append",
    default=[],
    metavar="SERVICE",
    help="Compose service to skip entirely (repeatable).",
  )
  parser.add_argument(
    "--starting-max-min",
    type=float,
    default=150.0,
    metavar="MIN",
    help=(
      "Alert when a container has been health=starting this long (default 150). "
      "Must exceed the longest legitimate start_period in the stack -- slskd's "
      "is 4h for a cold share scan, and inside start_period Docker counts no "
      "failures, so this is the only thing watching that window (ADR-0026)."
    ),
  )
  parser.add_argument(
    "--prowlarr-url",
    default=DEFAULT_PROWLARR_URL,
    help=f"Prowlarr base URL for the indexer check (default {DEFAULT_PROWLARR_URL}).",
  )
  parser.add_argument(
    "--indexer-down-min",
    type=float,
    default=PROWLARR_INDEXER_DOWN_MIN,
    metavar="MIN",
    help=(
      "How long an indexer must stay failed before alerting "
      f"(default {PROWLARR_INDEXER_DOWN_MIN:.0f}). Below this it is treated as "
      "flapping, which is the normal condition of a public tracker."
    ),
  )
  parser.add_argument("--dry-run", action="store_true", help="Print alerts, send nothing.")
  parser.add_argument(
    "--self-test",
    action="store_true",
    help="Send one test notification through the configured webhook and exit.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)

  if args.self_test:
    probe = Alert("watchdog:self-test", "info", "stack_watchdog self-test — delivery is working.")
    if not os.getenv("NTFY_TOKEN_SCRIPTS"):
      print("ERROR: no NTFY_TOKEN_SCRIPTS set", file=sys.stderr)
      return 2
    ok = notify(probe)
    print("self-test notification sent" if ok else "self-test notification FAILED")
    return 0 if ok else 2

  containers = inspect_containers(args.project)
  if containers is None:
    print("ERROR: cannot inspect docker containers", file=sys.stderr)
    return 2

  state = load_state(args.state)
  services = defined_services()
  if services is None:
    print("WARNING: `docker compose config --services` failed; checking running containers only", file=sys.stderr)

  alerts, restarts = check_containers(
    services, containers, state.get("restart_counts", {}), set(args.ignore)
  )
  if "autoheal" not in set(args.ignore):
    alerts += check_autoheal(containers, autoheal_logs())
  alerts += check_cron_jobs(args.cron_state_dir)
  # Damped on our side of the webhook: only indexers that stayed failed past
  # --indexer-down-min, so a public-tracker flap is silent. See the check's
  # docstring for why Prowlarr's own filtering is not used.
  alerts += check_indexer_failures(
    fetch_indexer_failures(args.prowlarr_url, os.getenv("API_KEY_PROWLARR", "")),
    args.indexer_down_min,
  )
  # Owns the *arr health warnings so their own unfiltered onHealthIssue
  # connections can be switched off; IndexerStatusCheck is dropped here because
  # check_indexer_failures above covers indexer state without the flapping.
  alerts += check_arr_health(fetch_arr_health())
  alerts += check_heartbeat_configured()
  alerts += check_wan_shaper()
  alerts += check_media_storage()
  # Observation only -- see the docstring for why this is not a restart.
  alerts += check_stuck_starting(containers, args.starting_max_min)
  alerts += lint_crontab(read_crontab(), REPO_ROOT)
  alerts += check_jellyfin_memory(args.mem_log, args.jellyfin_anon_mb, args.sampler_stale_min)
  oom_alerts, oom_cursor = check_kernel_oom(state.get("oom_cursor"))
  alerts += oom_alerts

  now = time.time()
  active = state.get("active", {})
  still: dict[str, dict] = {}

  for alert in alerts:
    prior = active.get(alert.key)
    last = prior.get("last_notified", 0) if prior else 0
    first_seen = (prior or {}).get("first_seen", now)
    active_min = (now - first_seen) / 60.0
    lane = lane_for(alert, active_min)
    print(f"[{alert.severity.upper()}] {alert.key} -> nas-{lane}: {alert.message}")
    repeat_min = alert.repeat_min if alert.repeat_min is not None else args.repeat_min
    due = (now - last) / 60.0 >= repeat_min
    # INVARIANT: an ESCALATION must not wait for --repeat-min. A container that
    # starts unhealthy lands in nas-infra; fifteen minutes later it belongs in
    # nas-attention and, if it is user-visible, five minutes in it belongs in
    # nas-critical. Without this clause the escalation would be recorded in the
    # state file and never actually sent, because `due` is False for another
    # 45 minutes -- the alert would sit in the quietest lane for the whole
    # outage. ADR-0033.
    escalated = prior is not None and prior.get("lane") not in (None, lane)
    if not args.dry_run and (prior is None or due or escalated):
      last = now if notify(alert, active_min=active_min) else last
    still[alert.key] = {"first_seen": first_seen, "last_notified": last, "lane": lane}

  for key, meta in active.items():
    if key in still or key.startswith("kernel:oom:"):
      continue
    down_min = (now - meta.get("first_seen", now)) / 60.0
    print(f"[RESOLVED] {key} (was active {down_min:.0f} min)")
    if not args.dry_run:
      notify(Alert(key, "info", f"cleared after {down_min:.0f} min"), resolved=True)

  # INVARIANT: --dry-run must not touch the state file. It used to, and that is
  # how a recovery gets lost: an ad-hoc run loads `active`, sees the problem is
  # over, prints [RESOLVED], skips the push because of --dry-run, and then saves
  # the pruned state anyway. The pending resolve is consumed, the next cron run
  # has nothing left to announce, and the alert stays open on the phone forever.
  # Observed 2026-09-02 20:15 -> 20:20: autoheal:down, container:autoheal:down
  # and container:slskd:unhealthy all vanished between two ticks with no
  # [RESOLVED] line and no ntfy message, which is why the 2026-09-03 triage
  # still listed all three as down hours after they had recovered.
  if not args.dry_run:
    save_state(
      args.state,
      {"active": still, "restart_counts": restarts, "oom_cursor": oom_cursor},
    )

  if not alerts:
    print(f"OK: {len(containers)} containers, no alerts")
  return 1 if alerts else 0


if __name__ == "__main__":
  sys.exit(main())
