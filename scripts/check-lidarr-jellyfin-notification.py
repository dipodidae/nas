#!/usr/bin/env python3
"""Tripwire: the day Lidarr gains mapFrom/mapTo, the bridge can be retired.

Used by `make verify-runtime`.

Lidarr's Jellyfin connection (notification id 6, `MediaBrowser`) exposes exactly
these fields, verified 2026-09-04 against Lidarr 3.1.5.5056:

    host, port, useSsl, urlBase, apiKey, notify, updateLibrary

There is no `mapFrom` and no `mapTo`. Sonarr's and Radarr's connections have
them and use them to translate paths in-app; Lidarr's does not, so Lidarr sends
its own root spelling (`/data/music/...`), which exists under no Jellyfin
library, and Jellyfin drops it while still answering `204`. That connection is
enabled, tests green, and has never once worked -- which is why
`scripts/lidarr_jellyfin_bridge.py` exists and does 100% of the work.

Tracked upstream as Lidarr#5646 (open) and Lidarr#3933.

Two things happen when those fields appear:

1. The bridge's translation becomes redundant and can be retired.
2. `onArtistDelete` / `onAlbumDelete`, deliberately `False` today because they
   would only send unmapped paths, become safe to enable.

Without a tripwire that decision outlives its reason by years, and the next
person inherits a workaround with no way to tell whether it is still needed.
So this check FAILS when the fields APPEAR -- the inverse of a normal assertion.

It also guards the other direction: the delete toggles must stay `False` while
the fields are absent, because a config restore can flip them back silently
(they live in Lidarr's SQLite DB, not in this repo).

Exit codes
----------
  0  fields still absent and the delete toggles are still off -- bridge required
  1  mapFrom/mapTo appeared (retire the bridge), or a delete toggle drifted on
  2  Lidarr unreachable, or API_KEY_LIDARR unset
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_LIDARR_HOST = "http://localhost:8686"
NOTIFICATION_ID = 6
MAPPING_FIELDS = ("mapFrom", "mapTo")
# Deliberately off while the mapping fields are absent. docs sec 6.3 and sec 10.
MUST_STAY_OFF = ("onArtistDelete", "onAlbumDelete")


def notification(host: str, api_key: str, notification_id: int) -> dict | None:
  """One notification definition, or None if Lidarr is unreachable."""
  req = urllib.request.Request(
    f"{host.rstrip('/')}/api/v1/notification/{notification_id}",
    headers={"X-Api-Key": api_key},
  )
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def mapping_fields_present(definition: dict) -> list[str]:
  """Which of mapFrom/mapTo Lidarr now exposes. Empty is the expected state."""
  names = {f.get("name") for f in definition.get("fields", [])}
  return [f for f in MAPPING_FIELDS if f in names]


def delete_toggles_on(definition: dict) -> list[str]:
  """Delete toggles that drifted to True while the mapping fields are absent."""
  return [t for t in MUST_STAY_OFF if definition.get(t) is True]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--host", default=os.getenv("LIDARR_HOST", DEFAULT_LIDARR_HOST))
  parser.add_argument("--id", type=int, default=NOTIFICATION_ID)
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  api_key = os.getenv("API_KEY_LIDARR")
  if not api_key:
    print("    !!! API_KEY_LIDARR is not set", file=sys.stderr)
    return 2

  definition = notification(args.host, api_key, args.id)
  if definition is None:
    print(f"    !!! Lidarr unreachable at {args.host}", file=sys.stderr)
    return 2

  rc = 0
  appeared = mapping_fields_present(definition)
  if appeared:
    print(
      f"    !!! Lidarr notification {args.id} now exposes {appeared} --\n"
      "        UPSTREAM HAS FIXED Lidarr#5646. This is good news, not a fault:\n"
      "          * scripts/lidarr_jellyfin_bridge.py can be retired once the\n"
      "            fields are set to /data/music -> /data/movies/music;\n"
      "          * onArtistDelete / onAlbumDelete become safe to enable;\n"
      "          * docs/music-pipeline-integration.md sec 6.2, 6.3 and 10 all\n"
      "            need revisiting.\n"
      "        Update this check once that work is done.",
      file=sys.stderr,
    )
    rc = 1

  drifted = delete_toggles_on(definition)
  if drifted and not appeared:
    print(
      f"    !!! {drifted} is True while the connection still has no mapFrom/mapTo.\n"
      "        Lidarr will send unmapped /data/music/... paths that Jellyfin drops\n"
      "        with a 204. These live in Lidarr's SQLite DB, so a config restore\n"
      "        can turn them back on silently. Set them False. docs sec 6.3.",
      file=sys.stderr,
    )
    rc = 1

  if rc == 0:
    print(
      f"    ok: notification {args.id} still has no mapFrom/mapTo "
      f"({len(definition.get('fields', []))} fields); delete toggles off; bridge required"
    )
  return rc


if __name__ == "__main__":
  sys.exit(main())
