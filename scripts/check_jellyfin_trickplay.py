#!/usr/bin/env python3
"""Assert Jellyfin's trickplay config can actually produce output.

Why this exists (2026-09-13). Trickplay had been enabled on the Movies and TV Shows
libraries for days and had produced **nothing**: 4582 consecutive
``IOException: Read-only file system`` failures, ~30 GPU-hours burned. Tiles are
stored next to the media (owner's choice: they belong on the 3.8 TB drive, not the
76%-full root, and they survive a Jellyfin config wipe), but
``${SHARE_DIRECTORY}:/data/movies`` was mounted ``:ro``, so every generation ran the
full ffmpeg extraction -- ~2 minutes per episode -- and then failed on the very last
step, writing the tiles.

Nothing alerted. The scheduled task reported no failure, every healthcheck stayed
green, and the only trace was an ``[ERR]`` line per item in a log nobody tails. The
symptom a user sees is a scrub bar that silently falls back to chapter images, and
then to nothing once chapter extraction is off too.

The two halves of this must agree and neither is visible to the other. The mount is
in the compose model (``make check`` guards it, ADR-0016); ``SaveTrickplayWithMedia``
is per-library in ``.docker-config/jellyfin/data/root/default/<lib>/options.xml``,
which is gitignored, and the rest is in ``system.xml``. A config restore, or an
untick in the web UI, silently breaks the pairing from the side git cannot see --
see ``.claude/skills/nas-runtime-vs-repo``.

Asserts, against the LIVE server:

1. Every trickplay-enabled library has ``SaveTrickplayWithMedia`` ON **and** a media
   path that is genuinely writable inside the container. Either half alone is the
   silent-failure state above, and it can never self-correct.
2. Movies and TV Shows still have trickplay extraction enabled at all.
3. ``EnableKeyFrameOnlyExtraction`` is on. Measured on this host: 112 s -> 14-18 s
   per 45-min episode (~7x), same 320x180 tiles, same thumbnail count. Losing it
   turns a ~7 h backfill into a ~30 h one that never finishes between nightly runs.
4. Trickplay output actually EXISTS (TrickplayInfos rows). Settings can be perfect
   and generation still broken; a 2xx from the config API is not evidence.

Exit: 0 ok / 1 drift / 2 unreachable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
DB_PATH = os.environ.get(
  "JELLYFIN_DB",
  ".docker-config/jellyfin/data/data/jellyfin.db",
)
# Libraries that must keep producing trickplay. Music/Collections legitimately do not.
REQUIRE_TRICKPLAY = ("Movies", "TV Shows")

OK, DRIFT, UNREACHABLE = 0, 1, 2


def _api(path: str, token: str) -> object:
  req = urllib.request.Request(
    f"{JELLYFIN_URL}{path}",
    headers={"Authorization": f'MediaBrowser Token="{token}"'},
  )
  with urllib.request.urlopen(req, timeout=15) as resp:
    return json.load(resp)


def container_path_is_writable(path: str) -> bool | None:
  """Can jellyfin actually create a directory under `path`? None if undeterminable.

  Probes by writing, not by reading the mount table or the compose model. Trickplay
  fails on the *write*, and a mount can be rw while the uid still cannot create a
  directory -- which looks identical from every other vantage point.
  """
  probe = f"{path.rstrip('/')}/.trickplay-writecheck"
  try:
    res = subprocess.run(
      [
        "docker", "exec", "jellyfin", "sh", "-c",
        f'mkdir -p "{probe}" && rmdir "{probe}"',
      ],
      capture_output=True,
      text=True,
      timeout=20,
      check=False,  # a non-zero exit IS the answer here, not an error
    )
  except (subprocess.TimeoutExpired, FileNotFoundError):
    return None
  return res.returncode == 0


def check_libraries(token: str, problems: list[str]) -> None:
  folders = _api("/Library/VirtualFolders", token)
  seen = {}
  for folder in folders:
    name = folder.get("Name", "?")
    opts = folder.get("LibraryOptions") or {}
    seen[name] = opts
    if not opts.get("EnableTrickplayImageExtraction"):
      continue

    if not opts.get("SaveTrickplayWithMedia"):
      problems.append(
        f"library {name!r}: SaveTrickplayWithMedia is OFF -- ~6 GB of tiles would "
        f"be redirected from the 3.8 TB media drive onto the 76%-full root (ADR-0039)"
      )
      continue

    for info in opts.get("PathInfos") or []:
      path = info.get("Path", "")
      writable = container_path_is_writable(path)
      if writable is None:
        problems.append(f"library {name!r}: could not probe {path} for writability")
      elif not writable:
        problems.append(
          f"library {name!r}: SaveTrickplayWithMedia=true but jellyfin cannot "
          f"write to {path} -- every generation runs the full ffmpeg extraction "
          f"(~2 min/episode) and then fails on the write, forever, without "
          f"alerting. Check for :ro on the media mount (ADR-0016, ADR-0039)"
        )

  for name in REQUIRE_TRICKPLAY:
    opts = seen.get(name)
    if opts is None:
      problems.append(f"library {name!r} not found on the server")
    elif not opts.get("EnableTrickplayImageExtraction"):
      problems.append(f"library {name!r}: trickplay extraction is OFF")


def check_options(token: str, problems: list[str]) -> None:
  cfg = _api("/System/Configuration", token)
  tp = cfg.get("TrickplayOptions") or {}
  if not tp.get("EnableKeyFrameOnlyExtraction"):
    problems.append(
      "TrickplayOptions.EnableKeyFrameOnlyExtraction is OFF -- measured 7x slower "
      "here (112 s vs 14-18 s per 45-min episode) for identical output"
    )


def check_output_exists(problems: list[str]) -> None:
  """Settings can be right and generation still broken. Look for actual rows."""
  if not os.path.exists(DB_PATH):
    problems.append(f"jellyfin.db not found at {DB_PATH}; cannot confirm any output")
    return
  try:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    items, thumbs = conn.execute(
      "SELECT COUNT(*), COALESCE(SUM(ThumbnailCount), 0) FROM TrickplayInfos"
    ).fetchone()
    conn.close()
  except sqlite3.Error as exc:
    problems.append(f"could not read TrickplayInfos: {exc}")
    return

  if items == 0:
    problems.append(
      "TrickplayInfos is EMPTY -- the settings look right but nothing has ever "
      "been produced. Check the jellyfin log for 'Error creating trickplay images'"
    )
  else:
    print(f"    ok: {items} items have trickplay ({thumbs} thumbnails)")


def main() -> int:
  token = os.environ.get("API_KEY_JELLYFIN")
  if not token:
    print("!!! API_KEY_JELLYFIN is not set", file=sys.stderr)
    return UNREACHABLE

  problems: list[str] = []
  try:
    check_libraries(token, problems)
    check_options(token, problems)
  except (urllib.error.URLError, TimeoutError, OSError) as exc:
    print(f"!!! jellyfin unreachable at {JELLYFIN_URL}: {exc}", file=sys.stderr)
    return UNREACHABLE

  check_output_exists(problems)

  if problems:
    for p in problems:
      print(f"    !!! {p}")
    return DRIFT

  print("    ok: tiles saved with media, media mount writable, keyframe-only on")
  return OK


if __name__ == "__main__":
  sys.exit(main())
