#!/usr/bin/env python3
"""Assert every album on disk is in Jellyfin, and every Jellyfin album is on disk.

This is the only check that can catch "an album exists on disk and nowhere
else". Both directions matter and neither substitutes for the other: disk ->
Jellyfin catches files with no item (how the missing Kraftwerk albums would have
been found), Jellyfin -> disk catches ghosts pointing at dead paths.

Why it is a script and not a runbook snippet
--------------------------------------------
The 2026-09-04 audit reported `15,268 disk albums vs 15,273 Jellyfin -> 0
missing, 0 ghosts`. That cannot be true. If both set differences are empty the
sets are equal, and equal sets have equal size. Re-run from a single pair of
normalised sets the same day, the real answer was 15,273 == 15,273 with both
differences empty -- the conclusion was right and one of the printed numbers was
not.

The cause was almost certainly **time skew**: the two halves were sampled
minutes apart with imports landing in between, so the disk count and the
Jellyfin count described different moments. That is invisible in a snippet you
paste in two steps, which is exactly why this now asserts the identity

    len(disk) == len(disk & jf) + len(disk - jf)
    len(jf)   == len(disk & jf) + len(jf - disk)

and refuses to print a self-contradicting result. A sweep that cannot notice its
own inconsistency cannot be trusted to notice the library's.

Two traps that manufacture ~1,500 false positives
-------------------------------------------------
1. **Multi-disc albums.** Jellyfin registers the `MusicAlbum` at the *album*
   folder; a disk walk yields `.../Disc 01`, `.../Disc 02`. Without `norm()`
   every multi-disc album shows up as both a "missing" and a "ghost". On this
   library that is the difference between 16,208 raw dirs and 15,273 albums.
2. **`.aif` / `.aiff` are in this library.** Five albums are aiff-only and
   appear as fake ghosts if the extension list omits them.

Paging
------
Jellyfin's `/Items` is paged with `StartIndex`, and the documented `Limit=20000`
was a silent cliff: at 20,001 albums it would have truncated and reported the
overflow as missing. This pages until `TotalRecordCount` is reached.

Exit codes
----------
  0  the two sides agree exactly
  2  a difference in either direction, the arithmetic did not close, or
     Jellyfin was unreachable. There is no exit 1: a discrepancy here is
     always worth a human, and `cron_job.py --ok-codes` defaults to `0,1`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

DEFAULT_JELLYFIN_HOST = "http://localhost:8096"
DEFAULT_DISK_ROOT = "/mnt/drive/music"
DEFAULT_JF_ROOT = "/data/movies/music"
PAGE_SIZE = 1000
AUDIO_EXTENSIONS = (
  "mp3", "flac", "m4a", "ogg", "opus", "wav", "wma", "aac", "aif", "aiff",
)
# Jellyfin registers the album at the album folder, not the per-disc subfolder.
DISC_SUFFIX = re.compile(r"/(?:Disc|CD|Disk)[ _]*\d+\s*$", re.IGNORECASE)


def norm(path: str) -> str:
  """Collapse a per-disc subfolder onto its album folder."""
  return DISC_SUFFIX.sub("", path)


def jellyfin_albums(host: str, api_key: str) -> tuple[set[str], int, int] | None:
  """(paths, total_items, items_without_a_path), or None if unreachable.

  Paged. A bare Limit is a silent cliff -- it truncates and every truncated
  album then reports as missing from Jellyfin.
  """
  paths: set[str] = set()
  seen = 0
  pathless = 0
  start = 0
  total = None
  while True:
    url = (
      f"{host.rstrip('/')}/Items?Recursive=true&IncludeItemTypes=MusicAlbum"
      f"&Fields=Path&Limit={PAGE_SIZE}&StartIndex={start}"
    )
    req = urllib.request.Request(url, headers={"X-Emby-Token": api_key})
    try:
      with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 - localhost
        body = json.loads(resp.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
      return None
    items = body.get("Items") or []
    total = body.get("TotalRecordCount", 0) if total is None else total
    for item in items:
      seen += 1
      path = item.get("Path")
      if path:
        paths.add(path)
      else:
        pathless += 1
    if not items or seen >= total:
      break
    start += len(items)
  return paths, seen, pathless


def disk_albums(root: str, jf_root: str) -> tuple[set[str], int]:
  """(normalised album folders in Jellyfin's namespace, raw dir count).

  One `find` traversal. A per-directory loop takes >2 min on this library and
  times out.
  """
  expr: list[str] = []
  for ext in AUDIO_EXTENSIONS:
    if expr:
      expr.append("-o")
    expr += ["-iname", f"*.{ext}"]
  out = subprocess.run(  # noqa: S603
    ["find", root, "-type", "f", "(", *expr, ")", "-printf", "%h\n"],
    capture_output=True,
    text=True,
    check=False,
  )
  raw = {line for line in out.stdout.splitlines() if line}
  translated = {jf_root + p[len(root) :] for p in raw if p.startswith(root)}
  return {norm(p) for p in translated}, len(raw)


def arithmetic_holds(disk: set[str], jf: set[str]) -> bool:
  """The identity a self-contradicting report violates."""
  both = disk & jf
  return len(disk) == len(both) + len(disk - jf) and len(jf) == len(both) + len(jf - disk)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--host", default=os.getenv("JELLYFIN_HOST", DEFAULT_JELLYFIN_HOST))
  parser.add_argument("--disk-root", default=DEFAULT_DISK_ROOT)
  parser.add_argument("--jellyfin-root", default=DEFAULT_JF_ROOT)
  parser.add_argument("--show", type=int, default=25, help="How many differing paths to print.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  api_key = os.getenv("API_KEY_JELLYFIN")
  if not api_key:
    print("ERROR: API_KEY_JELLYFIN is not set", file=sys.stderr)
    return 2

  fetched = jellyfin_albums(args.host, api_key)
  if fetched is None:
    print(f"ERROR: Jellyfin unreachable at {args.host}", file=sys.stderr)
    return 2
  jf, jf_seen, pathless = fetched
  disk, raw_count = disk_albums(args.disk_root, args.jellyfin_root)

  missing = disk - jf
  ghosts = jf - disk
  both = disk & jf

  print(f"  raw disk dirs (pre-norm)   : {raw_count}")
  print(f"  disk (post-norm, distinct) : {len(disk)}")
  print(f"  jellyfin albums            : {len(jf)}   (items seen {jf_seen}, pathless {pathless})")
  print(f"  in both                    : {len(both)}")
  print(f"  ON DISK, NOT IN JELLYFIN   : {len(missing)}")
  print(f"  IN JELLYFIN, NOT ON DISK   : {len(ghosts)}")

  if not arithmetic_holds(disk, jf):
    print(
      "ERROR: the counts contradict themselves -- equal-looking sets with unequal\n"
      "       sizes. The usual cause is TIME SKEW: the two sides were sampled far\n"
      "       enough apart that imports landed in between. Re-run; if it persists,\n"
      "       the normalisation is wrong.",
      file=sys.stderr,
    )
    return 2

  for label, paths in (("MISSING", missing), ("GHOST", ghosts)):
    for path in sorted(paths)[: args.show]:
      print(f"    {label}: {path}", file=sys.stderr)
    if len(paths) > args.show:
      print(f"    ... and {len(paths) - args.show} more", file=sys.stderr)

  if missing or ghosts:
    print(
      f"ERROR: {len(missing)} album(s) on disk are absent from Jellyfin and "
      f"{len(ghosts)} Jellyfin album(s) point at paths that no longer exist.",
      file=sys.stderr,
    )
    return 2

  print(f"  ok: {len(both)} albums agree in both directions")
  return 0


if __name__ == "__main__":
  sys.exit(main())
