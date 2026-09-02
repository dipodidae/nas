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

* **Failure is pushed.** A fatal exit sends an ntfy alert naming the job, its
  exit code and the tail of its stderr. A job that keeps failing re-pushes at
  `--alert-repeat-min` rather than on every run, so one broken `*/5` job is one
  alert an hour instead of twelve.
* **Recovery is pushed too.** When a job that was failing exits cleanly again
  it sends `RESOLVED: cron:<name>:failed`, symmetric with the `:stale` keys
  `stack_watchdog` already resolves. Without it every transient cron failure
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
  NAS_ALERT_WEBHOOK / NAS_ALERT_USER / NAS_ALERT_PASSWORD   as stack_watchdog.py

Usage
-----
  python scripts/cron_job.py --name media-ops-status --max-age-min 30 -- \
      .venv/bin/python scripts/media_ops_status.py --json-out /path/out.json
  python scripts/cron_job.py --name docker-prune --max-age-min 10380 --ok-codes 0 -- \
      /usr/bin/docker image prune -f
  python scripts/cron_job.py --name album-art --max-age-min 10380 --register
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

if "NAS_ALERT_WEBHOOK" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

# Reuse the watchdog's ntfy delivery rather than duplicating it: one place
# builds the auth header and maps severity to priority. Imported by path
# because this file is executed as a script, not as part of a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stack_watchdog import Alert, notify  # noqa: E402, I001


REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "logs" / "cron-state"
DEFAULT_OK_CODES = "0,1"
# Matches stack_watchdog's --repeat-min default: one broken thing should sound
# the same whichever half of the system noticed it.
DEFAULT_ALERT_REPEAT_MIN = 60.0
STDERR_TAIL_LINES = 12
STDERR_ALERT_CHARS = 600
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


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


def run_job(command: list[str]) -> tuple[int, str]:
  """Run the job, passing output through. Returns (exit code, stderr text)."""
  try:
    proc = subprocess.run(command, capture_output=True, text=True, check=False)
  except OSError as exc:
    # The command itself could not be executed at all — the `media_ops_status`
    # failure mode. There is no stderr from the job because there was no job.
    return 127, f"could not execute {command[0]!r}: {exc}"
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
    "--register",
    action="store_true",
    help="Write the state file without running anything, so a never-yet-run job is still watched.",
  )
  parser.add_argument("command", nargs=argparse.REMAINDER, help="-- followed by the command to run.")
  return parser.parse_args(argv)


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

  ok_codes = parse_ok_codes(args.ok_codes)
  started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
  print(f"--- {args.name} start {started}")
  code, stderr_text = run_job(command)
  elapsed = time.time() - now
  print(f"--- {args.name} exit={code} in {elapsed:.1f}s")

  state["last_run"] = now
  state["last_exit"] = code
  state["last_duration_s"] = round(elapsed, 1)

  webhook = os.getenv("NAS_ALERT_WEBHOOK") or os.getenv("SLSKD_ALERT_WEBHOOK") or ""
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
    save_state(args.name, state)
    # Close the alert. Without this every transient cron failure stays open
    # forever and the topic stops distinguishing live problems from old ones —
    # `stack_watchdog` already does exactly this for its own `:stale` keys.
    if failing_since:
      msg = f"cleared after {(now - failing_since) / 60.0:.0f} min (exit {code})"
      print(f"[RESOLVED] {key}: {msg}")
      if webhook:
        notify(webhook, Alert(key, "info", msg), resolved=True)
    return 0

  state["last_error"] = tail(stderr_text)
  state.setdefault("failing_since", now)
  detail = tail(stderr_text)[-STDERR_ALERT_CHARS:] or "(no stderr)"
  alert = Alert(key, "critical", f"exit {code} after {elapsed:.0f}s\n{detail}")
  # Re-push a *continuing* failure at the same slow interval the watchdog uses,
  # not on every run: a job on a */5 schedule would otherwise push 12 times an
  # hour for one broken thing.
  last_notified = float(state.get("last_failure_notified", 0) or 0)
  if (now - last_notified) / 60.0 >= args.alert_repeat_min:
    # The print-only path honours the same window as the push path, so what you
    # see in the job log is what the phone would have received.
    if webhook:
      if notify(webhook, alert):
        state["last_failure_notified"] = now
    else:
      print(f"ALERT (no webhook configured): {alert.message}", file=sys.stderr)
      state["last_failure_notified"] = now
  save_state(args.name, state)
  return code


if __name__ == "__main__":
  sys.exit(main())
