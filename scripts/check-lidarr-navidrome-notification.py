#!/usr/bin/env python3
"""Assert Lidarr still tells Navidrome to rescan when an album lands (ADR-0049).

Used by `make verify-runtime`.

Lidarr's `Subsonic` connector is what makes a fresh import show up in Navidrome
without waiting for the hourly `ND_SCANNER_SCHEDULE` sweep. It lives in Lidarr's
SQLite DB, and the Navidrome account it authenticates as lives in Navidrome's
SQLite DB -- neither is in this repo, so a config restore, a tag bump or someone
tidying users in the Navidrome UI can undo it without touching a tracked file.

Four things are checked, and every one of them fails SILENTLY in production:

1. `updateLibrary` is the only field on the connector that does anything.
   With it off, Lidarr saves cleanly, the Test button is green, and no scan is
   ever requested.

2. Only `OnReleaseImport` and `OnRename` call `_proxy.Update()` -- verified
   against Lidarr's own Subsonic.cs. Every other trigger calls `Notify()` only,
   which is gated on `Settings.Notify` and posts a Subsonic *chat message*.
   So `onReleaseImport` off means no scan on import, and a ticked
   `onTrackRetag` / `onArtistDelete` / `onAlbumDelete` is a no-op that reads
   like a working feature. Both directions are asserted.

3. The Navidrome principal must still hold `adminRole`. Navidrome 0.64 gates
   `/rest/startScan` on `is_admin` and ships no scan-only role: demoted, the
   same call returns Subsonic error 50 while `ping` and `getScanStatus` keep
   answering 200 -- so Lidarr logs nothing, Navidrome logs a warning nobody
   reads, and albums simply stop appearing until the next hourly scan.

4. The credentials in `.env` must still authenticate. Lidarr sends the password
   as a plaintext `p=` query parameter, so a rotation in the Navidrome UI that
   is not mirrored into `.env` (and into the connector) breaks it.

This probes `getUser`, never `startScan`: the check must not kick off a library
scan every time `make verify-runtime` runs.

Exit codes
----------
  0  the connector is wired and the Navidrome principal can still scan
  1  a setting drifted, or the principal lost adminRole
  2  Lidarr or Navidrome unreachable, or a required env var is unset
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_NAVIDROME_HOST = "http://localhost:4533"

IMPLEMENTATION = "Subsonic"
# What the connector must point at. Container name on nas-network, not a
# subdomain: the password rides in the query string, so it must never leave the
# bridge network via SWAG. ADR-0044 leaves /rest un-gated for Subsonic clients,
# which makes navidrome.<domain>/rest reachable and therefore tempting.
WANT_FIELDS = {"host": "navidrome", "port": 4533, "useSsl": False, "updateLibrary": True}
# The two triggers that reach _proxy.Update(). onUpgrade rides along: it is what
# lets OnReleaseImport fire for a replaced file rather than only a new one.
MUST_BE_ON = ("onReleaseImport", "onUpgrade", "onRename")
# Notify()-only triggers. Inert while `notify` is false, and misleading when on.
MUST_BE_OFF = ("onGrab", "onArtistAdd", "onArtistDelete", "onAlbumDelete", "onTrackRetag")


def _get_json(url: str, headers: dict[str, str] | None = None) -> dict | list | None:
  """Decoded JSON, or None when the host is unreachable or answers garbage."""
  req = urllib.request.Request(url, headers=headers or {})
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def subsonic_connector(host: str, api_key: str) -> dict | None:
  """Lidarr's one Subsonic notification, or None if absent/unreachable."""
  found = _get_json(f"{host.rstrip('/')}/api/v1/notification", {"X-Api-Key": api_key})
  if not isinstance(found, list):
    return None
  return next((n for n in found if n.get("implementation") == IMPLEMENTATION), None)


def field_values(definition: dict) -> dict[str, object]:
  """The connector's fields flattened to {name: value}."""
  return {f.get("name"): f.get("value") for f in definition.get("fields", [])}


def field_drift(definition: dict) -> list[str]:
  """Fields whose value is not what ADR-0049 requires."""
  values = field_values(definition)
  return [
    f"{name}={values.get(name)!r} (want {want!r})"
    for name, want in WANT_FIELDS.items()
    if values.get(name) != want
  ]


def trigger_drift(definition: dict) -> list[str]:
  """Triggers that are off and must be on, or on and known to be inert."""
  off = [f"{t} is off" for t in MUST_BE_ON if definition.get(t) is not True]
  on = [f"{t} is on" for t in MUST_BE_OFF if definition.get(t) is True]
  return off + on


def navidrome_user(host: str, user: str, password: str) -> dict | None:
  """Navidrome's own view of the principal, or None if it cannot authenticate."""
  query = urllib.parse.urlencode(
    {"u": user, "p": password, "v": "1.16.1", "c": "nas-verify", "f": "json", "username": user}
  )
  body = _get_json(f"{host.rstrip('/')}/rest/getUser.view?{query}")
  if not isinstance(body, dict):
    return None
  response = body.get("subsonic-response", {})
  if response.get("status") != "ok":
    return None
  return response.get("user")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--host", default=os.getenv("LIDARR_HOST", DEFAULT_LIDARR_HOST))
  parser.add_argument(
    "--navidrome-host", default=os.getenv("NAVIDROME_HOST", DEFAULT_NAVIDROME_HOST)
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)

  api_key = os.getenv("API_KEY_LIDARR")
  if not api_key:
    print("    !!! API_KEY_LIDARR is not set", file=sys.stderr)
    return 2
  nd_user = os.getenv("NAVIDROME_LIDARR_USER")
  nd_password = os.getenv("NAVIDROME_LIDARR_PASSWORD")
  if not nd_user or not nd_password:
    print(
      "    !!! NAVIDROME_LIDARR_USER / NAVIDROME_LIDARR_PASSWORD are not set.\n"
      "        See the Navidrome block in .env.example. ADR-0049",
      file=sys.stderr,
    )
    return 2

  definition = subsonic_connector(args.host, api_key)
  if definition is None:
    print(
      f"    !!! no Subsonic connector on Lidarr at {args.host} (or Lidarr is down).\n"
      "        Without it a new album waits up to an hour for Navidrome's\n"
      "        scheduled scan. ADR-0049",
      file=sys.stderr,
    )
    return 2

  rc = 0

  drifted = field_drift(definition)
  if drifted:
    print(
      f"    !!! Subsonic connector {definition.get('id')} drifted: {', '.join(drifted)}.\n"
      "        `updateLibrary` is the only field that makes this connector DO\n"
      "        anything -- with it off, Lidarr saves fine and Test stays green\n"
      "        while no scan is ever requested. ADR-0049",
      file=sys.stderr,
    )
    rc = 1

  triggers = trigger_drift(definition)
  if triggers:
    print(
      f"    !!! Subsonic connector triggers drifted: {', '.join(triggers)}.\n"
      "        Only OnReleaseImport and OnRename call Update() in Lidarr's\n"
      "        Subsonic.cs; the rest are Notify()-only and inert here, so a\n"
      "        ticked one reads like a working feature and is not. ADR-0049",
      file=sys.stderr,
    )
    rc = 1

  user = navidrome_user(args.navidrome_host, nd_user, nd_password)
  if user is None:
    print(
      f"    !!! Navidrome rejected {nd_user!r} from .env (or is unreachable at\n"
      f"        {args.navidrome_host}). Lidarr sends the same credentials, so\n"
      "        every import now fails to trigger a scan -- silently, because\n"
      "        Lidarr does not surface a failed library update. ADR-0049",
      file=sys.stderr,
    )
    return 2
  if user.get("adminRole") is not True:
    print(
      f"    !!! Navidrome user {nd_user!r} has lost adminRole. Navidrome gates\n"
      "        /rest/startScan on is_admin with no scan-only role, so the call\n"
      "        now returns Subsonic error 50 while ping and getScanStatus keep\n"
      "        answering 200. Restore with:\n"
      f"            docker exec navidrome navidrome user edit -u {nd_user} --set-admin\n"
      "        ADR-0049",
      file=sys.stderr,
    )
    rc = 1

  if rc == 0:
    print(
      f"    ok: Lidarr connector {definition.get('id')} -> navidrome:4533 "
      f"(updateLibrary on, fires on import/upgrade/rename); {nd_user!r} can still scan"
    )
  return rc


if __name__ == "__main__":
  sys.exit(main())
