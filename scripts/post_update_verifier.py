#!/usr/bin/env python3
"""Daily stack-health assertion: container state and HTTP reachability.

Originally a post-Watchtower hook. **Watchtower is retired** (ADR-0025) and
nothing applies an update on a schedule any more, so this is now a standalone
daily verifier — it kept its 04:30 cron slot.

Checks performed:
  • Docker container state & (if present) health status
  • HTTP(S) endpoint reachability + status code

Notification lanes (ADR-0033)
-----------------------------
The two findings are not the same kind of thing and must not share a lane:

  • **a service with no container at all** -> `nas-critical`. This is ADR-0006's
    failure mode: not unhealthy, *absent*. It cost qBittorrent thirteen hours,
    and it is the reason this file exists.
  • **anything else degraded** -> `nas-infra`. An unhealthy container or a
    non-2xx endpoint at 04:30 is usually mid-restart and gone by 04:35, and
    `stack_watchdog.py` is already watching it every five minutes with its own
    escalation ladder. Paging for it here would double-report.

Exit codes:
  0 all healthy
  1 degraded (at least one service unhealthy / endpoint failure)
  2 fatal (no services reachable / docker unavailable)

Environment:
  API_KEY_PROWLARR / API_KEY_SONARR / API_KEY_RADARR / API_KEY_LIDARR / API_KEY_SLSKD (optional for auth)
  DOCKER_BIN (default: docker)
  VERIFY_SERVICES comma list override of default services
  NTFY_TOKEN_SCRIPTS  ntfy token; unset = print only
  --no-notify         suppress the push (for an interactive run)
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# Auto-load .env to pick up API keys if not already in environment.
if not any(k in os.environ for k in ("API_KEY_PROWLARR", "API_KEY_SONARR", "API_KEY_RADARR", "API_KEY_LIDARR", "API_KEY_SLSKD")):
  try:  # pragma: no cover
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except Exception:
    pass

# Publish through the lane router; imported by path because this file runs as a
# script, not as part of a package.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify as notifier  # noqa: E402, I001


DEFAULT_SERVICES = [
  # name, port, path, https
  ("prowlarr", 9696, "/", False),
  ("sonarr", 8989, "/", False),
  ("radarr", 7878, "/", False),
  ("lidarr", 8686, "/", False),
  ("bazarr", 6767, "/", False),
  ("jellyfin", 8096, "/System/Info/Public", False),
  ("swag", 443, "/", True),
]

API_KEY_ENV = {
  "prowlarr": "API_KEY_PROWLARR",
  "sonarr": "API_KEY_SONARR",
  "radarr": "API_KEY_RADARR",
  "lidarr": "API_KEY_LIDARR",
}


@dataclass
class Result:
  service: str
  container_state: str
  health: str
  http_status: int | None
  latency_ms: float | None
  error: str | None = None

  @property
  def ok(self) -> bool:
    if self.error:
      return False
    if self.container_state != "running":
      return False
    if self.health and self.health not in ("healthy", ""):
      return False
    return not (self.http_status is not None and not (200 <= self.http_status < 300))


def docker_inspect(name: str, docker_bin: str) -> dict:
  try:
    out = subprocess.check_output(
      [docker_bin, "inspect", name], stderr=subprocess.DEVNULL, text=True
    )
    data = json.loads(out)
    return data[0] if data else {}
  except subprocess.CalledProcessError:
    return {}
  except FileNotFoundError as err:
    raise RuntimeError("docker binary not available; set DOCKER_BIN") from err


# SWAG is probed at https://localhost:443, and its certificate is a real
# Let's Encrypt cert for ${PUBLIC_DOMAIN} -- so hostname verification CANNOT
# pass and never could: every run reported
#   swag: err=HTTP error: [SSL: CERTIFICATE_VERIFY_FAILED] ... not valid for 'localhost'
# which the 04:30 cron line then swallowed, because cron_job.py's --ok-codes
# defaults to 0,1 and this file returns 1 for degraded. A check that has always
# failed and is always ignored is worse than no check: it occupies the slot.
#
# The property this probe is for is "does nginx answer on 443", not "is the
# certificate valid for localhost" -- which is a question with no useful answer.
# Certificate validity is a browser's job against the public name, and SWAG's
# own renewal is what watches it. The connection is over loopback, so an
# unverified context exposes nothing.
_LOOPBACK_TLS = ssl.create_default_context()
_LOOPBACK_TLS.check_hostname = False
_LOOPBACK_TLS.verify_mode = ssl.CERT_NONE


def http_probe(url: str, headers: dict, timeout: float = 5.0):
  req = urllib.request.Request(url, headers=headers)
  context = _LOOPBACK_TLS if url.startswith("https://") else None
  start = time.time()
  with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
    resp.read(512)  # content unused; small read for latency
    return resp.status, (time.time() - start) * 1000


def classify(results: list[Result]) -> tuple[list[str], list[str]]:
  """Split findings into (absent, degraded). Pure, so the routing is testable.

  "Absent" is deliberately its own bucket rather than the worst end of a
  severity scale: a container that is merely unhealthy is being watched by
  something else every five minutes, while a container that does not exist is
  being watched by nothing at all, because everything that inspects running
  containers cannot see it.
  """
  absent = [r.service for r in results if r.container_state == "missing"]
  degraded = [r.service for r in results if not r.ok and r.container_state != "missing"]
  return absent, degraded


def main(argv: list[str] | None = None) -> int:
  quiet = "--no-notify" in (argv if argv is not None else sys.argv[1:])
  docker_bin = os.getenv("DOCKER_BIN", "docker")
  services = []
  if override := os.getenv("VERIFY_SERVICES"):
    for item in override.split(","):
      name = item.strip()
      for tpl in DEFAULT_SERVICES:
        if tpl[0] == name:
          services.append(tpl)
          break
  if not services:
    services = DEFAULT_SERVICES

  results: list[Result] = []
  fatal = False
  for name, port, path, https in services:
    inspect = docker_inspect(name, docker_bin)
    state = inspect.get("State", {})
    container_state = state.get("Status", "missing")
    health = state.get("Health", {}).get("Status", "")
    scheme = "https" if https else "http"
    url = f"{scheme}://localhost:{port}{path}"
    headers = {}
    api_env = API_KEY_ENV.get(name)
    if api_env and (api_key := os.getenv(api_env)):
      headers["X-Api-Key"] = api_key
    http_status: int | None = None
    latency = None
    error = None
    if container_state == "running":
      try:
        http_status, latency = http_probe(url, headers)
      except urllib.error.URLError as e:
        error = f"HTTP error: {e.reason}" if hasattr(e, "reason") else str(e)
      except Exception as e:  # noqa
        error = f"HTTP probe failed: {e}"  # broad but safe
    else:
      error = "container not running"

    results.append(Result(name, container_state, health, http_status, latency, error))

  any_ok = any(r.ok for r in results)
  if not any_ok:
    fatal = True

  print("Post-Update Verification Summary:")
  for r in results:
    icon = "✅" if r.ok else ("❌" if r.error else "⚠️")
    lat = f"{r.latency_ms:.0f}ms" if r.latency_ms else "--"
    print(
      f" {icon} {r.service}: state={r.container_state} health={r.health or 'n/a'} http={r.http_status or '--'} lat={lat}"
      + (f" err={r.error}" if r.error else "")
    )

  absent, degraded = classify(results)
  if not quiet:
    if absent:
      # No cooldown: nas-critical is never suppressed, and this runs once a day.
      notifier.notify(
        notifier.Lane.CRITICAL,
        "verify: no container at all",
        f"{', '.join(absent)} — defined but absent. `docker compose up -d "
        f"{' '.join(absent)}`. This is the ADR-0006 failure mode.",
      )
    if degraded:
      notifier.notify(
        notifier.Lane.INFRA,
        "verify: degraded",
        "\n".join(
          f"{r.service}: state={r.container_state} health={r.health or 'n/a'} "
          f"http={r.http_status or '--'}{' err=' + r.error if r.error else ''}"
          for r in results
          if not r.ok and r.service in degraded
        ),
        dedup_key="verify-runtime:degraded",
      )

  if fatal:
    return 2
  return 0 if not (absent or degraded) else 1


if __name__ == "__main__":
  sys.exit(main())
