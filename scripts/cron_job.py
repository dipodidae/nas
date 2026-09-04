#!/usr/bin/env python3
"""Run a cron job so that its failure — or its silence — is loud.

Why this exists
---------------
Two jobs on this box died and nobody noticed for months: `autoheal` (stopped
2026-07-29, found 2026-09-01) and `media_ops_status.py` (crontab line ran
`.venv/bin/python` with no `cd`, so from cron's `$HOME` it resolved to nothing;
the ops dashboard served 2026-06-10 data for three months). Auditing the
crontab for that *specific* bug found no others, but that only covers jobs that
fail to start. A job that runs and exits fatal every time is equally invisible,
and a job whose cron line never fires produces no output at all — not even
stderr to notice.

So this wraps a job and closes both halves:

* **Failure is pushed, in the lane that failure deserves.** A fatal exit sends
  an ntfy alert naming the job, its exit code and the tail of its stderr. A job
  that keeps failing re-pushes at `--alert-repeat-min` rather than on every run,
  so one broken `*/5` job is one alert an hour instead of twelve.

  The *first* failure goes to `nas-infra`: most of them are transient and
  self-heal on the next tick, and paging for those is precisely how an alert
  feed gets muted. A **second consecutive** failure goes to `nas-attention` --
  twice in a row is no longer a blip. Jobs whose failure is a real incident
  regardless (`config-backup`, `offsite-backup`) pass `--fail-lane critical`
  and skip the ladder: an unnoticed backup failure is only discovered when you
  need the backup. ADR-0033.
* **Recovery is pushed too**, always to `nas-infra` at priority 2. When a job
  that was failing exits cleanly again it sends `RESOLVED: cron:<name>:failed`,
  symmetric with the `:stale` keys `stack_watchdog` already resolves. Without it every transient cron failure
  stayed open forever, and an open alert that cannot close is indistinguishable
  from a live one.
* **Silence is pushed too.** Each run records a state file under
  `logs/cron-state/`; `stack_watchdog.py` alerts when a job has not *succeeded*
  within the `--max-age-min` the cron line itself declares. That is the half
  that would have caught `media_ops_status.py`, which was failing at launch and
  therefore producing nothing to notice.

`--register` writes the state file without running anything, so a job that has
never run once is still watched: the watchdog measures staleness from the
registration time until the first success replaces it.

**Exit codes are not simply "0 good, non-zero bad" here.** This repo's scripts
use 0 success / 1 partial / 2 fatal (see AGENTS.md), and several report a
genuine finding as 1 — `slskd_login_watch.py` exits 1 for "logged out",
`stack_watchdog.py` exits 1 for "an alert is active", `media_ops_status.py`
exits 1 for DEGRADED. Treating those as failures would produce constant false
alarms, so `--ok-codes` defaults to `0,1` and jobs with a different contract
(plain shell commands, say) pass `--ok-codes 0`.

The wrapper exits 0 when the job's code was acceptable, and re-raises the
job's own code otherwise, so cron's own mail stays quiet for expected partials
and still fires for real failures. Job stdout and stderr are passed through
unchanged, so existing `>> logs/x.log 2>&1` redirects keep working.

**Wrap inside `flock`, not outside.** `flock -n` exits 1 without running
anything when the lock is held; outside the wrapper that would be recorded as
a successful run and would refresh the freshness clock for a job that never
executed.

Exit codes
----------
  0  the job's exit code was in --ok-codes (or --register succeeded)
  N  the job's own exit code, when it was not

Environment
-----------
  NTFY_TOKEN_SCRIPTS / NTFY_URL / NTFY_TOPIC_<LANE>   as stack_watchdog.py

Usage
-----
  python scripts/cron_job.py --name media-ops-status --max-age-min 30 -- \
      .venv/bin/python scripts/media_ops_status.py --json-out /path/out.json
  python scripts/cron_job.py --name docker-prune --max-age-min 10380 --ok-codes 0 -- \
      /usr/bin/docker image prune -f
  python scripts/cron_job.py --name album-art --max-age-min 10380 --register
  python scripts/cron_job.py --name config-backup --max-age-min 1560 \
      --fail-lane critical -- python scripts/config_backup.py ...
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

if "NTFY_TOKEN_SCRIPTS" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

# Publish through the same lane router everything else uses. Imported by path
# because this file is executed as a script, not as part of a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify as notifier  # noqa: E402, I001


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "logs" / "cron-state"
DEFAULT_OK_CODES = "0,1"
# Matches stack_watchdog's --repeat-min default: one broken thing should sound
# the same whichever half of the system noticed it.
DEFAULT_ALERT_REPEAT_MIN = 60.0
STDERR_TAIL_LINES = 12
STDERR_ALERT_CHARS = 600
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# A first failure is usually transient; a second consecutive one is not.
FIRST_FAILURE_LANE = "infra"
REPEAT_FAILURE_LANE = "attention"
# How often to re-stamp `in_flight_heartbeat` while a job is still running.
# stack_watchdog stops believing the marker after IN_FLIGHT_STALE_MIN (15min),
# so this must be comfortably below that; 60s is 15 beats of headroom.
IN_FLIGHT_HEARTBEAT_SEC = 60.0


def state_path(name: str) -> Path:
  return STATE_DIR / f"{name}.json"


def load_state(name: str) -> dict:
  try:
    data = json.loads(state_path(name).read_text())
  except (OSError, json.JSONDecodeError):
    return {}
  return data if isinstance(data, dict) else {}


def save_state(name: str, state: dict) -> None:
  try:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state_path(name).write_text(json.dumps(state, indent=1, sort_keys=True))
  except OSError as exc:
    print(f"WARNING: could not write cron state for {name}: {exc}", file=sys.stderr)


def parse_ok_codes(raw: str) -> set[int]:
  return {int(part) for part in raw.split(",") if part.strip()}


def tail(text: str, lines: int = STDERR_TAIL_LINES) -> str:
  kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
  return "\n".join(kept)


def failure_lane(pinned: str | None, consecutive_failures: int) -> str:
  """Which lane this failure belongs in. Pure, so the ladder is testable.

  `--fail-lane` wins outright: for `config-backup` the first failure already
  is the incident, because a backup failure is only ever discovered when the
  backup is needed. Otherwise a single failure is nas-infra (most self-heal on
  the next tick) and a second consecutive one is nas-attention.
  """
  if pinned:
    return pinned
  return FIRST_FAILURE_LANE if consecutive_failures <= 1 else REPEAT_FAILURE_LANE


def _heartbeat_loop(name: str, state: dict, stop: threading.Event) -> None:
  """Re-stamp the in-flight marker until `stop` is set."""
  while not stop.wait(IN_FLIGHT_HEARTBEAT_SEC):
    state["in_flight_heartbeat"] = time.time()
    save_state(name, state)


def run_job(command: list[str], name: str | None = None, state: dict | None = None) -> tuple[int, str]:
  """Run the job, passing output through. Returns (exit code, stderr text).

  While the job runs, `in_flight_heartbeat` is re-stamped every
  IN_FLIGHT_HEARTBEAT_SEC so `stack_watchdog.check_cron_jobs` can tell a job
  that is *slow* from one that has gone *quiet*. Without it, a job whose single
  pass outlives its own `--max-age-min` is indistinguishable from a job whose
  cron line is broken: `last_success` is only written when the child exits, so
  the freshness clock cannot advance while the work is happening.
  `playlist-sync` sat 21h into a genuinely progressing run and was paged as
  stale for every one of them.

  The heartbeat is deliberately a repeated stamp rather than a single "running"
  flag: a job killed with -9 never gets to clear its own marker, and a flag that
  can only be set would silence the staleness check permanently.
  """
  stop = threading.Event()
  beater: threading.Thread | None = None
  if name and state is not None:
    now = time.time()
    state["in_flight_since"] = now
    state["in_flight_heartbeat"] = now
    save_state(name, state)
    beater = threading.Thread(target=_heartbeat_loop, args=(name, state, stop), daemon=True)
    beater.start()
  try:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
  except OSError as exc:
    # The command itself could not be executed at all — the `media_ops_status`
    # failure mode. There is no stderr from the job because there was no job.
    return 127, f"could not execute {command[0]!r}: {exc}"
  finally:
    stop.set()
    if beater is not None:
      beater.join(timeout=5)
    if state is not None:
      state.pop("in_flight_since", None)
      state.pop("in_flight_heartbeat", None)
  if proc.stdout:
    sys.stdout.write(proc.stdout)
  if proc.stderr:
    sys.stderr.write(proc.stderr)
  return proc.returncode, proc.stderr or ""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Run a cron job, record its freshness, and push its failures.",
  )
  parser.add_argument("--name", required=True, help="Short job id (kebab-case); names the state file.")
  parser.add_argument(
    "--max-age-min",
    type=float,
    required=True,
    help="How long this job may go without succeeding before the watchdog alerts.",
  )
  parser.add_argument(
    "--ok-codes",
    default=DEFAULT_OK_CODES,
    help=f"Exit codes that count as success (default {DEFAULT_OK_CODES} — 0 ok, 1 partial).",
  )
  parser.add_argument(
    "--alert-repeat-min",
    type=float,
    default=DEFAULT_ALERT_REPEAT_MIN,
    help=(
      "Minutes before a still-failing job re-pushes its alert "
      f"(default {DEFAULT_ALERT_REPEAT_MIN:.0f}; 0 = push every failing run)."
    ),
  )
  parser.add_argument(
    "--fail-lane",
    default=None,
    metavar="LANE",
    help=(
      "Pin the lane a failure of THIS job publishes to, skipping the "
      f"{FIRST_FAILURE_LANE} -> {REPEAT_FAILURE_LANE} ladder. Use "
      "`critical` for a job whose failure is an incident on the first run "
      "(config-backup, offsite-backup)."
    ),
  )
  parser.add_argument(
    "--register",
    action="store_true",
    help="Write the state file without running anything, so a never-yet-run job is still watched.",
  )
  parser.add_argument(
    "--lock",
    help=(
      "Hold an exclusive lock on this path for the duration of the job. Use this "
      "instead of wrapping the line in `flock -n`: an external flock exits 1 "
      "WITHOUT running anything, so cron_job never starts and the skip leaves no "
      "trace at all -- a skipped run is then indistinguishable from a clean one."
    ),
  )
  parser.add_argument(
    "--lock-wait",
    type=float,
    default=0.0,
    help="Seconds to wait for --lock before giving up (default 0 = don't wait).",
  )
  parser.add_argument(
    "--max-skips",
    type=int,
    default=3,
    help=(
      "Alert after this many CONSECUTIVE lock skips (default 3). One skip is "
      "contention; several in a row means the holder is starving this job."
    ),
  )
  parser.add_argument("command", nargs=argparse.REMAINDER, help="-- followed by the command to run.")
  return parser.parse_args(argv)


def acquire_lock(path: str, wait_seconds: float):
  """An exclusive flock, or None if it is held. Caller keeps the handle open.

  Deliberately mirrors `flock -n` / `flock -w N`, so migrating a cron line from
  an external flock to `--lock` does not change the contention behaviour -- only
  whether the skip is recorded.
  """
  handle = open(path, "a+")  # noqa: SIM115 - must outlive this function
  deadline = time.monotonic() + max(0.0, wait_seconds)
  while True:
    try:
      fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
      return handle
    except OSError:
      if time.monotonic() >= deadline:
        handle.close()
        return None
      time.sleep(0.25)


def record_skip(name: str, state: dict, lock: str, max_skips: int) -> int:
  """Count a lock skip, and alert once it stops looking like ordinary contention.

  Exits 0 either way: a skip is not a failure of this job, it is a report about
  another job holding the lock too long. Five jobs share
  /tmp/nas-tubifarry-cleanup.lock and the 05:30 one takes it with `flock -w 600`,
  so it can hold the lock straight through the :37 window.
  """
  skips = int(state.get("consecutive_lock_skips") or 0) + 1
  state["consecutive_lock_skips"] = skips
  state["last_lock_skip"] = time.time()
  save_state(name, state)
  print(f"--- {name} SKIPPED: lock {lock} is held (consecutive skips: {skips})")
  if skips >= max_skips:
    notifier.notify(
      "attention",
      f"cron:{name}:lock-starved",
      f"{name} has skipped {skips} consecutive runs waiting on {lock}. "
      "Another holder is starving it; a skipped run produces no output, so this "
      "would otherwise look exactly like a healthy quiet period.",
    )
  return 0


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  if not NAME_PATTERN.match(args.name):
    print(f"ERROR: --name {args.name!r} must be kebab-case [a-z0-9-]", file=sys.stderr)
    return 2

  now = time.time()
  state = load_state(args.name)
  state.setdefault("registered", now)
  state["max_age_min"] = args.max_age_min

  if args.register:
    save_state(args.name, state)
    print(f"registered cron job {args.name!r} (max age {args.max_age_min:.0f} min)")
    return 0

  command = [c for c in args.command if c != "--"]
  if not command:
    print("ERROR: no command given (put it after --)", file=sys.stderr)
    return 2

  lock_handle = None
  if args.lock:
    lock_handle = acquire_lock(args.lock, args.lock_wait)
    if lock_handle is None:
      return record_skip(args.name, state, args.lock, args.max_skips)
    # Reaching the job means contention (if any) resolved; the streak is over.
    if state.get("consecutive_lock_skips"):
      print(f"--- {args.name} lock acquired after {state['consecutive_lock_skips']} skip(s)")
    state["consecutive_lock_skips"] = 0

  ok_codes = parse_ok_codes(args.ok_codes)
  started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
  print(f"--- {args.name} start {started}")
  code, stderr_text = run_job(command, args.name, state)
  elapsed = time.time() - now
  print(f"--- {args.name} exit={code} in {elapsed:.1f}s")

  state["last_run"] = now
  state["last_exit"] = code
  state["last_duration_s"] = round(elapsed, 1)

  key = f"cron:{args.name}:failed"
  # Was this job in a failed state before this run? `failing_since` is the
  # marker: it is set on the first failing run and only cleared by a success.
  # It is what makes the resolve below symmetric with the alert above, rather
  # than inferring "was failing" from `last_exit`, which a --register call or a
  # hand-edited state file could quietly lose.
  failing_since = float(state.get("failing_since") or 0)

  if code in ok_codes:
    state["last_success"] = now
    state.pop("last_error", None)
    state.pop("failing_since", None)
    state.pop("last_failure_notified", None)
    state.pop("last_failure_lane", None)
    state.pop("consecutive_failures", None)
    save_state(args.name, state)
    # Close the alert. Without this every transient cron failure stays open
    # forever and the topic stops distinguishing live problems from old ones —
    # `stack_watchdog` already does exactly this for its own `:stale` keys.
    if failing_since:
      msg = f"cleared after {(now - failing_since) / 60.0:.0f} min (exit {code})"
      print(f"[RESOLVED] {key}: {msg}")
      notifier.resolved(key, msg)
    return 0

  state["last_error"] = tail(stderr_text)
  state.setdefault("failing_since", now)
  state["consecutive_failures"] = int(state.get("consecutive_failures") or 0) + 1
  detail = tail(stderr_text)[-STDERR_ALERT_CHARS:] or "(no stderr)"
  lane = failure_lane(args.fail_lane, state["consecutive_failures"])
  message = f"exit {code} after {elapsed:.0f}s\n{detail}"
  # Re-push a *continuing* failure at the same slow interval the watchdog uses,
  # not on every run: a job on a */5 schedule would otherwise push 12 times an
  # hour for one broken thing. An ESCALATION is exempt -- the same reasoning as
  # stack_watchdog's: a first failure that lands in nas-infra and then becomes a
  # repeat failure must actually reach nas-attention, not wait out the window in
  # the quiet lane.
  last_notified = float(state.get("last_failure_notified", 0) or 0)
  escalated = state.get("last_failure_lane") not in (None, lane)
  if (now - last_notified) / 60.0 >= args.alert_repeat_min or escalated:
    print(f"ALERT nas-{lane}: {key}: {message}", file=sys.stderr)
    if notifier.notify(lane, key, message).sent:
      state["last_failure_notified"] = now
      state["last_failure_lane"] = lane
  save_state(args.name, state)
  return code


if __name__ == "__main__":
  sys.exit(main())
