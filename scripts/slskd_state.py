#!/usr/bin/env python3
"""Is slskd unreachable because it is BROKEN, or because it is still starting?

Shared by the cron scripts that talk to slskd, because the difference decides
an exit code and therefore whether a human's phone buzzes.

The problem this solves
-----------------------
slskd does not bind :5030 until its share scan finishes, and a full cold scan of
this share takes over two hours (177k files, measured 2026-09-02). That window
is legitimate and declared: `slskd`'s compose healthcheck carries a
`start_period` sized to cover it precisely so `autoheal` does not restart it
mid-scan -- which would mark the scan suspect, force another full rescan on the
next start, and leave slskd permanently never-up (ADR-0009, ADR-0026).

But every slskd-dependent cron job hits "connection refused" for that whole
window and exits 2. `scripts/cron_job.py` treats 2 as fatal, so a routine,
expected, self-resolving two-hour startup produced priority-5 alerts with a
skull on them, every cron tick. An alerting system that cries wolf during normal
operation is worse than no alerting system, because it trains you to swipe the
notification away -- and this stack exists because things went unnoticed.

So: while slskd is demonstrably still initializing, an slskd-dependent job
reports exit 1 ("partial / nothing done, and here is why") instead of 2. Exit 1
is inside `cron_job.py`'s `--ok-codes 0,1` default, so cron stays quiet, the log
still records it, and a genuinely broken slskd still exits 2 and still shouts.

Why not just widen --ok-codes for those jobs
--------------------------------------------
Because that would silence a real failure too. The point is to distinguish, not
to mute.

Exit codes: this is a library, not a script.
"""

from __future__ import annotations

import json
import subprocess

CONTAINER = "slskd"

# Log phrases that prove slskd is mid-initialization rather than wedged. Read
# from the container log because slskd's API is exactly what is unavailable --
# asking the thing that is down whether it is starting cannot work.
_SCANNING_MARKERS = (
    "scanned ",                      # "Scanned 93% of shared directories"
    "starting shared file scan",
    "share scan started",
    "enumerating shared directories",
    "initializing shares",
)


def _docker_json(args: list[str]) -> dict | None:
    try:
        out = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=10, check=True,
        ).stdout
        parsed = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if isinstance(parsed, list):
        return parsed[0] if parsed else None
    return parsed if isinstance(parsed, dict) else None


def health_status(container: str = CONTAINER) -> str | None:
    """'starting' / 'healthy' / 'unhealthy' / None if unknown."""
    info = _docker_json(["inspect", container])
    if not info:
        return None
    health = ((info.get("State") or {}).get("Health") or {})
    status = health.get("Status")
    return str(status).lower() if status else None


def scan_in_progress(container: str = CONTAINER, tail: int = 40) -> bool:
    """True if slskd's recent log shows a share scan still running."""
    try:
        proc = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    text = (proc.stdout + proc.stderr).lower()
    return any(marker in text for marker in _SCANNING_MARKERS)


def is_initializing(container: str = CONTAINER) -> bool:
    """True when slskd being unreachable is EXPECTED rather than a fault.

    Deliberately two independent signals, because each has a hole:

    * `health=starting` alone is not enough. The window can expire while a scan
      is genuinely still running -- exactly what happened on 2026-09-02, when a
      forced rescan outran a 90m start_period and the container flipped to
      `unhealthy` at 93% scanned. Treating that as a fault would have restarted
      it and destroyed two hours of work.
    * the scan log alone is not enough either: a container that has been up for
      days with a stale scan line in its last 40 lines is not starting.

    Either signal is accepted, because the cost of a false "initializing" is one
    downgraded alert on a job that will run again in minutes, while the cost of
    a false "broken" is a restart that makes the situation strictly worse.
    """
    return health_status(container) == "starting" or scan_in_progress(container)


def unreachable_exit_code(container: str = CONTAINER) -> int:
    """2 if slskd looks broken, 1 if it is merely still coming up."""
    return 1 if is_initializing(container) else 2
