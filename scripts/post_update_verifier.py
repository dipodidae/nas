#!/usr/bin/env python3
"""Post-watchtower update verifier.

Intended to be executed after Watchtower performs container updates (e.g. via
WATCHTOWER_NOTIFICATION_COMMAND or a cron). Validates that critical services
are healthy both at the container and HTTP endpoint layer.

Checks performed:
  • Docker container state & (if present) health status
  • HTTP(S) endpoint reachability + status code

Exit codes:
  0 all healthy
  1 degraded (at least one service unhealthy / endpoint failure)
  2 fatal (no services reachable / docker unavailable)

Environment:
  API_KEY_PROWLARR / API_KEY_SONARR / API_KEY_RADARR (optional for auth)
  DOCKER_BIN (default: docker)
  VERIFY_SERVICES comma list override of default services
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

# Auto-load .env to pick up API keys if not already in environment.
if not any(k in os.environ for k in ("API_KEY_PROWLARR", "API_KEY_SONARR", "API_KEY_RADARR")):
  try:  # pragma: no cover
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except Exception:
    pass


DEFAULT_SERVICES = [
  # name, port, path, https
  ("prowlarr", 9696, "/", False),
  ("sonarr", 8989, "/", False),
  ("radarr", 7878, "/", False),
  ("bazarr", 6767, "/", False),
  ("jellyfin", 8096, "/System/Info/Public", False),
  ("swag", 443, "/", True),
]

API_KEY_ENV = {
  "prowlarr": "API_KEY_PROWLARR",
  "sonarr": "API_KEY_SONARR",
  "radarr": "API_KEY_RADARR",
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


def http_probe(url: str, headers: dict, timeout: float = 5.0):
  req = urllib.request.Request(url, headers=headers)
  start = time.time()
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    resp.read(512)  # content unused; small read for latency
    return resp.status, (time.time() - start) * 1000


def main() -> int:
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

  if fatal:
    return 2
  degraded = any(not r.ok for r in results)
  return 0 if not degraded else 1


if __name__ == "__main__":
  sys.exit(main())
