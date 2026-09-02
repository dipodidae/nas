#!/usr/bin/env python3
"""Sample Jellyfin container memory usage for leak diagnosis (cron-driven).

v2 (2026-09-01, corrected): the first version tracked cgroup memory.current,
which includes reclaimable page cache from reading media files and is NOT
what killed the process. The kernel's OOM-killer acts on anonymous memory
(anon-rss) — that's the number that actually matters and the only one
directly comparable to the "Killed process ... anon-rss:NNkB" kernel log
lines already analysed in docs/jellyfin-playback-audit.md.

v4 (2026-09-02, ffprobe fan-out test): added a census of live ffmpeg/ffprobe
CHILD processes plus a marker for whether Jellyfin is running a library scan.
The reason: multiple upstream reports (jellyfin/jellyfin#16048, #16549) pin
10.11.x memory blowups on ffprobe fan-out during library scans rather than on a
slow leak in the server process, and the reporters' own mitigation was capping
the parallel scan/extraction thread counts. Those are two different diseases
with two different cures, and NOTHING in v3 could tell them apart: a leak and a
fan-out both look like "anon went up". So each sample now records
  ffprobe=<count>   live ffprobe children
  ffmpeg=<count>    live ffmpeg children (transcodes AND scan-time extraction)
  children_rss=<bytes>  summed RSS of those children -- charged to the SAME
                        cgroup as the server, so it is part of mem_current but
                        NOT part of the server's own anon
  scanning=<yes|no|NA>  whether a library scan is running, checked by BOTH
                        mechanisms -- the RefreshLibrary scheduled task AND
                        each virtual folder's RefreshStatus. The second is
                        required, not belt-and-braces: this host scans via
                        jellyfin_library_scan.py's per-library
                        POST /Items/{id}/Refresh, which leaves the scheduled
                        task Idle, so a ScheduledTasks-only probe would have
                        read "no scan" through every real scan
Without `scanning`, correlating peaks against the Fri/Sat/Sun 05:05 cron is
guesswork about a task whose real start and end times are not in the cron line.

v3 (2026-09-01, H3/glibc-arena test): added two counters from the host-side
/proc/<pid>/maps (readable directly, no docker exec/sudo needed — same uid
as the container's `abc` user), testing dotnet/runtime#122027-style glibc
malloc arena fragmentation and dotnet/runtime#89776/#121455-style W^X
"doublemapper" memfd accumulation, both proposed as explanations for the
native (non-managed-heap) memory growth documented in
docs/jellyfin-playback-audit.md:
  arena_regions=<count>   anonymous mappings sized ~64MiB (glibc's default
                          per-arena chunk size) — a rough proxy for live
                          malloc arena count, not exact (large anon mmaps
                          for other reasons would also match; treat as a
                          leading indicator, not ground truth)
  doublemapper=<count>    mappings whose path contains "doublemapper" — .NET's
                          W^X double-mapped JIT memory (memfd_create-backed)

Records per sample, tab-separated:
  timestamp
  anon=<bytes>       from cgroup memory.stat "anon" field — the OOM-relevant number
  file=<bytes>       from cgroup memory.stat "file" field — page cache, expected to grow benignly
  slab=<bytes>       from cgroup memory.stat "slab" field
  kstack=<bytes>     from cgroup memory.stat "kernel_stack" field
  mem_current=<bytes>  cgroup memory.current (total charged, anon+file+slab+...)
  mem_peak=<bytes>     cgroup memory.peak (high-water mark since last reset)
  vmrss=<bytes>        /proc/<jellyfin-pid>/status VmRSS — per-process figure,
                       directly comparable to the kernel's per-task RSS table
  arena_regions=<count>  see v3 note above
  doublemapper=<count>   see v3 note above

On any failure to read a value, the sample line records the failure mode
explicitly (SAMPLE_FAILED reason=...) instead of silently writing "NA" and
moving on, so a broken sampler doesn't get mistaken for "everything's fine."

Exit codes
----------
  0 success
  1 partial (container unreachable / a value couldn't be read; logged to stderr)
  2 fatal (unexpected error)

Usage
-----
  python scripts/jellyfin_mem_sample.py

Cron (every minute):
  * * * * * cd /home/tom/nas && /usr/bin/python3 scripts/jellyfin_mem_sample.py >> logs/jellyfin_mem_sample.err 2>&1
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def _load_env_file(path: Path) -> None:
    """Read KEY=value out of .env into os.environ without python-dotenv.

    Deliberately dependency-free: the cron line for this sampler runs
    `/usr/bin/python3`, NOT the venv, so `import dotenv` fails there -- which
    made `scanning=` record NA on every scheduled sample while working fine by
    hand. A field that is only populated when you run it manually is worse than
    no field, because it looks present.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    except OSError:
        return

CONTAINER = "jellyfin"
LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "jellyfin-mem.log"
MAX_LOG_BYTES = 10 * 1024 * 1024  # ~10MB

_load_env_file(Path(__file__).resolve().parent.parent / ".env")


def _rotate_if_needed(path: Path) -> None:
    """Keep the log from growing forever: one rotation, one backup kept."""
    if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
        backup = path.with_suffix(path.suffix + ".1")
        path.replace(backup)


def _docker_exec(cmd: list[str]) -> str:
    """Run a command inside the jellyfin container, raise on any failure."""
    result = subprocess.run(
        ["docker", "exec", CONTAINER, *cmd],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return result.stdout


def _parse_memory_stat(raw: str) -> dict[str, int]:
    """Parse cgroup v2 memory.stat (space-separated 'key value' lines)."""
    stats: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            stats[parts[0]] = int(parts[1])
    return stats


def _jellyfin_pid() -> str:
    """Find the actual jellyfin server process PID inside the container."""
    out = _docker_exec(["pgrep", "-f", "^/usr/bin/jellyfin"])
    pid = out.strip().splitlines()[0]
    if not pid.isdigit():
        raise ValueError(f"unexpected pgrep output: {out!r}")
    return pid


def _vmrss_bytes(pid: str) -> int:
    raw = _docker_exec(["cat", f"/proc/{pid}/status"])
    for line in raw.splitlines():
        if line.startswith("VmRSS:"):
            # format: "VmRSS:\t   123456 kB"
            kb = int(line.split()[1])
            return kb * 1024
    raise ValueError("VmRSS not found in /proc/<pid>/status")


def _host_pid() -> str | None:
    """Find the jellyfin process's *host-side* PID via `docker top`, so we
    can read /proc/<pid>/maps directly (same uid, no docker exec needed).
    Returns None if it can't be found (sample continues without arena data
    rather than failing the whole sample)."""
    try:
        result = subprocess.run(
            ["docker", "top", CONTAINER, "-o", "pid,cmd"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    for line in result.stdout.splitlines():
        if "/usr/bin/jellyfin" in line:
            pid = line.split()[0]
            if pid.isdigit():
                return pid
    return None


def _maps_counts(host_pid: str) -> tuple[int | None, int | None]:
    """Count (arena_regions, doublemapper) from /proc/<host_pid>/maps.

    arena_regions: anonymous mappings sized ~64MiB (glibc's default
    MALLOC_ARENA chunk size), a leading indicator for live malloc arena
    count per dotnet/runtime#122027 — not exact ground truth.
    doublemapper: mappings whose path contains "doublemapper" — .NET's W^X
    double-mapped JIT memory per dotnet/runtime#89776 / #121455.
    """
    try:
        with open(f"/proc/{host_pid}/maps") as f:
            lines = f.readlines()
    except OSError:
        return None, None

    arena_regions = 0
    doublemapper = 0
    target_size = 64 * 1024 * 1024  # 64MiB
    tolerance = 1 * 1024 * 1024  # +/- 1MiB, mappings aren't always exact
    for line in lines:
        parts = line.split(maxsplit=5)
        if len(parts) < 5:
            continue
        addr_range = parts[0]
        path = parts[5].strip() if len(parts) > 5 else ""
        if "doublemapper" in path:
            doublemapper += 1
        # Anonymous (no path) mappings only, for the arena heuristic
        if not path:
            try:
                start_s, end_s = addr_range.split("-")
                size = int(end_s, 16) - int(start_s, 16)
            except ValueError:
                continue
            if abs(size - target_size) <= tolerance:
                arena_regions += 1
    return arena_regions, doublemapper


def _child_processes() -> tuple[int, int, int | None]:
    """Census of live ffprobe/ffmpeg children: (ffprobe, ffmpeg, summed RSS).

    Read host-side via `docker top` so it needs no exec into the container and
    cannot fail the whole sample. RSS is summed from the host /proc, and is
    None if any of it is unreadable -- a partial sum would understate the very
    thing this is measuring.

    Why this matters for the diagnosis: these children are charged to the same
    cgroup as the server, so they inflate `mem_current` while leaving the
    server's own `anon` flat. A fan-out therefore looks completely different
    from a leak once you count them -- and identical if you do not.
    """
    try:
        result = subprocess.run(
            ["docker", "top", CONTAINER, "-o", "pid,rss,cmd"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return 0, 0, None

    ffprobe = ffmpeg = 0
    rss_kb = 0
    unreadable = False
    for line in result.stdout.splitlines()[1:]:
        parts = line.split(maxsplit=2)
        if len(parts) < 3:
            continue
        _pid, rss, cmd = parts
        # Match the binary, not the string anywhere in the command line: a
        # transcode's argv mentions paths that can contain either word.
        exe = cmd.split()[0].rsplit("/", 1)[-1] if cmd.split() else ""
        if exe.startswith("ffprobe"):
            ffprobe += 1
        elif exe.startswith("ffmpeg"):
            ffmpeg += 1
        else:
            continue
        if rss.isdigit():
            rss_kb += int(rss)
        else:
            unreadable = True
    return ffprobe, ffmpeg, None if unreadable else rss_kb * 1024


def _jellyfin_get(path: str):
    """GET a Jellyfin API path as parsed JSON, or None. Never raises."""
    key = os.environ.get("API_KEY_JELLYFIN_ARR") or os.environ.get("API_KEY_JELLYFIN")
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:8096{path}",
            headers={"Authorization": f'MediaBrowser Token="{key}"'},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:  # noqa: S310
            return json.load(resp)
    except (OSError, ValueError):
        return None


def _scan_running() -> str:
    """'yes' / 'no' / 'NA' -- is a library scan running, by EITHER mechanism?

    Two sources, because this host uses the one that the obvious source cannot
    see. `scripts/jellyfin_library_scan.py` drives the per-library
    `POST /Items/{id}/Refresh` endpoint, and that leaves the `RefreshLibrary`
    SCHEDULED TASK reporting `Idle` -- the script's own docstring says so. A
    probe that only read ScheduledTasks would therefore have recorded
    `scanning=no` through every scan this box actually runs, forever, while
    looking like a working field. So `RefreshStatus` on each virtual folder is
    checked as well, which is what the per-library refresh does move.

    Asked of the API rather than inferred from the cron schedule: the cron line
    records when a scan is TRIGGERED, not whether it is still going, and an
    overrunning scan is exactly the case worth catching. NA rather than a guess
    when the API is unreachable, so a broken probe stays distinguishable from a
    quiet server.
    """
    seen = False

    tasks = _jellyfin_get("/ScheduledTasks")
    if isinstance(tasks, list):
        seen = True
        for task in tasks:
            name = f"{task.get('Key', '')} {task.get('Name', '')}"
            is_scan = "RefreshLibrary" in name or "Scan Media Library" in name
            if is_scan and str(task.get("State", "")).lower() == "running":
                return "yes"

    folders = _jellyfin_get("/Library/VirtualFolders")
    if isinstance(folders, list):
        seen = True
        for folder in folders:
            if str(folder.get("RefreshStatus", "")).lower() not in ("", "idle"):
                return "yes"

    return "no" if seen else "NA"


def main() -> int:
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _rotate_if_needed(LOG_PATH)

    try:
        stat_raw = _docker_exec(["cat", "/sys/fs/cgroup/memory.stat"])
        stats = _parse_memory_stat(stat_raw)
        mem_current = int(_docker_exec(["cat", "/sys/fs/cgroup/memory.current"]).strip())
        mem_peak_raw = _docker_exec(["cat", "/sys/fs/cgroup/memory.peak"]).strip()
        mem_peak = int(mem_peak_raw) if mem_peak_raw.isdigit() else None
        pid = _jellyfin_pid()
        vmrss = _vmrss_bytes(pid)
    except subprocess.TimeoutExpired:
        with LOG_PATH.open("a") as f:
            f.write(f"{timestamp}\tSAMPLE_FAILED reason=timeout\n")
        print(f"{timestamp} sample failed: docker exec timed out", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        with LOG_PATH.open("a") as f:
            f.write(f"{timestamp}\tSAMPLE_FAILED reason=docker_exec_failed rc={exc.returncode}\n")
        print(f"{timestamp} sample failed: {exc}", file=sys.stderr)
        return 1
    except (ValueError, IndexError) as exc:
        with LOG_PATH.open("a") as f:
            f.write(f"{timestamp}\tSAMPLE_FAILED reason=parse_error detail={exc}\n")
        print(f"{timestamp} sample failed to parse: {exc}", file=sys.stderr)
        return 1

    def mb(n: int | None) -> str:
        return f"{n / (1024 * 1024):.1f}MB" if n is not None else "NA"

    host_pid = _host_pid()
    arena_regions, doublemapper = (None, None)
    if host_pid is not None:
        arena_regions, doublemapper = _maps_counts(host_pid)

    def count_str(n: int | None) -> str:
        return str(n) if n is not None else "NA"

    ffprobe, ffmpeg, children_rss = _child_processes()
    scanning = _scan_running()

    line = (
        f"{timestamp}\t"
        f"anon={mb(stats.get('anon'))}\t"
        f"file={mb(stats.get('file'))}\t"
        f"slab={mb(stats.get('slab'))}\t"
        f"kstack={mb(stats.get('kernel_stack'))}\t"
        f"mem_current={mb(mem_current)}\t"
        f"mem_peak={mb(mem_peak)}\t"
        f"vmrss={mb(vmrss)}\t"
        f"arena_regions={count_str(arena_regions)}\t"
        f"doublemapper={count_str(doublemapper)}\t"
        f"ffprobe={ffprobe}\t"
        f"ffmpeg={ffmpeg}\t"
        f"children_rss={mb(children_rss)}\t"
        f"scanning={scanning}\n"
    )
    with LOG_PATH.open("a") as f:
        f.write(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
