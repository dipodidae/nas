#!/usr/bin/env python3
"""Prove Umami ingests an event end to end. Used by `make verify-runtime`.

A green healthcheck here only means /api/heartbeat answers; it says nothing
about the path real events take. So this sends one, the way the ongehoord Nuxt
server does (nuxt-umami `proxy: 'cloak'`): through SWAG's PUBLIC route to
`/api/send`, with the visitor IP in the payload. Then it reads the event back
from Umami's API and checks that the location came from that IP (NL), not
from request headers (ADR-0056, SKIP_LOCATION_HEADERS).

Events go to the dedicated `nas-canary` website, so the real sites'
numbers are never touched.

Exit codes
----------
  0  the event arrived, geolocated from the payload IP
  1  it was sent but never showed up, or was geolocated wrongly
  2  Umami or its public route is unreachable, or the credentials fail
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

LOCAL = "http://127.0.0.1:3450"
CANARY = "nas-canary"
# SURFnet: a fixed, well-known Dutch address. GeoLite puts it in NL.
PROBE_IP = "145.97.0.1"
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


def request(url: str, body: dict | None = None, token: str | None = None) -> object:
    """JSON in, JSON out. Raises urllib errors to the caller."""
    headers = {"user-agent": UA}
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip().startswith(("{", "[")) else raw


def find_path(metrics: object, path: str) -> int:
    """Count for `path` in a /metrics?type=path answer. Pure."""
    if not isinstance(metrics, list):
        raise ValueError(f"unexpected metrics payload: {metrics!r}")
    return sum(int(m.get("y", 0)) for m in metrics if m.get("x") == path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wait", type=float, default=10.0, help="seconds to wait for the event")
    args = ap.parse_args()

    domain = os.environ.get("PUBLIC_DOMAIN")
    user = os.environ.get("UMAMI_ADMIN_USER")
    password = os.environ.get("UMAMI_ADMIN_PASSWORD")
    if not (domain and user and password):
        print("    !!! PUBLIC_DOMAIN / UMAMI_ADMIN_USER / UMAMI_ADMIN_PASSWORD not set")
        return 2

    try:
        token = request(f"{LOCAL}/api/auth/login", {"username": user, "password": password})["token"]
        sites = request(f"{LOCAL}/api/websites?pageSize=100", token=token)
        rows = sites.get("data", sites) if isinstance(sites, dict) else sites
        canary = next((s for s in rows if s.get("name") == CANARY), None)
        if canary is None:
            canary = request(f"{LOCAL}/api/websites", {"name": CANARY, "domain": "canary.invalid"}, token=token)
    except (urllib.error.URLError, KeyError, TypeError, ValueError) as exc:
        print(f"    !!! Umami API unreachable or login failed: {exc}")
        return 2

    path = f"/verify-runtime/{uuid.uuid4().hex[:12]}"
    started = int(time.time() * 1000) - 60_000
    payload = {"type": "event", "payload": {
        "website": canary["id"], "hostname": "canary.invalid", "url": path, "title": "verify-runtime",
        "language": "nl-NL", "screen": "1920x1080", "ip": PROBE_IP,
    }}
    try:
        request(f"https://umami.{domain}/api/send", payload)
    except urllib.error.URLError as exc:
        print(f"    !!! POST https://umami.{domain}/api/send failed: {exc}")
        return 2

    deadline = time.monotonic() + args.wait
    count, countries = 0, []
    while time.monotonic() < deadline:
        now = int(time.time() * 1000)
        q = f"startAt={started}&endAt={now}"
        count = find_path(request(f"{LOCAL}/api/websites/{canary['id']}/metrics?type=path&{q}", token=token), path)
        if count:
            countries = [m.get("x") for m in request(
                f"{LOCAL}/api/websites/{canary['id']}/metrics?type=country&{q}", token=token)]
            break
        time.sleep(1)

    if not count:
        print(f"    !!! event {path} was accepted by the public route but never appeared in Umami")
        return 1
    if "NL" not in countries:
        print(f"    !!! event arrived but was geolocated {countries}, not NL -- location is coming from")
        print("        request headers (Vercel/Cloudflare), not the visitor IP. SKIP_LOCATION_HEADERS lost?")
        return 1
    print(f"    ok: event ingested through the public route, geolocated from the payload IP ({path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
