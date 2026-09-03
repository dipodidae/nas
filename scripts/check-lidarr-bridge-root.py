#!/usr/bin/env python3
"""Assert Lidarr's root folder is one the Jellyfin bridge knows how to translate.

Used by `make verify-runtime`. This is a RUNTIME fact and cannot live in
scripts/check-invariants.sh: Lidarr's root folder is a row in its SQLite DB, not
anything the compose model can see, so the config can be perfectly correct while
the two have silently drifted apart.

Why it exists
-------------
On 2026-09-02 the ADR-0003 repath moved Lidarr's root `/music` -> `/data/music`.
`lidarr_jellyfin_bridge.py` still translated `/music` alone, so every import for
the next day was dropped by its `translate()` and never reached Jellyfin --
seven Kraftwerk albums sat on disk, correct in Lidarr, invisible in Jellyfin.
Nothing failed: the bridge printed a WARNING, exited 0, and advanced its cursor
past them. The bridge now exits 2 on an unmapped folder, but that only fires
*after* an import has already been missed. This check fires before one is.

Exit codes
----------
  0  every Lidarr root folder is covered by the bridge's map-from roots
  1  a root folder is not covered -- imports under it will never reach Jellyfin
  2  Lidarr is unreachable, or API_KEY_LIDARR is unset
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lidarr_jellyfin_bridge import DEFAULT_MAP_FROM, _matching_root, _roots  # noqa: E402

DEFAULT_LIDARR_HOST = "http://localhost:8686"


def root_folders(host: str, api_key: str) -> list[str] | None:
  """Lidarr's configured root folder paths, or None if Lidarr is unreachable."""
  req = urllib.request.Request(
    f"{host.rstrip('/')}/api/v1/rootfolder", headers={"X-Api-Key": api_key}
  )
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - localhost
      body = json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None
  return [str(r["path"]) for r in body if isinstance(r, dict) and r.get("path")]


def uncovered(paths: list[str], map_from) -> list[str]:
  """Root folders the bridge would not translate."""
  roots = _roots(map_from)
  return [p for p in paths if _matching_root(p.rstrip("/"), roots) is None]


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--host", default=os.getenv("LIDARR_HOST", DEFAULT_LIDARR_HOST))
  args = parser.parse_args(argv)

  api_key = os.getenv("API_KEY_LIDARR")
  if not api_key:
    print("    !!! API_KEY_LIDARR is unset", file=sys.stderr)
    return 2

  paths = root_folders(args.host, api_key)
  if paths is None:
    print(f"    !!! Lidarr unreachable at {args.host}", file=sys.stderr)
    return 2
  if not paths:
    print("    !!! Lidarr has no root folder configured", file=sys.stderr)
    return 1

  missing = uncovered(paths, DEFAULT_MAP_FROM)
  if missing:
    print(
      f"    !!! Lidarr root {missing} is under none of {list(DEFAULT_MAP_FROM)} --\n"
      "        lidarr_jellyfin_bridge.py will drop every import under it and\n"
      "        Jellyfin will never see the album. Add it to DEFAULT_MAP_FROM.\n"
      "        ADR-0003.",
      file=sys.stderr,
    )
    return 1

  print(f"    ok: {', '.join(paths)} covered by {', '.join(DEFAULT_MAP_FROM)}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
