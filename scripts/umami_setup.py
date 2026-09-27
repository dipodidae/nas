#!/usr/bin/env python3
"""Configure Umami's reports for ongehoord.nl as code. `make umami-setup` (runs in .venv: needs PyYAML). ADR-0056.

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
import uuid
from pathlib import Path

import yaml

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
    goal("Donate page visited (NL)", "Reached /nl/doneren.", "path", "/nl/doneren"),
    goal("Donate page visited (EN)", "Reached /en/doneren.", "path", "/en/doneren"),
    goal("Investigation read to the end", "Scrolled to the end-of-article call to action.", "event", "investigation-complete"),
    goal("Video played", "Started one of the investigation films.", "event", "video-play"),
    goal("Page shared", "Used the share or copy-link button.", "event", "share"),
    goal("Donation completed (PayPal)", "Came back from PayPal to /bedankt after paying: the one CONFIRMED donation signal (iDEAL and bank transfers end off-site).", "event", "donate-complete"),
    goal("Contact form sent", "The contact form went through (failures are the separate `contact-error` event).", "event", "contact-submit"),
    goal("Source checked", "Opened a citation popover. It shows readers verifying the claims.", "event", "source-open"),
    goal("Location opened on the map", "Opened a farm or slaughterhouse on the locations map.", "event", "location-open"),
    goal("Vegan Challenge click-out", "Followed the 'alternatives' call to action at the end of an investigation.", "event", "vegan-challenge-click"),
    goal("Read at least half an investigation", "Scrolled past 50% of an investigation (any `slug`).", "event", "investigation-progress"),
    # --- funnels: where people drop off --------------------------------------
    *[f for lang in ("nl", "en") for f in (
        funnel(f"Reader → donor ({lang.upper()})",
               "STRICT: opened an investigation, finished it, THEN opened the donate options and chose a method. Compare with the loose twin: the gap is what finishing the story is worth.",
               [step("path", f"/{lang}/onderzoek/*"), step("event", "investigation-complete"),
                step("event", "donate-open"), step("event", "donate-method")], window=240),
        funnel(f"Reader → donor, at any point ({lang.upper()})",
               "LOOSE: opened an investigation, then donate options and a method, whether or not they finished reading.",
               [step("path", f"/{lang}/onderzoek/*"), step("event", "donate-open"), step("event", "donate-method")], window=240),
        funnel(f"Reader → sharer ({lang.upper()})",
               "Opened an investigation, read it to the end, then shared it.",
               [step("path", f"/{lang}/onderzoek/*"), step("event", "investigation-complete"), step("event", "share")], window=120),
        funnel(f"Map → investigation ({lang.upper()})",
               "Explored the map, opened a location, then went on to read an investigation.",
               [step("path", f"/{lang}/locaties*"), step("event", "location-open"), step("path", f"/{lang}/onderzoek/*")], window=60),
        funnel(f"Contact page → message sent ({lang.upper()})",
               "Reached /contact and sent a message successfully.",
               [step("path", f"/{lang}/contact"), step("event", "contact-submit", status="success")], window=60),
    )],
    funnel("Donate options → payment method",
           "The donate modal's own conversion: opened, then a method chosen.",
           [step("event", "donate-open"), step("event", "donate-method")], window=30),
    funnel("Donate options → PayPal → completed",
           "Opened the donate options, chose PayPal, and came back paid. PayPal is the only method whose completion this site can see.",
           [step("event", "donate-open"), step("event", "donate-method", method="paypal"), step("event", "donate-complete")], window=60),
    funnel("Read depth",
           "How far into an investigation readers get: 25%, 50%, 75%, then the end. Filter by `slug` (event property) for one piece.",
           [step("event", "investigation-progress", depth="25"), step("event", "investigation-progress", depth="50"),
            step("event", "investigation-progress", depth="75"), step("event", "investigation-complete")], window=180),
    funnel("Film watched",
           "Of the people who start a film, how many reach 25/50/75% and the end. Filter by `video` for one film.",
           [step("event", "video-play"), step("event", "video-progress", percent="25"), step("event", "video-progress", percent="50"),
            step("event", "video-progress", percent="75"), step("event", "video-progress", percent="100")], window=120),
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

# Names this script used to manage. Umami's path matching supports a TRAILING
# `*` only (a prefix match, verified against live data: `/*/onderzoek/*`
# matched nothing), so the locale-agnostic versions were split per language.
RETIRED = {"Donate page visited", "Reader → donor", "Reader → sharer", "Map → investigation", "Contact page → message sent"}

# Anchored regexes (operator `re`), not `contains`: "/en" is also inside
# /nl/locaties/enschede-..., and a segment that quietly mixes languages is
# worse than none.
SEGMENTS: list[dict] = [
    {"name": "Investigation readers", "filters": [{"name": "path", "operator": "re", "value": "^/(nl|en)/onderzoek/"}]},
    {"name": "English site", "filters": [{"name": "path", "operator": "re", "value": "^/en(/|$)"}]},
    {"name": "Dutch site", "filters": [{"name": "path", "operator": "re", "value": "^/nl(/|$)"}]},
    {"name": "Map & locations", "filters": [{"name": "path", "operator": "re", "value": "^/(nl|en)/locaties"}]},
    {"name": "Mobile", "filters": [{"name": "device", "operator": "eq", "value": "mobile"}]},
    {"name": "Belgium", "filters": [{"name": "country", "operator": "eq", "value": "BE"}]},
    # `utmSource`, camelCase: `utm_source` is not a filter name and is silently
    # ignored (the segment then matches everyone). Verified on nas-canary.
    {"name": "Came from a share", "filters": [{"name": "utmSource", "operator": "eq", "value": "share"}]},
]


# The site's content tree is this repo's submodule. Every PUBLISHED
# investigation's launch date becomes an annotation, so a traffic spike on a
# chart sits next to the piece that caused it. Drafts and anything still under
# embargo are skipped: a title must never show up here before it is public.
CONTENT = Path(__file__).resolve().parent.parent / "webapps/ongehoord/src/content/nl/onderzoek"


def launches(content: Path = CONTENT, today: dt.date | None = None) -> list[tuple[str, str]]:
    """[(ISO date, note)] for every published, released investigation. Pure apart from reading files."""
    today = today or dt.datetime.now(dt.UTC).date()
    out = []
    for index in sorted(content.glob("*/index.md")):
        text = index.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        meta = yaml.safe_load(text.split("---", 2)[1]) or {}
        if meta.get("published") is not True or meta.get("publishAt"):
            continue
        date = meta.get("date")
        date = date if isinstance(date, dt.date) else dt.date.fromisoformat(str(date)) if date else None
        if not date or date > today:
            continue
        out.append((f"{date.isoformat()}T12:00:00.000Z", f"Investigation published: {meta.get('title', index.parent.name)}"))
    return out


BOARD_NAME = "Impact"
BOARD_TEXT = (
    "Ongehoord in one screen. Top to bottom: how many people the work reaches, "
    "whether they read it, whether reading turns into support, and where they came from. "
    "Donation intent is a click on a payment method; only PayPal completions are confirmed. "
    "Rebuilt by `make umami-setup` on the NAS, so edit it there, not here."
)


def _col(component_type: str, **props: object) -> dict:
    return {"id": str(uuid.uuid4()), "component": {"type": component_type, "props": props}}


def board_rows(report_ids: dict[str, str]) -> list[dict]:
    """The Impact board layout. Tiles whose report does not exist are left out."""
    def goal(name: str) -> dict | None:
        return _col("Goal", reportId=report_ids[name]) if name in report_ids else None

    def fun(name: str) -> dict | None:
        return _col("Funnel", reportId=report_ids[name]) if name in report_ids else None

    rows = [
        [_col("TextBlock", text=BOARD_TEXT)],
        [_col("WebsiteMetricsBar")],
        [_col("WebsiteChart")],
        [goal("Investigation read to the end"), goal("Donation intent"), goal("Donation completed (PayPal)")],
        [fun("Read depth"), fun("Reader → donor, at any point (NL)")],
        [fun("Film watched"), goal("Page shared"), goal("Source checked")],
        [_col("MetricsTable", type="path", limit="10"), _col("MetricsTable", type="referrer", limit="10")],
        [_col("UTM", param="utm_source", limit=10), _col("WorldMap")],
        [_col("EventsChart")],
    ]
    return [{"id": str(uuid.uuid4()), "columns": [c for c in r if c]} for r in rows if any(r)]


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
    for name in sorted(RETIRED & existing.keys()):
        print(f"    delete report   {name} (retired)")
        if not dry_run:
            api.call("DELETE", f"/api/reports/{existing[name]['id']}")
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

    reports = {r["name"]: r["id"] for r in api.rows(f"/api/reports?websiteId={website_id}&pageSize=200")}
    boards = [b for b in api.rows("/api/boards?pageSize=200")
              if b.get("name") == BOARD_NAME and (b.get("parameters") or {}).get("websiteId") == website_id]
    body = {"type": "website", "name": BOARD_NAME, "description": "Reach, reading, support, sources.",
            "parameters": {"websiteId": website_id, "rows": board_rows(reports)}}
    print(f"    {'update' if boards else 'create':6} board    {BOARD_NAME}")
    if not dry_run:
        try:
            api.call("POST", f"/api/boards/{boards[0]['id']}" if boards else "/api/boards", body)
        except urllib.error.HTTPError as exc:
            failures.append(f"board: {exc.code} {exc.read().decode()[:200]}")

    have_notes = {n.get("note") for n in api.rows(f"/api/websites/{website_id}/annotations?pageSize=1000")}
    for date, note in [(LAUNCH, LAUNCH_NOTE), *launches()]:
        if note in have_notes:
            continue
        print(f"    create annotation {date[:10]} {note[:60]}")
        if not dry_run:
            try:
                api.call("POST", f"/api/websites/{website_id}/annotations", {"date": date, "allDay": True, "note": note})
            except urllib.error.HTTPError as exc:
                failures.append(f"annotation {note!r}: {exc.code} {exc.read().decode()[:200]}")
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
