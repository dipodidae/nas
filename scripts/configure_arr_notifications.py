#!/usr/bin/env python3
r"""Put each *arr's notification connectors into the shape ADR-0033 declares.

Why this is a script and not a one-off curl
-------------------------------------------
Because it has to be re-runnable. \*arr connector settings live in each app's
SQLite database, a UI toggle can change them at any time, and the whole point of
the six-lane split is that "On Grab" must not quietly come back on. So the
desired state is declared here, applied idempotently, and asserted by
`make verify-runtime` (which calls this file with `--check`).

Two rules it follows, both learned the hard way in this repo:

* **Payloads are built from the LIVE schema**, `GET /notification/schema`, not
  from remembered field names. The four apps genuinely differ — Lidarr has
  `onImportFailure`/`onDownloadFailure` and no `onManualInteractionRequired`;
  Sonarr and Radarr are the exact inverse; Prowlarr supports only
  `onGrab`/`onHealthIssue`/`onHealthRestored`/`onApplicationUpdate`, every one of
  which is on the do-not-notify list. A trigger flag that does not exist on an
  app is silently dropped by the API, so guessing produces a connector that
  looks configured and fires for nothing.
* **`GET /notification` masks secrets** as `********`. Writing a masked value
  back stores the literal asterisks, and the connector then fails
  authentication while the UI shows a saved token. Every write re-injects the
  real token from `.env`.

Health triggers stay OFF, deliberately
--------------------------------------
`onHealthIssue` / `onHealthRestored` are **not** enabled, on any app. ADR-0032
switched them off eleven days after they produced 103 messages in 48 hours for
two dead indexers: all three apps raise the same indexer warning, the connector
has no filtering, and it fires on every transition. `stack_watchdog.py` owns
\*arr health now — `check_arr_health` + `check_indexer_failures`, deduped per
app, damped past a duration threshold, and routed to `nas-attention` — so the
lane is covered without the flapping. Turning them back on here would restore
the exact noise the taxonomy exists to remove.

Exit codes
----------
  0  every app matches the desired state (or was brought to it)
  1  at least one app could not be reached or does not match (with --check)
  2  fatal (no API key at all, so nothing could be checked)

Usage
-----
  python scripts/configure_arr_notifications.py --check     # assert only
  python scripts/configure_arr_notifications.py --apply     # converge
  python scripts/configure_arr_notifications.py --apply --test-first
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field

if "NTFY_TOKEN_ARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


# The path the script is mounted at inside every one of the three containers.
CUSTOM_SCRIPT_PATH = "/custom-scripts/arr_notify.sh"
ATTENTION_NAME = "ntfy — attention"
MEDIA_NAME = "ntfy — media (custom script)"
# ntfy priority as the *arr select field expresses it: 1 Min .. 5 Max.
PRIORITY_HIGH = 4
# In-network, because these run in containers. ntfy listens on :8410 and not
# :80 — it runs as ${PUID}:${PGID} (ADR-0014) and cannot bind a low port.
NTFY_IN_NETWORK = "http://ntfy:8410"
JELLYFIN_CLICK_DEFAULT = "https://jellyfin.4eva.me"

# Triggers that must NEVER be on, whatever else changes. This is the actual
# noise fix, so it is a denylist and not a comment.
FORBIDDEN_TRIGGERS = frozenset({
  "onGrab",
  "onRename",
  "onTrackRetag",
  "onApplicationUpdate",
  "onHealthIssue",
  "onHealthRestored",
  "onArtistAdd",
  "onSeriesAdd",
  "onMovieAdded",
})


@dataclass(frozen=True)
class App:
  name: str
  base: str
  api: str
  key_env: str
  # Wanted triggers for the native Ntfy "attention" connector. Empty means the
  # app has nothing worth an attention message and the connector is removed.
  attention: tuple[str, ...]
  # Wanted triggers for the CustomScript "media" connector.
  media: tuple[str, ...]


APPS: tuple[App, ...] = (
  App(
    "sonarr", "http://localhost:8989", "v3", "API_KEY_SONARR",
    attention=("onManualInteractionRequired",),
    media=("onImportComplete", "onUpgrade"),
  ),
  App(
    "radarr", "http://localhost:7878", "v3", "API_KEY_RADARR",
    attention=("onManualInteractionRequired",),
    # Radarr has no onImportComplete: `onDownload` IS its import event.
    media=("onDownload", "onUpgrade"),
  ),
  App(
    "lidarr", "http://localhost:8686", "v1", "API_KEY_LIDARR",
    # Lidarr is the only one with failure triggers, and the only one WITHOUT
    # onManualInteractionRequired.
    attention=("onImportFailure", "onDownloadFailure"),
    # Lidarr's media messages come from process_soulseek_imports.py instead:
    # its only download client is slskd, so almost nothing arrives through
    # Lidarr's own import pipeline, and this box does hundreds of music imports
    # a day. See the module docstring in that script.
    media=(),
  ),
  App(
    "prowlarr", "http://localhost:9696", "v1", "API_KEY_PROWLARR",
    # Prowlarr supports only onGrab / onHealthIssue / onHealthRestored /
    # onApplicationUpdate. Every one is excluded, so there is nothing for a
    # connector to carry and the existing zero-trigger one is deleted.
    attention=(),
    media=(),
  ),
)


@dataclass
class Finding:
  app: str
  message: str
  fixed: bool = False


@dataclass
class Outcome:
  findings: list[Finding] = field(default_factory=list)
  unreachable: list[str] = field(default_factory=list)

  @property
  def ok(self) -> bool:
    return not self.findings and not self.unreachable


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _request(method: str, url: str, key: str, payload: dict | None = None):
  data = json.dumps(payload).encode() if payload is not None else None
  headers = {"X-Api-Key": key}
  if data:
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, data=data, method=method, headers=headers)
  with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - localhost
    body = resp.read().decode("utf-8", "replace")
  return json.loads(body) if body.strip() else None


def get(app: App, key: str, path: str):
  return _request("GET", f"{app.base}/api/{app.api}{path}", key)


# --------------------------------------------------------------------------
# Pure logic
# --------------------------------------------------------------------------


def schema_for(schemas: list[dict], implementation: str) -> dict | None:
  return next((s for s in schemas if s.get("implementation") == implementation), None)


def supported_triggers(schema: dict) -> set[str]:
  """Which `onXxx` flags this app's implementation actually supports.

  Read off `supportsOnXxx: true`, because an unsupported flag in a PUT body is
  accepted and dropped — the connector then looks configured and fires for
  nothing.
  """
  out = set()
  for k, v in schema.items():
    if k.startswith("supports") and v is True:
      name = k[len("supports"):]
      out.add(name[0].lower() + name[1:])
  return out


def active_triggers(connector: dict) -> set[str]:
  return {k for k, v in connector.items() if k.startswith("on") and v is True}


def desired_triggers(wanted: tuple[str, ...], schema: dict) -> set[str]:
  """Wanted triggers, minus anything this app cannot do. Pure."""
  return set(wanted) & supported_triggers(schema)


def build_payload(
  schema: dict,
  name: str,
  triggers: set[str],
  fields: dict[str, object],
  existing_id: int | None = None,
) -> dict:
  """A full connector body, built from the schema's own field list.

  Starts from the schema so every field the app expects is present with its
  default, then overrides only what we care about. Building the dict by hand
  instead drops fields the app then reads as empty.
  """
  payload = {k: v for k, v in schema.items() if not k.startswith("supports")}
  payload["name"] = name
  payload["tags"] = payload.get("tags") or []
  for flag in list(payload):
    if flag.startswith("on") and isinstance(payload[flag], bool):
      payload[flag] = flag in triggers
  schema_fields = {f["name"]: dict(f) for f in schema.get("fields") or []}
  out_fields = []
  for fname, fdef in schema_fields.items():
    value = fields.get(fname, fdef.get("value"))
    entry = {"name": fname, "value": value}
    out_fields.append(entry)
  payload["fields"] = out_fields
  if existing_id is not None:
    payload["id"] = existing_id
  return payload


def field_value(connector: dict, name: str):
  for f in connector.get("fields") or []:
    if f.get("name") == name:
      return f.get("value")
  return None


# --------------------------------------------------------------------------
# Convergence
# --------------------------------------------------------------------------


def _ntfy_fields(token: str, topic: str, tags: list[str]) -> dict[str, object]:
  return {
    "serverUrl": NTFY_IN_NETWORK,
    "accessToken": token,
    # userName/password stay empty: a token is scoped and revocable on its own,
    # a shared password is neither (AGENTS.md).
    "userName": "",
    "password": "",
    "priority": PRIORITY_HIGH,
    "topics": [topic],
    "tags": tags,
    "clickUrl": "",
  }


def converge_app(app: App, key: str, token: str, apply: bool) -> Outcome:
  """Bring one app to the desired state, or report how it differs."""
  outcome = Outcome()
  try:
    schemas = get(app, key, "/notification/schema") or []
    existing = get(app, key, "/notification") or []
  except (OSError, urllib.error.HTTPError, ValueError) as exc:
    outcome.unreachable.append(f"{app.name}: {exc}")
    return outcome

  attention_topic = os.getenv("NTFY_TOPIC_ATTENTION") or "nas-attention"

  # --- 1. anything forbidden, anywhere, on any connector -----------------
  for conn in existing:
    bad = sorted(active_triggers(conn) & FORBIDDEN_TRIGGERS)
    if not bad:
      continue
    # The Jellyfin (MediaBrowser) connector legitimately uses onRename and the
    # delete events -- those drive library refreshes, not notifications, and
    # ADR-0016/the jellyfin audit depend on them. Only notification connectors
    # are in scope here.
    if conn.get("implementation") == "MediaBrowser":
      continue
    outcome.findings.append(
      Finding(app.name, f"{conn.get('name')!r} has forbidden trigger(s) {bad}")
    )
    if apply:
      body = dict(conn)
      for flag in bad:
        body[flag] = False
      _request("PUT", f"{app.base}/api/{app.api}/notification/{conn['id']}", key, body)
      outcome.findings[-1].fixed = True

  # --- 2. the native Ntfy "attention" connector --------------------------
  # There may be MORE than one: this stack had `ntfy — alerts` and
  # `ntfy — media` side by side. Exactly one Ntfy connector survives, the
  # attention one; every other is deleted, because nas-media is now served by
  # the CustomScript connector and two publishers for one lane means every
  # import arrives twice.
  ntfy_schema = schema_for(schemas, "Ntfy")
  ntfy_all = [c for c in existing if c.get("implementation") == "Ntfy"]
  current = next(
    (c for c in ntfy_all if c.get("name") == ATTENTION_NAME),
    next((c for c in ntfy_all if "alert" in str(c.get("name", "")).lower()), None),
  ) or (ntfy_all[0] if ntfy_all else None)
  for extra in ntfy_all:
    if current is not None and extra.get("id") == current.get("id"):
      continue
    outcome.findings.append(Finding(
      app.name,
      f"{extra.get('name')!r} is a second Ntfy connector; nas-media is served by "
      f"the custom script now, so this one would double-notify — removing it",
    ))
    if apply:
      _request("DELETE", f"{app.base}/api/{app.api}/notification/{extra['id']}", key)
      outcome.findings[-1].fixed = True
  wanted = desired_triggers(app.attention, ntfy_schema) if ntfy_schema else set()

  if not wanted:
    if current is not None:
      outcome.findings.append(Finding(
        app.name,
        f"{current.get('name')!r} carries no enabled trigger this app supports "
        f"— every one available is on the do-not-notify list; removing it",
      ))
      if apply:
        _request("DELETE", f"{app.base}/api/{app.api}/notification/{current['id']}", key)
        outcome.findings[-1].fixed = True
  else:
    fields = _ntfy_fields(token, attention_topic, ["warning"])
    needs_write = current is None or active_triggers(current) != wanted or [
      field_value(current, "topics"), field_value(current, "priority")
    ] != [[attention_topic], PRIORITY_HIGH]
    if needs_write:
      outcome.findings.append(Finding(
        app.name,
        f"attention connector differs: have "
        f"{sorted(active_triggers(current)) if current else 'nothing'}, "
        f"want {sorted(wanted)} on {attention_topic}",
      ))
      if apply:
        payload = build_payload(
          ntfy_schema, ATTENTION_NAME, wanted, fields,
          existing_id=current["id"] if current else None,
        )
        if current:
          _request(
            "PUT", f"{app.base}/api/{app.api}/notification/{current['id']}", key, payload)
        else:
          _request("POST", f"{app.base}/api/{app.api}/notification", key, payload)
        outcome.findings[-1].fixed = True

  # --- 3. the CustomScript "media" connector -----------------------------
  cs_schema = schema_for(schemas, "CustomScript")
  cs_current = next((c for c in existing if c.get("implementation") == "CustomScript"), None)
  cs_wanted = desired_triggers(app.media, cs_schema) if cs_schema else set()

  if not cs_wanted:
    if cs_current is not None:
      outcome.findings.append(
        Finding(app.name, f"{cs_current.get('name')!r} should not exist on this app; removing")
      )
      if apply:
        _request("DELETE", f"{app.base}/api/{app.api}/notification/{cs_current['id']}", key)
        outcome.findings[-1].fixed = True
  else:
    needs_write = (
      cs_current is None
      or active_triggers(cs_current) != cs_wanted
      or field_value(cs_current, "path") != CUSTOM_SCRIPT_PATH
    )
    if needs_write:
      outcome.findings.append(Finding(
        app.name,
        f"media connector differs: have "
        f"{sorted(active_triggers(cs_current)) if cs_current else 'nothing'} at "
        f"{field_value(cs_current, 'path') if cs_current else '-'}, "
        f"want {sorted(cs_wanted)} at {CUSTOM_SCRIPT_PATH}",
      ))
      if apply:
        payload = build_payload(
          cs_schema, MEDIA_NAME, cs_wanted,
          {"path": CUSTOM_SCRIPT_PATH, "arguments": ""},
          existing_id=cs_current["id"] if cs_current else None,
        )
        if cs_current:
          _request(
            "PUT", f"{app.base}/api/{app.api}/notification/{cs_current['id']}", key, payload)
        else:
          _request("POST", f"{app.base}/api/{app.api}/notification", key, payload)
        outcome.findings[-1].fixed = True

  return outcome


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Converge *arr notification connectors.")
  group = parser.add_mutually_exclusive_group()
  group.add_argument("--apply", action="store_true", help="Write the desired state.")
  group.add_argument(
    "--check", action="store_true", help="Report differences only (the default).",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  apply = args.apply
  token = os.getenv("NTFY_TOKEN_ARR", "")
  if apply and not token:
    print("ERROR: NTFY_TOKEN_ARR is not set; refusing to write a blank token",
          file=sys.stderr)
    return 2

  keys = {app.name: os.getenv(app.key_env, "") for app in APPS}
  if not any(keys.values()):
    print("ERROR: no API_KEY_* set for any *arr; nothing can be checked", file=sys.stderr)
    return 2

  rc = 0
  for app in APPS:
    key = keys[app.name]
    if not key:
      print(f"  {app.name}: {app.key_env} unset — skipped")
      rc = 1
      continue
    outcome = converge_app(app, key, token, apply)
    for problem in outcome.unreachable:
      print(f"  !!! {problem}")
      rc = 1
    for finding in outcome.findings:
      mark = "fixed" if finding.fixed else "DIFFERS"
      print(f"  {mark}: {finding.app}: {finding.message}")
      if not finding.fixed:
        rc = 1
    if outcome.ok:
      print(f"  ok: {app.name} matches")
  return rc


if __name__ == "__main__":
  sys.exit(main())
