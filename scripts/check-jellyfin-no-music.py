#!/usr/bin/env python3
"""Assert Jellyfin still holds no music, and that its DB has not refilled (ADR-0051).

Used by `make verify-runtime`.

Jellyfin serves movies and series here, and one day books. Navidrome owns music.
Two services indexing the same 237,560-file tree is what made Jellyfin's database
168,128 audio items and 1.43 GB, of which **94% was free pages** by the time the
music was removed -- SQLite does not shrink on delete, so the cost outlives the
data unless someone VACUUMs.

This is an INVERSE assertion: it fails when something comes BACK. Adding a music
library is two clicks in Jellyfin's UI and nothing else in this repo would notice
-- the healthcheck stays green, `make check` cannot see runtime library config,
and the only symptom is the database quietly growing again.

Three things checked:

1. **No library with `CollectionType == "music"`.** The direct guard.

2. **No `Audio` / `MusicAlbum` / `MusicArtist` items.** Not redundant: removing a
   library leaves its items ORPHANED rather than deleting them. That is exactly
   what happened here -- the library was gone from `/Library/VirtualFolders`
   while 168,128 audio items still answered `/Items`, and a normal library scan
   never reaps them because they no longer sit under any root to be validated
   against. So the library being absent does NOT imply the items are.

3. **The database is not mostly free pages.** A high free ratio means a large
   delete happened and was never reclaimed. It is a performance assertion as
   much as a size one: every index scan walks a file that is mostly holes.

Exit codes
----------
  0  no music library, no music items, database compact
  1  music is back, or the database wants a VACUUM
  2  Jellyfin unreachable, or API_KEY_JELLYFIN unset
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_JELLYFIN_HOST = "http://localhost:8096"
MUSIC_ITEM_TYPES = ("Audio", "MusicAlbum", "MusicArtist")
MUSIC_COLLECTION_TYPE = "music"
# Below this the free ratio does not matter -- a small file is a small file.
VACUUM_FLOOR_BYTES = 200 * 1024 * 1024
# Above this fraction of free pages, a VACUUM is owed. Measured baseline after
# the 2026-09-16 prune: 1.43 GB at 94% free -> 69 MB at ~0%.
VACUUM_FREE_RATIO = 0.50


def _get_json(host: str, path: str, token: str) -> dict | list | None:
  """Decoded JSON from Jellyfin, or None when it cannot be reached."""
  req = urllib.request.Request(
    f"{host.rstrip('/')}{path}",
    headers={"Authorization": f'MediaBrowser Token="{token}"'},
  )
  try:
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def music_libraries(host: str, token: str) -> list[str] | None:
  """Names of libraries whose CollectionType is music. None if unreachable."""
  folders = _get_json(host, "/Library/VirtualFolders", token)
  if not isinstance(folders, list):
    return None
  return [
    f.get("Name") or "<unnamed>"
    for f in folders
    if f.get("CollectionType") == MUSIC_COLLECTION_TYPE
  ]


def item_count(host: str, token: str, item_type: str) -> int | None:
  """How many items of one type Jellyfin holds, or None if unreachable."""
  query = urllib.parse.urlencode(
    {
      "Recursive": "true",
      "IncludeItemTypes": item_type,
      "Limit": 0,
      "EnableTotalRecordCount": "true",
    }
  )
  body = _get_json(host, f"/Items?{query}", token)
  if not isinstance(body, dict):
    return None
  return int(body.get("TotalRecordCount", 0))


def database_bloat(db_path: Path) -> tuple[int, float] | None:
  """(size_bytes, free_page_ratio) for jellyfin.db, or None if unreadable.

  Opened read-only so a running Jellyfin is unaffected.
  """
  if not db_path.is_file():
    return None
  try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
      pages = con.execute("pragma page_count").fetchone()[0]
      free = con.execute("pragma freelist_count").fetchone()[0]
    finally:
      con.close()
  except sqlite3.Error:
    return None
  if not pages:
    return None
  return db_path.stat().st_size, free / pages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--host", default=os.getenv("JELLYFIN_HOST", DEFAULT_JELLYFIN_HOST))
  parser.add_argument("--config-dir", default=os.getenv("CONFIG_DIRECTORY", ""))
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  token = os.getenv("API_KEY_JELLYFIN")
  if not token:
    print("    !!! API_KEY_JELLYFIN is not set", file=sys.stderr)
    return 2

  libraries = music_libraries(args.host, token)
  if libraries is None:
    print(f"    !!! Jellyfin unreachable at {args.host}", file=sys.stderr)
    return 2

  rc = 0

  if libraries:
    print(
      f"    !!! Jellyfin has a music library again: {libraries}.\n"
      "        Navidrome owns music (ADR-0044/0049/0050). Two indexers over the\n"
      "        same tree is what grew this database to 168,128 audio items.\n"
      "        Remove the library, then delete the ORPHANED items it leaves\n"
      "        behind -- removing the library does not remove them. ADR-0051",
      file=sys.stderr,
    )
    rc = 1

  counts: dict[str, int] = {}
  for item_type in MUSIC_ITEM_TYPES:
    found = item_count(args.host, token, item_type)
    if found is None:
      print(f"    !!! could not count {item_type} on {args.host}", file=sys.stderr)
      return 2
    if found:
      counts[item_type] = found
  if counts:
    print(
      f"    !!! music items still in Jellyfin: {counts}.\n"
      "        Note these survive their library being deleted, and a library\n"
      "        scan does not reap them -- they sit under no root to validate\n"
      "        against. Delete them by id, then VACUUM. ADR-0051",
      file=sys.stderr,
    )
    rc = 1

  if args.config_dir:
    bloat = database_bloat(Path(args.config_dir) / "jellyfin" / "data" / "data" / "jellyfin.db")
    if bloat is not None:
      size, ratio = bloat
      if size > VACUUM_FLOOR_BYTES and ratio > VACUUM_FREE_RATIO:
        print(
          f"    !!! jellyfin.db is {size / 1e9:.2f} GB and {ratio:.0%} free pages.\n"
          "        A large delete was never reclaimed: SQLite keeps the file at\n"
          "        its high-water mark, so every scan walks mostly holes. Stop\n"
          "        jellyfin and VACUUM it. ADR-0051",
          file=sys.stderr,
        )
        rc = 1
      elif rc == 0:
        print(
          f"    ok: no music library, no music items; "
          f"jellyfin.db {size / 1e6:.0f} MB, {ratio:.0%} free"
        )
        return 0

  if rc == 0:
    print("    ok: no music library and no music items in Jellyfin")
  return rc


if __name__ == "__main__":
  sys.exit(main())
