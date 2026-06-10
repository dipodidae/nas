#!/usr/bin/env python3
"""Unified media-ops status aggregator — stack health in one command.

Pulls health from all major stack components (Docker containers, *arr APIs,
slskd, qBittorrent, cron log freshness) and emits either a human-readable
terminal summary or a structured JSON report for dashboard consumption.

Sources gathered
----------------
1. Docker containers — ``docker ps`` state + health for all stack containers.
2. *arr services — Sonarr/Radarr/Lidarr/Prowlarr/Bazarr via their REST APIs:
     health endpoint + queue depth.  Each is independently degradable.
3. slskd — ``GET /api/v0/application`` on :5030; reports up + download counts.
4. qBittorrent — best-effort login + torrent count on :8080 (via gluetun).
5. Cron log freshness — ``~/nas/logs/*.log`` mtime age + last non-empty line.

Port map (confirmed against docker-compose.yml)
-----------------------------------------------
  sonarr   8989   /api/v3/health  /api/v3/queue
  radarr   7878   /api/v3/health  /api/v3/queue
  lidarr   8686   /api/v1/health  /api/v1/queue
  prowlarr 9696   /api/v1/health  (no queue)
  bazarr   6767   /api/v1/health  (no queue — uses its own schema)
  slskd    5030   /api/v0/application
  qbit     8080   /api/v2/auth/login  /api/v2/torrents/info

Output
------
  Default: terminal summary with ✓/⚠/✗ icons per service.
  --json:      print full JSON report to stdout.
  --json-out PATH: write full JSON report to PATH (also prints summary).

Exit codes
----------
  0  everything healthy
  1  degraded (at least one service down, stale, or unhealthy)
  2  fatal (docker binary missing or other bootstrap failure)

Environment
-----------
  API_KEY_SONARR   (optional) Sonarr API key
  API_KEY_RADARR   (optional) Radarr API key
  API_KEY_LIDARR   (optional) Lidarr API key
  API_KEY_PROWLARR (optional) Prowlarr API key
  API_KEY_SLSKD    (optional) slskd API key
  QBITTORRENT_USER (optional, default: admin)
  QBITTORRENT_PASS (optional) qBittorrent WebUI password

Usage
-----
  python scripts/media_ops_status.py
  python scripts/media_ops_status.py --json
  python scripts/media_ops_status.py --json-out /tmp/status.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Auto-load .env so we can be run directly without pre-exporting vars.
# ---------------------------------------------------------------------------
if not any(
    k in os.environ
    for k in ("API_KEY_LIDARR", "API_KEY_SONARR", "API_KEY_RADARR", "API_KEY_PROWLARR")
):
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except ImportError:
        pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_UTC = _dt.UTC

DEFAULT_LOG_DIR = Path.home() / "nas" / "logs"
# A cron log is considered stale if its mtime is older than this (seconds).
DEFAULT_STALE_AFTER_S = 4 * 3600  # 4 hours (safe margin for hourly crons)

_ARR_SERVICES: list[tuple[str, int, str, str | None]] = [
    # (name, port, health_path, queue_path_or_None)
    ("sonarr", 8989, "/api/v3/health", "/api/v3/queue?pageSize=1"),
    ("radarr", 7878, "/api/v3/health", "/api/v3/queue?pageSize=1"),
    ("lidarr", 8686, "/api/v1/health", "/api/v1/queue?pageSize=1"),
    ("prowlarr", 9696, "/api/v1/health", None),
    ("bazarr", 6767, "/api/v1/health", None),
]

_ARR_KEY_ENV: dict[str, str] = {
    "sonarr": "API_KEY_SONARR",
    "radarr": "API_KEY_RADARR",
    "lidarr": "API_KEY_LIDARR",
    "prowlarr": "API_KEY_PROWLARR",
}

# *arr health-endpoint issue types that we surface as warnings (not fatal).
_WARN_TYPES = {"warning", "notice"}
_ERROR_TYPES = {"error"}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ContainerState:
    """One row from ``docker ps``."""

    name: str
    state: str    # running / exited / …
    status: str   # raw status string e.g. "Up 3 days (healthy)"
    healthy: str  # "healthy" / "unhealthy" / "starting" / ""


@dataclass
class ArrStatus:
    """Health + queue depth for a single *arr service."""

    name: str
    port: int
    reachable: bool
    health_issues: list[dict]   # raw health objects from API
    queue_total: int | None     # None = no queue endpoint / unreachable
    error: str | None = None


@dataclass
class SlskdStatus:
    """slskd application status."""

    reachable: bool
    version: str | None = None
    downloads_total: int | None = None
    uploads_total: int | None = None
    error: str | None = None


@dataclass
class QbitStatus:
    """qBittorrent status (best-effort)."""

    reachable: bool
    torrent_count: int | None = None
    error: str | None = None


@dataclass
class LogStatus:
    """Freshness of a single cron log file."""

    path: str
    mtime_epoch: float | None
    age_s: float | None
    stale: bool
    last_line: str | None
    error: str | None = None


@dataclass
class OpsReport:
    """Full aggregated report.  Top-level JSON keys mirror these fields."""

    generated_at: str           # ISO-8601 UTC timestamp
    overall: str                # "ok" / "degraded" / "down"
    containers: list[ContainerState] = field(default_factory=list)
    arr_services: list[ArrStatus] = field(default_factory=list)
    slskd: SlskdStatus | None = None
    qbittorrent: QbitStatus | None = None
    logs: list[LogStatus] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helper functions (no I/O — unit-testable)
# ---------------------------------------------------------------------------


def parse_docker_ps(stdout: str) -> list[ContainerState]:
    """Parse the output of ``docker ps --format '{{.Names}}|{{.State}}|{{.Status}}'``.

    Each line is ``name|state|status``.  Health is extracted from the status
    string (e.g. ``Up 3 days (healthy)`` → ``healthy``).
    """
    results: list[ContainerState] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        name, state, status = parts[0], parts[1], parts[2]
        healthy = _extract_health(status)
        results.append(ContainerState(name=name, state=state, status=status, healthy=healthy))
    return results


def _extract_health(status: str) -> str:
    """Pull the health label from a Docker status string.

    ``'Up 3 days (healthy)'`` → ``'healthy'``
    ``'Up 10 minutes (unhealthy)'`` → ``'unhealthy'``
    ``'Up 2 hours (health: starting)'`` → ``'starting'``
    ``'Up 5 minutes'`` → ``''``
    """
    s = status.lower()
    if "(healthy)" in s:
        return "healthy"
    if "(unhealthy)" in s:
        return "unhealthy"
    if "starting" in s:
        return "starting"
    return ""


def log_age_status(mtime_epoch: float, now_epoch: float, stale_after_s: float) -> str:
    """Return a human-readable age string and whether the log is stale.

    Returns a formatted age string like ``'2h 15m'`` or ``'stale (5h 3m)'``.
    """
    age = now_epoch - mtime_epoch
    h, rem = divmod(int(age), 3600)
    m = rem // 60
    age_str = f"{h}h {m}m" if h else f"{m}m"
    if age > stale_after_s:
        return f"stale ({age_str})"
    return age_str


def derive_overall_status(
    containers: list[ContainerState],
    arr_services: list[ArrStatus],
    slskd: SlskdStatus | None,
    qbit: QbitStatus | None,
    logs: list[LogStatus],
) -> str:
    """Derive the overall health label from all gathered data.

    Returns ``'ok'``, ``'degraded'``, or ``'down'``.

    Rules (first match wins):
    - ``'down'``:    no containers at all visible, OR all critical *arr services
                     unreachable.
    - ``'degraded'``: any container unhealthy, any *arr error-level health issue,
                      any critical service unreachable, or any stale log.
    - ``'ok'``:      everything else.

    Critical services for "down" determination: sonarr, radarr, lidarr, prowlarr.
    """
    critical_names = {"sonarr", "radarr", "lidarr", "prowlarr"}
    if not containers:
        return "down"

    critical_arr = [s for s in arr_services if s.name in critical_names]
    if critical_arr and all(not s.reachable for s in critical_arr):
        return "down"

    # Check for any degraded condition
    for c in containers:
        if c.healthy == "unhealthy":
            return "degraded"

    for s in arr_services:
        if not s.reachable:
            return "degraded"
        for issue in s.health_issues:
            if str(issue.get("type", "")).lower() in _ERROR_TYPES:
                return "degraded"

    if slskd is not None and not slskd.reachable:
        return "degraded"

    if any(lg.stale for lg in logs):
        return "degraded"

    return "ok"


def _arr_health_summary(issues: list[dict]) -> str:
    """Return a compact string summarising *arr health issues."""
    if not issues:
        return "ok"
    parts = []
    for issue in issues:
        t = issue.get("type", "?").lower()
        msg = issue.get("message", "")
        parts.append(f"[{t}] {msg}")
    return "; ".join(parts)


def format_summary(report: OpsReport) -> str:
    """Render the OpsReport as a human-readable terminal string.

    Pure function — no I/O.  Returns the full summary text including a
    header, per-service lines, log freshness, and an overall status footer.
    """
    lines: list[str] = []
    lines.append(f"Media-ops status  {report.generated_at}")
    lines.append("=" * 60)

    # --- Containers ---
    lines.append("\nContainers:")
    if not report.containers:
        lines.append("  ✗ no containers found (docker unavailable?)")
    else:
        for c in sorted(report.containers, key=lambda x: x.name):
            if c.state != "running":
                icon = "✗"
            elif c.healthy == "unhealthy":
                icon = "⚠"
            elif c.healthy in ("healthy", ""):
                icon = "✓"
            else:
                icon = "⚠"
            health_str = f" ({c.healthy})" if c.healthy else ""
            lines.append(f"  {icon} {c.name}: {c.state}{health_str}")

    # --- *arr services ---
    lines.append("\n*arr services:")
    for s in report.arr_services:
        if not s.reachable:
            icon = "✗"
            detail = s.error or "unreachable"
        else:
            issues = s.health_issues
            has_error = any(str(i.get("type", "")).lower() in _ERROR_TYPES for i in issues)
            has_warn = any(str(i.get("type", "")).lower() in _WARN_TYPES for i in issues)
            if has_error:
                icon = "✗"
            elif has_warn:
                icon = "⚠"
            else:
                icon = "✓"
            health_str = _arr_health_summary(issues)
            queue_str = ""
            if s.queue_total is not None:
                queue_str = f"  queue={s.queue_total}"
            detail = f"health={health_str}{queue_str}"
        lines.append(f"  {icon} {s.name}:{s.port}  {detail}")

    # --- slskd ---
    lines.append("\nslskd:")
    if report.slskd is None:
        lines.append("  - not checked")
    elif not report.slskd.reachable:
        lines.append(f"  ✗ unreachable: {report.slskd.error or 'unknown'}")
    else:
        s = report.slskd
        parts = ["up"]
        if s.version:
            parts.append(f"v{s.version}")
        if s.downloads_total is not None:
            parts.append(f"dl={s.downloads_total}")
        if s.uploads_total is not None:
            parts.append(f"ul={s.uploads_total}")
        lines.append(f"  ✓ {' '.join(parts)}")

    # --- qBittorrent ---
    lines.append("\nqBittorrent:")
    if report.qbittorrent is None:
        lines.append("  - not checked")
    elif not report.qbittorrent.reachable:
        lines.append(f"  ✗ unreachable: {report.qbittorrent.error or 'unknown'}")
    else:
        q = report.qbittorrent
        parts = ["up"]
        if q.torrent_count is not None:
            parts.append(f"torrents={q.torrent_count}")
        lines.append(f"  ✓ {' '.join(parts)}")

    # --- Log freshness ---
    lines.append("\nCron logs:")
    if not report.logs:
        lines.append("  - no log files found")
    else:
        for lg in sorted(report.logs, key=lambda x: x.path):
            name = Path(lg.path).name
            if lg.error:
                lines.append(f"  ✗ {name}: {lg.error}")
            elif lg.mtime_epoch is None:
                lines.append(f"  ✗ {name}: no mtime")
            else:
                icon = "⚠" if lg.stale else "✓"
                age_s = f"age={log_age_status(lg.mtime_epoch, _dt.datetime.now(_UTC).timestamp(), DEFAULT_STALE_AFTER_S)}"
                last = f"  last={lg.last_line!r}" if lg.last_line else ""
                lines.append(f"  {icon} {name}: {age_s}{last}")

    # --- Footer ---
    lines.append("")
    overall_icon = {"ok": "✓", "degraded": "⚠", "down": "✗"}.get(report.overall, "?")
    lines.append(f"Overall: {overall_icon} {report.overall.upper()}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# I/O gathering functions
# ---------------------------------------------------------------------------


def _http_get(
    url: str,
    headers: dict[str, str],
    timeout: int = 5,
) -> tuple[int, bytes]:
    """Perform a GET request.  Returns (status_code, body_bytes)."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def gather_containers(docker_bin: str = "docker") -> list[ContainerState]:
    """Run ``docker ps`` and return parsed container states.

    Raises ``RuntimeError`` if the docker binary is not found (fatal).
    Returns an empty list if docker ps fails for any other reason.
    """
    fmt = "{{.Names}}|{{.State}}|{{.Status}}"
    try:
        result = subprocess.run(
            [docker_bin, "ps", "--format", fmt, "--no-trunc"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return []
        return parse_docker_ps(result.stdout)
    except FileNotFoundError as exc:
        raise RuntimeError(f"docker binary not found: {docker_bin!r}") from exc
    except subprocess.TimeoutExpired:
        return []


def gather_arr_service(
    name: str,
    port: int,
    health_path: str,
    queue_path: str | None,
    api_key: str | None,
) -> ArrStatus:
    """Fetch health + queue depth for a single *arr service.

    Degrades gracefully: a connection error sets ``reachable=False`` with an
    error message rather than raising.
    """
    base = f"http://localhost:{port}"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-Api-Key"] = api_key

    # --- health ---
    health_issues: list[dict] = []
    try:
        status, body = _http_get(f"{base}{health_path}", headers)
        if status == 200:
            data = json.loads(body) if body else []
            if isinstance(data, list):
                health_issues = data
        elif status == 401:
            return ArrStatus(
                name=name, port=port, reachable=True,
                health_issues=[], queue_total=None,
                error="401 Unauthorized (check API key)",
            )
        else:
            return ArrStatus(
                name=name, port=port, reachable=False,
                health_issues=[], queue_total=None,
                error=f"HTTP {status}",
            )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return ArrStatus(
            name=name, port=port, reachable=False,
            health_issues=[], queue_total=None,
            error=str(exc),
        )
    except ValueError as exc:
        # Non-JSON body (e.g. an HTML error/login page) — reachable but unparseable.
        return ArrStatus(
            name=name, port=port, reachable=True,
            health_issues=[], queue_total=None,
            error=f"non-JSON response ({exc})",
        )

    # --- queue ---
    queue_total: int | None = None
    if queue_path:
        try:
            qstatus, qbody = _http_get(f"{base}{queue_path}", headers)
            if qstatus == 200:
                qdata = json.loads(qbody) if qbody else {}
                if isinstance(qdata, dict):
                    queue_total = qdata.get("totalRecords")
        except (urllib.error.URLError, OSError, TimeoutError, ValueError):
            pass  # queue depth is best-effort

    return ArrStatus(
        name=name,
        port=port,
        reachable=True,
        health_issues=health_issues,
        queue_total=queue_total,
    )


def gather_all_arr(env: dict[str, str] | None = None) -> list[ArrStatus]:
    """Gather health for all configured *arr services."""
    if env is None:
        env = dict(os.environ)
    results = []
    for name, port, health_path, queue_path in _ARR_SERVICES:
        key_env = _ARR_KEY_ENV.get(name)
        api_key = env.get(key_env) if key_env else None
        results.append(gather_arr_service(name, port, health_path, queue_path, api_key))
    return results


def gather_slskd(api_key: str | None) -> SlskdStatus:
    """Fetch slskd application info from ``/api/v0/application``."""
    base = "http://localhost:5030"
    headers: dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key

    try:
        status, body = _http_get(f"{base}/api/v0/application", headers)
        if status != 200:
            return SlskdStatus(reachable=False, error=f"HTTP {status}")
        data = json.loads(body) if body else {}
        # slskd reports version as a dict {full,current,latest,...}; older builds as a string.
        raw_version = data.get("version")
        version = raw_version.get("current") if isinstance(raw_version, dict) else raw_version
        # Transfer counts live under server.downloads / server.uploads
        server = data.get("server", {})
        dl = server.get("downloads", {})
        ul = server.get("uploads", {})
        dl_count = dl.get("inProgress") if isinstance(dl, dict) else None
        ul_count = ul.get("inProgress") if isinstance(ul, dict) else None
        return SlskdStatus(
            reachable=True,
            version=version,
            downloads_total=dl_count,
            uploads_total=ul_count,
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return SlskdStatus(reachable=False, error=str(exc))
    except (ValueError, KeyError) as exc:
        return SlskdStatus(reachable=False, error=f"parse error: {exc}")


def gather_qbittorrent(user: str | None, password: str | None) -> QbitStatus:
    """Login to qBittorrent and count torrents.  Best-effort — never fatal."""
    base = "http://localhost:8080"

    if not user or not password:
        return QbitStatus(reachable=False, error="credentials not set (QBITTORRENT_USER/PASS)")

    # Login
    login_data = urllib.parse.urlencode({"username": user, "password": password}).encode()
    login_req = urllib.request.Request(
        f"{base}/api/v2/auth/login",
        data=login_data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(login_req, timeout=5) as resp:  # noqa: S310 - localhost
            body = resp.read().decode()
            if body.strip().lower() != "ok.":
                return QbitStatus(reachable=False, error="login rejected")
            # Cookie is set by urllib's default CookieJar — but urllib doesn't
            # automatically carry cookies between requests.  We need the SID cookie.
            cookie_hdr = resp.getheader("Set-Cookie", "")
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return QbitStatus(reachable=False, error=str(exc))

    # Extract SID from Set-Cookie
    sid = ""
    for part in cookie_hdr.split(";"):
        kv = part.strip()
        if kv.startswith("SID="):
            sid = kv.split("=", 1)[1]
            break
    if not sid:
        return QbitStatus(reachable=True, torrent_count=None, error="no SID cookie returned")

    # Fetch torrent list (pageSize limit to reduce payload)
    try:
        status, body_bytes = _http_get(
            f"{base}/api/v2/torrents/info?limit=1",
            {"Cookie": f"SID={sid}"},
        )
        if status != 200:
            return QbitStatus(reachable=True, torrent_count=None, error=f"HTTP {status}")
        # Count all torrents via a second call for total (no paging API in qbit v2)
        status2, body2 = _http_get(
            f"{base}/api/v2/torrents/info",
            {"Cookie": f"SID={sid}"},
        )
        if status2 == 200:
            torrents = json.loads(body2) if body2 else []
            return QbitStatus(reachable=True, torrent_count=len(torrents))
        return QbitStatus(reachable=True, torrent_count=None)
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        return QbitStatus(reachable=True, torrent_count=None, error=str(exc))


def gather_logs(log_dir: Path, now_epoch: float, stale_after_s: float) -> list[LogStatus]:
    """Stat every ``*.log`` file in ``log_dir`` and report freshness."""
    results: list[LogStatus] = []
    try:
        log_files = sorted(log_dir.glob("*.log"))
    except OSError as exc:
        return [LogStatus(
            path=str(log_dir / "*.log"),
            mtime_epoch=None, age_s=None, stale=True,
            last_line=None, error=f"cannot list log dir: {exc}",
        )]

    for lf in log_files:
        try:
            stat = lf.stat()
            mtime = stat.st_mtime
            age = now_epoch - mtime
            stale = age > stale_after_s
            # Read last non-empty line efficiently
            last_line = _read_last_line(lf)
            results.append(LogStatus(
                path=str(lf),
                mtime_epoch=mtime,
                age_s=age,
                stale=stale,
                last_line=last_line,
            ))
        except OSError as exc:
            results.append(LogStatus(
                path=str(lf),
                mtime_epoch=None, age_s=None, stale=True,
                last_line=None, error=str(exc),
            ))
    return results


def _read_last_line(path: Path, max_bytes: int = 8192) -> str | None:
    """Return the last non-empty line from a text file without reading all of it."""
    try:
        size = path.stat().st_size
        if size == 0:
            return None
        read_size = min(size, max_bytes)
        with path.open("rb") as fh:
            fh.seek(max(0, size - read_size))
            chunk = fh.read(read_size).decode("utf-8", errors="replace")
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unified media-ops status aggregator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print full JSON report to stdout (instead of terminal summary).",
    )
    parser.add_argument(
        "--json-out",
        metavar="PATH",
        help="Write full JSON report to PATH (terminal summary still printed).",
    )
    parser.add_argument(
        "--log-dir",
        metavar="PATH",
        default=str(DEFAULT_LOG_DIR),
        help=f"Directory to scan for cron logs (default: {DEFAULT_LOG_DIR}).",
    )
    parser.add_argument(
        "--stale-after",
        type=int,
        default=DEFAULT_STALE_AFTER_S,
        metavar="SECONDS",
        help=f"Seconds before a log is considered stale (default: {DEFAULT_STALE_AFTER_S}).",
    )
    parser.add_argument(
        "--docker-bin",
        default="docker",
        help="Docker binary to use (default: docker).",
    )
    parser.add_argument(
        "--skip-qbit",
        action="store_true",
        help="Skip qBittorrent check (useful when VPN is down).",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------


def _report_to_dict(report: OpsReport) -> dict:
    """Serialise OpsReport to a plain dict (JSON-safe)."""
    return {
        "generated_at": report.generated_at,
        "overall": report.overall,
        "containers": [asdict(c) for c in report.containers],
        "arr_services": [asdict(s) for s in report.arr_services],
        "slskd": asdict(report.slskd) if report.slskd else None,
        "qbittorrent": asdict(report.qbittorrent) if report.qbittorrent else None,
        "logs": [asdict(lg) for lg in report.logs],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env = dict(os.environ)
    now = _dt.datetime.now(_UTC)
    now_epoch = now.timestamp()
    log_dir = Path(args.log_dir).expanduser()

    # 1. Docker containers
    try:
        containers = gather_containers(args.docker_bin)
    except RuntimeError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2

    # 2. *arr services
    arr_services = gather_all_arr(env)

    # 3. slskd
    slskd_key = env.get("API_KEY_SLSKD")
    slskd = gather_slskd(slskd_key)

    # 4. qBittorrent (best-effort)
    if args.skip_qbit:
        qbit: QbitStatus | None = None
    else:
        qbit_user = env.get("QBITTORRENT_USER", "admin")
        qbit_pass = env.get("QBITTORRENT_PASS")
        qbit = gather_qbittorrent(qbit_user, qbit_pass)

    # 5. Cron log freshness
    logs = gather_logs(log_dir, now_epoch, args.stale_after)

    # Assemble
    overall = derive_overall_status(containers, arr_services, slskd, qbit, logs)
    report = OpsReport(
        generated_at=now.isoformat(),
        overall=overall,
        containers=containers,
        arr_services=arr_services,
        slskd=slskd,
        qbittorrent=qbit,
        logs=logs,
    )

    report_dict = _report_to_dict(report)

    if args.json:
        print(json.dumps(report_dict, indent=2, default=str))
        return 0 if overall == "ok" else (1 if overall == "degraded" else 2)

    summary = format_summary(report)
    print(summary)

    if args.json_out:
        out_path = Path(args.json_out).expanduser()
        try:
            out_path.write_text(json.dumps(report_dict, indent=2, default=str))
            print(f"\nJSON report written to {out_path}")
        except OSError as exc:
            print(f"WARNING: could not write JSON report: {exc}", file=sys.stderr)

    if overall == "ok":
        return 0
    if overall == "degraded":
        return 1
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: unexpected error: {exc}", file=sys.stderr)
        sys.exit(2)
