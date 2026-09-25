#!/usr/bin/env python3
"""Assert Navidrome's plugins are loaded AND that Instant Mix is AudioMuse's (ADR-0053).

Used by `make verify-runtime`.

Every piece of this lives outside git: the .ndp files and the `plugin` table
(enabled flag, config, grants) are in ${CONFIG_DIRECTORY}/navidrome, and
AudioMuse's analysis is in its Postgres. A restored navidrome.db brings every
plugin back DISABLED, and Navidrome says nothing about it -- Instant Mix just
falls through to Last.fm, lyrics just stop, and both still "work".

Three things are checked:

1. Every plugin in navidrome/plugins/plugins.lock is installed with the
   pinned sha256, enabled, and carries no `last_error`.

2. AudioMuse answers `/api/similar_tracks` for a track it has analysed.

3. BY EFFECT, the thing that matters: Navidrome's getSimilarSongs2 for that
   same track returns AudioMuse's answer. Checked as overlap between the two
   result sets, because a Last.fm fallback also returns a non-empty list --
   "Instant Mix returned songs" proves nothing about which agent answered.

Exit codes
----------
  0  plugins enabled and Instant Mix is served by AudioMuse
  1  a plugin drifted, or Instant Mix is answered by something else
  2  Navidrome/AudioMuse unreachable, nothing analysed yet, or env unset
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LOCK = REPO / "navidrome" / "plugins" / "plugins.lock"
NAVIDROME = os.getenv("NAVIDROME_HOST", "http://localhost:4533")
AUDIOMUSE = os.getenv("AUDIOMUSE_HOST", "http://localhost:8010")
# Below this share of Navidrome's answer coming from AudioMuse's, something
# else is answering. Measured 2026-09-25: 15/15 identical ids.
MIN_OVERLAP = 0.5


def read_lock(path: Path = LOCK) -> dict[str, str]:
  """{plugin id: pinned sha256}. Pure apart from the read."""
  pins: dict[str, str] = {}
  for line in path.read_text().splitlines():
    parts = line.split()
    if parts and not parts[0].startswith("#"):
      pins[parts[0]] = parts[2]
  return pins


def plugin_drift(pins: dict[str, str], rows: dict[str, dict]) -> list[str]:
  """What is wrong with the installed set. Pure, for testing."""
  bad = []
  for pid, sha in pins.items():
    row = rows.get(pid)
    if row is None:
      bad.append(f"{pid}: not installed")
      continue
    if row["sha256"] != sha:
      bad.append(f"{pid}: sha256 {row['sha256'][:12]} != pinned {sha[:12]}")
    if not row["enabled"]:
      bad.append(f"{pid}: DISABLED")
    if row["last_error"]:
      bad.append(f"{pid}: last_error={row['last_error']!r}")
  return bad


def overlap(ours: list[str], theirs: list[str]) -> float:
  """Share of Navidrome's ids that AudioMuse also returned."""
  return len(set(ours) & set(theirs)) / len(ours) if ours else 0.0


def _get(url: str, headers: dict[str, str] | None = None) -> object | None:
  req = urllib.request.Request(url, headers=headers or {})
  try:
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read())
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def installed_plugins(db: Path) -> dict[str, dict]:
  con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
  try:
    return {
      r[0]: {"sha256": r[1], "enabled": bool(r[2]), "last_error": r[3]}
      for r in con.execute("select id, sha256, enabled, last_error from plugin")
    }
  finally:
    con.close()


def main() -> int:
  token = os.getenv("AUDIOMUSE_API_TOKEN")
  user, password = os.getenv("NAVIDROME_AUDIOMUSE_USER"), os.getenv("NAVIDROME_AUDIOMUSE_PASSWORD")
  config_dir = os.getenv("CONFIG_DIRECTORY")
  if not (token and user and password and config_dir):
    print("    !!! AUDIOMUSE_API_TOKEN / NAVIDROME_AUDIOMUSE_* / CONFIG_DIRECTORY unset", file=sys.stderr)
    return 2

  rc = 0
  try:
    rows = installed_plugins(Path(config_dir) / "navidrome" / "navidrome.db")
  except sqlite3.Error as exc:
    print(f"    !!! cannot read navidrome.db: {exc}", file=sys.stderr)
    return 2
  drift = plugin_drift(read_lock(), rows)
  if drift:
    print(
      "    !!! Navidrome plugins drifted: " + "; ".join(drift) + "\n"
      "        Re-apply with `make navidrome-plugins`. A restored navidrome.db\n"
      "        brings every plugin back disabled, silently. ADR-0053",
      file=sys.stderr,
    )
    rc = 1
  else:
    print(f"    ok: {len(rows)} plugins enabled at their pinned sha256")

  # Seeds from AudioMuse's OWN index (search_tracks only returns analysed
  # tracks): early in the first pass a random library track is almost never
  # analysed, and probing random Navidrome songs found none in 40.
  auth = {"Authorization": f"Bearer {token}"}
  q = urllib.parse.urlencode({"u": user, "p": password, "v": "1.16.1", "c": "nas-verify", "f": "json"})
  seed, theirs = None, []
  for letter in "aeo":
    found = _get(f"{AUDIOMUSE}/api/search_tracks?search_query={letter}&limit=5", auth)
    for cand in [t.get("item_id") for t in found] if isinstance(found, list) else []:
      got = _get(f"{AUDIOMUSE}/api/similar_tracks?item_id={cand}&n=10", auth)
      if isinstance(got, list) and got:
        seed, theirs = cand, [t.get("item_id") for t in got]
        break
    if seed:
      break
  if seed is None:
    print(
      "    !!! AudioMuse gave no seed with similar tracks (unreachable, bad\n"
      "        AUDIOMUSE_API_TOKEN, or nothing analysed yet). ADR-0053",
      file=sys.stderr,
    )
    return 2

  body = _get(f"{NAVIDROME}/rest/getSimilarSongs2.view?{q}&id={seed}&count=10")
  ours = [s["id"] for s in (body or {}).get("subsonic-response", {}).get("similarSongs2", {}).get("song", [])]
  share = overlap(ours, theirs)
  if share < MIN_OVERLAP:
    print(
      f"    !!! Instant Mix for {seed} is NOT AudioMuse's: {len(ours)} songs, "
      f"{share:.0%} shared with AudioMuse's answer.\n"
      "        Something ahead of `audiomuseai` in ND_AGENTS is answering, or the\n"
      "        plugin is disabled/misconfigured -- Last.fm similarity is back. ADR-0053",
      file=sys.stderr,
    )
    rc = 1
  else:
    print(f"    ok: Instant Mix is AudioMuse's ({share:.0%} of {len(ours)} shared)")
  return rc


if __name__ == "__main__":
  sys.exit(main())
