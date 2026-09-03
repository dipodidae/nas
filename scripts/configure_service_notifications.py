#!/usr/bin/env python3
r"""Put jellyseerr's and cleanuparr's notifiers into the shape ADR-0033 declares.

The \*arr suite has its own converger (`configure_arr_notifications.py`); these
two are different enough to keep separate — jellyseerr has two agents and a
double-encoded payload template, cleanuparr is an armed deletion engine whose
every deletion must produce exactly one message (ADR-0017).

Why jellyseerr's two agents are swapped relative to the obvious layout
----------------------------------------------------------------------
Reading `/app/dist/lib/notifications/agents/ntfy.js` inside the container rather
than assuming:

* the **native ntfy agent hardcodes `priority = 3`** in `buildPayload` and sets
  no tags at all. There is no setting for either.
* the **webhook agent** takes an arbitrary `authHeader` and an arbitrary JSON
  body template, so priority, tags and title are all controllable.

So the webhook agent serves **nas-requests** (which must be priority 4 — a
request waiting for approval is the "needs a human today" lane, and in this
design severity is carried by the priority) and the native ntfy agent serves
**nas-media** (whose priority is 3, exactly what the agent hardcodes). The cost
is that "now available" arrives without a `popcorn` tag and with jellyseerr's
own title rather than a custom one. That is decoration; the priority is the
contract.

Two traps, both already recorded in AGENTS.md and both re-hit here
------------------------------------------------------------------
* **`jsonPayload` must be double-encoded.** The runtime does
  `JSON.parse(JSON.parse(base64decode(stored)))`, but the settings API stores
  whatever plain JSON you send — so send `json.dumps(template_text)`, not the
  template text, or the runtime rejects it with
  `"[object Object]" is not valid JSON`.
* **The token must be re-sent on every write.** A GET omits or masks it.

Exit codes
----------
  0  both services match the desired state (or were brought to it)
  1  at least one differs (with --check) or could not be reached
  2  fatal (no credentials at all)

Usage
-----
  python scripts/configure_service_notifications.py --check
  python scripts/configure_service_notifications.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

if "NTFY_TOKEN_ARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


REPO_ROOT = Path(__file__).resolve().parent.parent
JELLYSEERR_URL = os.getenv("JELLYSEERR_HOST", "http://127.0.0.1:5056")
JELLYSEERR_SETTINGS = REPO_ROOT / ".docker-config" / "jellyseerr" / "settings.json"
CLEANUPARR_URL = os.getenv("CLEANUPARR_HOST", "http://127.0.0.1:11011")
NTFY_IN_NETWORK = "http://ntfy:8410"

# Jellyseerr's Notification bitmask, read from
# /app/dist/lib/notifications/index.js inside the container.
N_MEDIA_PENDING = 2
N_MEDIA_AVAILABLE = 8
N_MEDIA_FAILED = 16
N_MEDIA_DECLINED = 64
N_ISSUE_CREATED = 256
N_ISSUE_COMMENT = 512

# nas-requests: everything that needs a person to decide or look.
# Deliberately NOT MEDIA_APPROVED / MEDIA_AUTO_APPROVED — an approval is the
# system doing what it was told, and MEDIA_AVAILABLE covers the outcome.
REQUEST_TYPES = (
  N_MEDIA_PENDING | N_MEDIA_FAILED | N_MEDIA_DECLINED | N_ISSUE_CREATED | N_ISSUE_COMMENT
)
# nas-media: "you can watch it now" is new media, not a request update.
AVAILABLE_TYPES = N_MEDIA_AVAILABLE

# Cleanuparr's enums are PascalCase over the API even though cleanuparr.db
# stores them lowercase.
CLEANUPARR_PRIORITY_HIGH = "High"
CLEANUPARR_AUTH_TOKEN = "AccessToken"
# The three events that delete or strike something. onStalledStrike and
# onSlowStrike stay off: they fire dozens at a time and are in the UI event log
# anyway (docs/cleanuparr-configuration.md).
CLEANUPARR_WANTED_EVENTS = frozenset({
  "onFailedImportStrike", "onQueueItemDeleted", "onDownloadCleaned",
})


def _http(method: str, url: str, headers: dict[str, str], payload: dict | None = None):
  data = json.dumps(payload).encode() if payload is not None else None
  hdrs = dict(headers)
  if data:
    hdrs["Content-Type"] = "application/json"
  req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
  with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - localhost
    body = resp.read().decode("utf-8", "replace")
  if not body.strip():
    return None
  try:
    return json.loads(body)
  except ValueError:
    # cleanuparr answers HTTP 200 with its SPA index.html for every unknown
    # /api path, so a non-JSON body means the ROUTE is wrong, not the payload.
    # Returning it rather than raising is what makes that diagnosable.
    return {"_non_json": body[:200]}


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------


def request_payload_template(topic: str) -> str:
  """The nas-requests webhook body, as the template text jellyseerr stores."""
  return json.dumps({
    "topic": topic,
    "title": "Jellyseerr — {{subject}}",
    "message": "{{event}}\n{{message}}",
    "priority": 4,
    "tags": ["inbox_tray"],
  })


def encode_json_payload(template_text: str) -> str:
  """Double-encode, because jellyseerr's runtime parses twice.

  This is the whole bug AGENTS.md records: the settings API happily stores a
  raw template, and the runtime then fails with
  `"[object Object]" is not valid JSON` on every notification.
  """
  return json.dumps(template_text)


def decode_json_payload(stored: str) -> str:
  """Inverse of `encode_json_payload`, tolerant of a single-encoded value."""
  try:
    inner = json.loads(stored)
  except (ValueError, TypeError):
    return stored
  return inner if isinstance(inner, str) else stored


def jellyseerr_api_key(path: Path = JELLYSEERR_SETTINGS) -> str:
  try:
    return str(json.loads(path.read_text())["main"]["apiKey"])
  except (OSError, ValueError, KeyError):
    return ""


# --------------------------------------------------------------------------
# Jellyseerr
# --------------------------------------------------------------------------


def converge_jellyseerr(token: str, apply: bool) -> list[str]:
  """Returns a list of findings; empty means it already matches."""
  findings: list[str] = []
  key = jellyseerr_api_key()
  if not key:
    return [f"jellyseerr: no API key in {JELLYSEERR_SETTINGS}"]
  headers = {"X-Api-Key": key}
  base = f"{JELLYSEERR_URL}/api/v1/settings/notifications"
  requests_topic = os.getenv("NTFY_TOPIC_REQUESTS") or "nas-requests"
  media_topic = os.getenv("NTFY_TOPIC_MEDIA") or "nas-media"

  # --- the webhook agent -> nas-requests --------------------------------
  wanted_webhook = {
    "enabled": True,
    "types": REQUEST_TYPES,
    "options": {
      "webhookUrl": NTFY_IN_NETWORK,
      "authHeader": f"Bearer {token}",
      "jsonPayload": encode_json_payload(request_payload_template(requests_topic)),
    },
  }
  try:
    current = _http("GET", f"{base}/webhook", headers) or {}
  except (OSError, urllib.error.HTTPError, ValueError) as exc:
    return [f"jellyseerr: unreachable ({exc})"]

  opts = current.get("options") or {}
  stored_template = decode_json_payload(str(opts.get("jsonPayload") or ""))
  differs = (
    not current.get("enabled")
    or int(current.get("types") or 0) != REQUEST_TYPES
    or opts.get("webhookUrl") != NTFY_IN_NETWORK
    or requests_topic not in stored_template
    or '"priority": 4' not in stored_template
  )
  if differs:
    findings.append(
      f"jellyseerr webhook: types={current.get('types')} url={opts.get('webhookUrl')!r} "
      f"-> want types={REQUEST_TYPES} on {requests_topic} at priority 4"
    )
    if apply:
      _http("POST", f"{base}/webhook", headers, wanted_webhook)

  # --- the native ntfy agent -> nas-media -------------------------------
  wanted_ntfy = {
    "enabled": True,
    "types": AVAILABLE_TYPES,
    "options": {
      "url": NTFY_IN_NETWORK,
      "topic": media_topic,
      "authMethodToken": True,
      "authMethodUsernamePassword": False,
      "token": token,
      "username": "",
      "password": "",
    },
  }
  try:
    current = _http("GET", f"{base}/ntfy", headers) or {}
  except (OSError, urllib.error.HTTPError, ValueError) as exc:
    findings.append(f"jellyseerr ntfy agent: unreachable ({exc})")
    return findings
  opts = current.get("options") or {}
  differs = (
    not current.get("enabled")
    or int(current.get("types") or 0) != AVAILABLE_TYPES
    or opts.get("topic") != media_topic
    or opts.get("url") != NTFY_IN_NETWORK
    or not opts.get("authMethodToken")
  )
  if differs:
    findings.append(
      f"jellyseerr ntfy: enabled={current.get('enabled')} types={current.get('types')} "
      f"topic={opts.get('topic')!r} -> want types={AVAILABLE_TYPES} on {media_topic}"
    )
    if apply:
      _http("POST", f"{base}/ntfy", headers, wanted_ntfy)

  return findings


# --------------------------------------------------------------------------
# Cleanuparr
# --------------------------------------------------------------------------


def converge_cleanuparr(token: str, apply: bool) -> list[str]:
  """Point cleanuparr's ntfy provider at nas-attention.

  It is an armed deletion engine with `dryRun: false` (ADR-0017), so the rule
  is that **every deletion it performs produces exactly one notification**.
  The three enabled events are the three that delete or strike something;
  `onStalledStrike`/`onSlowStrike` stay off because they fire dozens at a time
  and are visible in the UI's own event log.
  """
  findings: list[str] = []
  key = os.getenv("API_KEY_CLEANUPARR", "")
  if not key:
    return ["cleanuparr: API_KEY_CLEANUPARR unset"]
  headers = {"X-Api-Key": key}
  # Discovered by grepping /app/wwwroot/main-*.js inside the container: every
  # unknown /api path returns the SPA's index.html with HTTP 200, so probing
  # cannot distinguish a wrong route from an empty one.
  url = f"{CLEANUPARR_URL}/api/configuration/notification_providers"
  topic = os.getenv("NTFY_TOPIC_ATTENTION") or "nas-attention"
  try:
    data = _http("GET", url, headers) or {}
  except (OSError, urllib.error.HTTPError, ValueError) as exc:
    return [f"cleanuparr: unreachable ({exc})"]

  providers = data.get("providers") or []
  ntfy = next((p for p in providers if str(p.get("type", "")).lower() == "ntfy"), None)
  if ntfy is None:
    return ["cleanuparr: no ntfy provider configured at all"]

  config = ntfy.get("configuration") or {}
  topics = config.get("topics") or []
  events = ntfy.get("events") or {}
  on_now = {k for k, v in events.items() if v is True}
  differs = (
    topics != [topic]
    or config.get("priority") != CLEANUPARR_PRIORITY_HIGH
    or on_now != CLEANUPARR_WANTED_EVENTS
    or not ntfy.get("isEnabled")
    or config.get("authenticationType") != CLEANUPARR_AUTH_TOKEN
  )
  if not differs:
    return findings

  findings.append(
    f"cleanuparr ntfy: topics={topics} priority={config.get('priority')!r} "
    f"auth={config.get('authenticationType')!r} events={sorted(on_now)} "
    f"-> want [{topic}] priority={CLEANUPARR_PRIORITY_HIGH} "
    f"auth={CLEANUPARR_AUTH_TOKEN} events={sorted(CLEANUPARR_WANTED_EVENTS)}"
  )
  if apply:
    # The UPDATE DTO is FLAT -- name/serverUrl/topics/... plus the event flags
    # at the top level -- and NOT the nested {configuration, events} shape the
    # GET returns. Feeding the GET's own shape back returns HTTP 400. Read off
    # the UI's own submit handler in /app/wwwroot/chunk-*.js; the enum values
    # are PascalCase ("High", "AccessToken"), not the lowercase forms stored in
    # cleanuparr.db.
    body = {
      "name": ntfy.get("name") or "ntfy (nas-attention)",
      "isEnabled": True,
      "serverUrl": NTFY_IN_NETWORK,
      "topics": [topic],
      "authenticationType": CLEANUPARR_AUTH_TOKEN,
      "accessToken": token,
      "priority": CLEANUPARR_PRIORITY_HIGH,
      "tags": ["wastebasket"],
    }
    for flag in events:
      body[flag] = flag in CLEANUPARR_WANTED_EVENTS
    for flag in CLEANUPARR_WANTED_EVENTS:
      body[flag] = True
    result = _http("PUT", f"{url}/ntfy/{ntfy['id']}", headers, body)
    if isinstance(result, dict) and "_non_json" in result:
      findings.append(
        f"cleanuparr: PUT hit the SPA fallback, not the API: {result['_non_json'][:80]}"
      )
  return findings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Converge jellyseerr + cleanuparr notifiers.")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--apply", action="store_true", help="Write the desired state.")
  group.add_argument("--check", action="store_true", help="Report differences only (default).")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  token = os.getenv("NTFY_TOKEN_ARR", "")
  if not token:
    print("ERROR: NTFY_TOKEN_ARR is not set", file=sys.stderr)
    return 2

  findings = converge_jellyseerr(token, args.apply) + converge_cleanuparr(token, args.apply)
  if not findings:
    print("  ok: jellyseerr and cleanuparr match")
    return 0
  for finding in findings:
    print(f"  {'fixed' if args.apply else 'DIFFERS'}: {finding}")
  return 0 if args.apply else 1


if __name__ == "__main__":
  sys.exit(main())
