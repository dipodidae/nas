#!/usr/bin/env python3
"""Configure Umami's reports for ongehoord.nl as code. `make umami-setup`. ADR-0056.

Everything a dashboard user would otherwise click together (goals, funnels,
attribution, journeys, segments, the launch annotation) lives in Umami's own
Postgres, which is not in git. This script is the source of truth for it: run
it after a restore, or after changing the event catalogue in the site
(app/utils/analytics.ts in ongehoord-ui-content), and it converges every
managed item by NAME. It updates the items that exist, creates the missing
ones, and leaves hand-made items (any other name) alone.

The same set is applied to the production and the acceptance website, so a
report can be tried on acceptance data before anyone relies on it.

Exit codes
----------
  0  every managed item is in place on every website
  1  some items failed (named on stderr); the rest were applied
  2  Umami unreachable, login failed, or a website is missing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

LOCAL = "http://127.0.0.1:3450"
WEBSITES = {
    "Ongehoord": "www.ongehoord.nl",
    "Ongehoord (acceptance)": "acceptance.ongehoord.nl",
}
LAUNCH = "2026-09-27T12:00:00.000Z"
LAUNCH_NOTE = "Umami live via the first-party proxy; Google Analytics removed. Nothing before this date is comparable."

# Saved reports need a date range; the dashboard lets you change it per view.
# A rolling 30 days is what opens by default.
_now = dt.datetime.now(dt.UTC)
RANGE = {
    "startDate": (_now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    "endDate": _now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
}


def goal(name: str, description: str, kind: str, value: str) -> dict:
    return {"type": "goal", "name": name, "description": description,
            "parameters": {**RANGE, "type": kind, "value": value}}


def step(kind: str, value: str, **filters: str) -> dict:
    s = {"type": kind, "value": value}
    if filters:
        s["filters"] = [{"property": k, "operator": "eq", "value": v} for k, v in filters.items()]
    return s


def funnel(name: str, description: str, steps: list[dict], window: int = 60) -> dict:
    return {"type": "funnel", "name": name, "description": description,
            "parameters": {**RANGE, "window": window, "steps": steps}}


# The questions an investigative NGO actually asks of its site, in the order
# they matter: does the work get READ, does reading turn into SUPPORT, and
# where do readers come from and go to.
REPORTS: list[dict] = [
    # --- goals: one number each ---------------------------------------------
    goal("Donation intent", "Chose a payment method: iDEAL, PayPal or copying the IBAN. The payment itself happens off-site, so this is the last step this site can see.", "event", "donate-method"),
    goal("Donate options opened", "The donate modal was opened, from any placement (see the event's `source` property).", "event", "donate-open"),
    goal("Donate page visited", "Reached /doneren directly (NL or EN).", "path", "/*/doneren"),
    goal("Investigation read to the end", "Scrolled to the end-of-article call to action.", "event", "investigation-complete"),
    goal("Video played", "Started one of the investigation films.", "event", "video-play"),
    goal("Page shared", "Used the share or copy-link button.", "event", "share"),
    goal("Contact form sent", "Submitted the contact form (property `status` says whether it went through).", "event", "contact-submit"),
    goal("Source checked", "Opened a citation popover. It shows readers verifying the claims.", "event", "source-open"),
    goal("Location opened on the map", "Opened a farm or slaughterhouse on the locations map.", "event", "location-open"),
    goal("Vegan Challenge click-out", "Followed the 'alternatives' call to action at the end of an investigation.", "event", "outbound"),
    # --- funnels: where people drop off --------------------------------------
    funnel("Reader → donor",
           "Of the people who open an investigation, how many finish it, open the donate options and choose a method.",
           [step("path", "/*/onderzoek/*"), step("event", "investigation-complete"),
            step("event", "donate-open"), step("event", "donate-method")], window=120),
    funnel("Donate options → payment method",
           "The donate modal's own conversion: opened, then a method chosen.",
           [step("event", "donate-open"), step("event", "donate-method")], window=30),
    funnel("Reader → sharer",
           "Opened an investigation, read it to the end, then shared it.",
           [step("path", "/*/onderzoek/*"), step("event", "investigation-complete"), step("event", "share")], window=120),
    funnel("Map → investigation",
           "Explored the map, opened a location, then went on to read an investigation.",
           [step("path", "/*/locaties"), step("event", "location-open"), step("path", "/*/onderzoek/*")], window=60),
    funnel("Contact page → message sent",
           "Reached /contact and sent a message successfully.",
           [step("path", "/*/contact"), step("event", "contact-submit", status="success")], window=60),
    # --- the rest ---------------------------------------------------------------
    {"type": "attribution", "name": "What brings donors",
     "description": "Which referrer, campaign and channel led to a donation intent (last click).",
     "parameters": {**RANGE, "model": "last-click", "type": "event", "step": "donate-method"}},
    {"type": "attribution", "name": "What brings readers who finish",
     "description": "First-click attribution for reading an investigation to the end.",
     "parameters": {**RANGE, "model": "first-click", "type": "event", "step": "investigation-complete"}},
    {"type": "journey", "name": "Paths to a donation",
     "description": "The pages and events visitors pass through before choosing a payment method.",
     "parameters": {**RANGE, "steps": 5, "endStep": "donate-method"}},
    {"type": "journey", "name": "Where readers go next",
     "description": "What visitors do after landing on the home page.",
     "parameters": {**RANGE, "steps": 5, "startStep": "/nl"}},
    {"type": "retention", "name": "Returning readers",
     "description": "How many visitors come back in the weeks after their first visit.",
     "parameters": {**RANGE}},
    {"type": "utm", "name": "Campaigns",
     "description": "utm_source / utm_medium / utm_campaign on inbound links: tag newsletter and social posts to see them here.",
     "parameters": {**RANGE}},
]

SEGMENTS: list[dict] = [
    {"name": "Investigation readers", "filters": [{"name": "path", "operator": "c", "value": "/onderzoek/"}]},
    {"name": "English site", "filters": [{"name": "path", "operator": "c", "value": "/en"}]},
    {"name": "Dutch site", "filters": [{"name": "path", "operator": "c", "value": "/nl"}]},
    {"name": "Map & locations", "filters": [{"name": "path", "operator": "c", "value": "/locaties"}]},
    {"name": "Mobile", "filters": [{"name": "device", "operator": "eq", "value": "mobile"}]},
    {"name": "Belgium", "filters": [{"name": "country", "operator": "eq", "value": "BE"}]},
]


class Api:
    def __init__(self, base: str, token: str | None = None):
        self.base, self.token = base, token

    def call(self, method: str, path: str, body: dict | None = None) -> object:
        headers = {"content-type": "application/json"}
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
        return json.loads(raw) if raw.strip() else None

    def rows(self, path: str) -> list[dict]:
        out = self.call("GET", path)
        return out.get("data", []) if isinstance(out, dict) else (out or [])


def converge(api: Api, website_id: str, dry_run: bool) -> list[str]:
    """Apply REPORTS, SEGMENTS and the launch annotation to one website."""
    failures: list[str] = []

    existing = {r["name"]: r for r in api.rows(f"/api/reports?websiteId={website_id}&pageSize=200")}
    for rep in REPORTS:
        body = {"websiteId": website_id, **rep}
        have = existing.get(rep["name"])
        verb = "update" if have else "create"
        print(f"    {verb:6} report   {rep['type']:11} {rep['name']}")
        if dry_run:
            continue
        try:
            api.call("POST", f"/api/reports/{have['id']}" if have else "/api/reports", body)
        except urllib.error.HTTPError as exc:
            failures.append(f"report {rep['name']!r}: {exc.code} {exc.read().decode()[:200]}")

    segs = {s["name"]: s for s in api.rows(f"/api/websites/{website_id}/segments?type=segment&pageSize=200")}
    for seg in SEGMENTS:
        body = {"type": "segment", "name": seg["name"], "parameters": {"filters": seg["filters"]}}
        have = segs.get(seg["name"])
        print(f"    {'update' if have else 'create':6} segment  {seg['name']}")
        if dry_run:
            continue
        try:
            path = f"/api/websites/{website_id}/segments" + (f"/{have['id']}" if have else "")
            api.call("POST", path, body)
        except urllib.error.HTTPError as exc:
            failures.append(f"segment {seg['name']!r}: {exc.code} {exc.read().decode()[:200]}")

    notes = api.rows(f"/api/websites/{website_id}/annotations")
    if not any(n.get("note") == LAUNCH_NOTE for n in notes):
        print("    create annotation (launch)")
        if not dry_run:
            try:
                api.call("POST", f"/api/websites/{website_id}/annotations",
                         {"date": LAUNCH, "allDay": True, "note": LAUNCH_NOTE})
            except urllib.error.HTTPError as exc:
                failures.append(f"annotation: {exc.code} {exc.read().decode()[:200]}")
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    args = ap.parse_args()

    user, password = os.environ.get("UMAMI_ADMIN_USER"), os.environ.get("UMAMI_ADMIN_PASSWORD")
    if not (user and password):
        print("!!! UMAMI_ADMIN_USER / UMAMI_ADMIN_PASSWORD are not set", file=sys.stderr)
        return 2
    try:
        token = Api(LOCAL).call("POST", "/api/auth/login", {"username": user, "password": password})["token"]
        api = Api(LOCAL, token)
        sites = {w["name"]: w for w in api.rows("/api/websites?pageSize=100")}
    except (urllib.error.URLError, KeyError, TypeError) as exc:
        print(f"!!! Umami unreachable or login failed: {exc}", file=sys.stderr)
        return 2

    failures: list[str] = []
    for name, domain in WEBSITES.items():
        site = sites.get(name)
        if site is None:
            print(f"!!! website {name!r} ({domain}) does not exist in Umami", file=sys.stderr)
            return 2
        print(f"==> {name} ({site['domain']}, {site['id']})")
        failures += [f"{name}: {f}" for f in converge(api, site["id"], args.dry_run)]

    for f in failures:
        print(f"!!! {f}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
