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

Delivery
--------
ntfy, over a plain `POST <topic-url>` with a text body. Chosen over Gotify
because it needs no server-side setup before the first alert can land, the
same one-call contract works against ntfy.sh or a self-hosted instance with
only a URL change, and this repo already speaks it (`SLSKD_ALERT_WEBHOOK` in
scripts/slskd_login_watch.py). Gotify would require standing up a server and
minting per-application tokens first.

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
  NAS_ALERT_WEBHOOK      ntfy topic URL to POST alerts to. Falls back to
                         SLSKD_ALERT_WEBHOOK so one topic can serve both.
                         Unset = print only (still useful in cron mail).
  NAS_ALERT_USER         basic-auth user for the ntfy topic. The self-hosted
  NAS_ALERT_PASSWORD     instance runs auth-default-access=deny-all, so these
                         are required against it (optional for an open topic).

Usage
-----
  python scripts/stack_watchdog.py
  python scripts/stack_watchdog.py --state logs/stack_watchdog.json
  python scripts/stack_watchdog.py --jellyfin-anon-mb 4096 --repeat-min 60
  python scripts/stack_watchdog.py --self-test    # send one test notification
"""

from __future__ import annotations

import argparse
import base64
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

if "NAS_ALERT_WEBHOOK" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


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
SEVERITY_PRIORITY = {"critical": "urgent", "warning": "high", "notice": "low", "info": "default"}
# A standing configuration gap is not an incident: it cannot resolve on its own
# and re-reading it every hour teaches you to ignore the topic. Once a day is
# enough to keep it from being forgotten, which is the only job it has.
CONFIG_GAP_REPEAT_MIN = 1440.0


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
  """

  key: str
  severity: str
  message: str
  repeat_min: float | None = None


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
    if age_min <= max_age:
      continue
    if state.get("last_success"):
      what = f"last succeeded {age_min / 60:.1f}h ago"
    else:
      what = f"has never succeeded (registered {age_min / 60:.1f}h ago)"
    detail = f"; last exit {state['last_exit']}" if "last_exit" in state else "; it has not run at all"
    alerts.append(
      Alert(
        f"cron:{path.stem}:stale",
        "critical",
        f"cron job {path.stem} {what}, allowed {max_age / 60:.1f}h{detail}",
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


def check_media_storage(mount: str = "/mnt/drive", min_free_gb: float = 100.0) -> list[Alert]:
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


def _auth_header() -> dict[str, str]:
  """Basic-auth header for ntfy, when credentials are configured.

  The ntfy instance runs `auth-default-access=deny-all`, so an unauthenticated
  publish is rejected with 403 — the credentials are not optional in practice,
  but the script still works against an open topic without them.
  """
  user = os.getenv("NAS_ALERT_USER", "")
  password = os.getenv("NAS_ALERT_PASSWORD", "")
  if not user or not password:
    return {}
  token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
  return {"Authorization": f"Basic {token}"}


def notify(webhook: str, alert: Alert, resolved: bool = False) -> bool:
  """POST one alert to ntfy. Returns False on failure; never raises."""
  title = ("RESOLVED: " if resolved else "") + alert.key
  priority = "default" if resolved else SEVERITY_PRIORITY.get(alert.severity, "default")
  if resolved:
    tags = "white_check_mark"
  else:
    tags = {"critical": "rotating_light,skull", "warning": "warning"}.get(alert.severity, "information_source")
  req = urllib.request.Request(
    webhook,
    data=alert.message.encode("utf-8"),
    method="POST",
    headers={"Title": title, "Priority": priority, "Tags": tags, **_auth_header()},
  )
  try:
    urllib.request.urlopen(req, timeout=15).close()  # noqa: S310 - operator-supplied URL
  except (OSError, ValueError) as exc:
    print(f"WARNING: alert POST failed: {exc}", file=sys.stderr)
    return False
  return True


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
  parser.add_argument("--dry-run", action="store_true", help="Print alerts, send nothing.")
  parser.add_argument(
    "--self-test",
    action="store_true",
    help="Send one test notification through the configured webhook and exit.",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  webhook = os.getenv("NAS_ALERT_WEBHOOK") or os.getenv("SLSKD_ALERT_WEBHOOK") or ""

  if args.self_test:
    probe = Alert("watchdog:self-test", "info", "stack_watchdog self-test — delivery is working.")
    if not webhook:
      print("ERROR: no NAS_ALERT_WEBHOOK / SLSKD_ALERT_WEBHOOK set", file=sys.stderr)
      return 2
    ok = notify(webhook, probe)
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
  alerts += check_heartbeat_configured()
  alerts += check_wan_shaper()
  alerts += check_media_storage()
  alerts += lint_crontab(read_crontab(), REPO_ROOT)
  alerts += check_jellyfin_memory(args.mem_log, args.jellyfin_anon_mb, args.sampler_stale_min)
  oom_alerts, oom_cursor = check_kernel_oom(state.get("oom_cursor"))
  alerts += oom_alerts

  now = time.time()
  active = state.get("active", {})
  still: dict[str, dict] = {}

  for alert in alerts:
    print(f"[{alert.severity.upper()}] {alert.key}: {alert.message}")
    prior = active.get(alert.key)
    last = prior.get("last_notified", 0) if prior else 0
    repeat_min = alert.repeat_min if alert.repeat_min is not None else args.repeat_min
    due = (now - last) / 60.0 >= repeat_min
    if webhook and not args.dry_run and (prior is None or due):
      last = now if notify(webhook, alert) else last
    still[alert.key] = {"first_seen": (prior or {}).get("first_seen", now), "last_notified": last}

  for key, meta in active.items():
    if key in still or key.startswith("kernel:oom:"):
      continue
    down_min = (now - meta.get("first_seen", now)) / 60.0
    print(f"[RESOLVED] {key} (was active {down_min:.0f} min)")
    if webhook and not args.dry_run:
      notify(webhook, Alert(key, "info", f"cleared after {down_min:.0f} min"), resolved=True)

  save_state(
    args.state,
    {"active": still, "restart_counts": restarts, "oom_cursor": oom_cursor},
  )

  if not alerts:
    print(f"OK: {len(containers)} containers, no alerts")
  return 1 if alerts else 0


if __name__ == "__main__":
  sys.exit(main())
