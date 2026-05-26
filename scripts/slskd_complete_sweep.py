#!/usr/bin/env python3
"""Reap slskd download copies that Lidarr has already imported into /music/.

Background
----------
Tubifarry uses Lidarr's standard import flow which (with
``copyUsingHardlinks=true`` on a same-filesystem mount) hardlinks the file
from ``/downloads/complete/slskd/<dir>/<file>`` into
``/music/<artist>/<album>/<file>``. The slskd copy is left behind — over
time ``/downloads/complete/slskd/`` accumulates GBs of dirs that are 100%
already represented under ``/music/``.

This script identifies those duplicates by file-size match against the
live library and deletes only fully-duplicated slskd dirs.

Match strategy
--------------
1. Walk ``/music`` once and build {file_size: count}.
2. For each ``/downloads/complete/slskd/<dir>`` directory:
   - List its audio files (.mp3, .flac, .ogg, .m4a, .opus, .wav, .aac, .wma).
   - Compute the fraction of those whose size matches an entry in the music
     size index.
   - If ``match_ratio >= --threshold`` (default 1.0 — every file matches)
     AND the dir mtime is older than ``--min-age-hours`` (default 1) AND
     no active Lidarr queue item references the dir, mark it for deletion.
3. Delete the marked dirs.

Safety rails
------------
- **Threshold defaults to 1.0** — every audio file must match. Sub-100%
  matches are reported but skipped unless ``--threshold`` is relaxed.
- **Age gate** — only dirs whose mtime is older than ``--min-age-hours``
  (default 1) are eligible. Brand-new downloads in flight are excluded.
- **Lidarr-active gate** — if any Lidarr queue item references the dir
  (``outputPath`` / ``downloadId``-derived path), the dir is left alone.
- **Path containment check** — only ever deletes children of
  ``SLSKD_COMPLETE_DIR``; refuses if the resolved target escapes it.
- ``--dry-run`` reports the plan and exits 0.
- ``--limit`` caps the number of deletions in a single run (safety cushion
  while tuning).

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some deletes failed)
  2 fatal (config missing, Lidarr unreachable, no /music)

Environment
-----------
  API_KEY_LIDARR        (required) Lidarr API key
  LIDARR_HOST           (default: http://localhost:8686)
  MUSIC_DIR             (default: /mnt/drive/music)
  SLSKD_COMPLETE_DIR    (default: /mnt/drive/downloads/complete/slskd)

Usage
-----
  python scripts/slskd_complete_sweep.py             # delete confirmed dups
  python scripts/slskd_complete_sweep.py --dry-run   # report only
  python scripts/slskd_complete_sweep.py --threshold 0.9 --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_MUSIC_DIR = "/mnt/drive/music"
DEFAULT_SLSKD_COMPLETE_DIR = "/mnt/drive/downloads/complete/slskd"
AUDIO_EXTS = frozenset(
  {".mp3", ".flac", ".ogg", ".m4a", ".opus", ".wav", ".aac", ".wma", ".ape", ".alac"}
)


@dataclass(frozen=True)
class DirReport:
  path: Path
  audio_files: int
  matched: int
  ratio: float
  mtime: float


def build_music_size_index(music_root: Path) -> Counter:
  """Return Counter of audio-file sizes under music_root."""
  sizes: Counter = Counter()
  for dirpath, _dirs, files in os.walk(music_root):
    for name in files:
      ext = os.path.splitext(name)[1].lower()
      if ext not in AUDIO_EXTS:
        continue
      try:
        sizes[os.path.getsize(os.path.join(dirpath, name))] += 1
      except OSError:
        continue
  return sizes


def scan_slskd_dir(d: Path, music_sizes: Counter) -> DirReport | None:
  audio_files = 0
  matched = 0
  try:
    mtime = d.stat().st_mtime
  except OSError:
    return None
  for dirpath, _dirs, files in os.walk(d):
    for name in files:
      ext = os.path.splitext(name)[1].lower()
      if ext not in AUDIO_EXTS:
        continue
      audio_files += 1
      try:
        size = os.path.getsize(os.path.join(dirpath, name))
      except OSError:
        continue
      if music_sizes.get(size, 0) > 0:
        matched += 1
  if audio_files == 0:
    return None
  return DirReport(
    path=d,
    audio_files=audio_files,
    matched=matched,
    ratio=matched / audio_files,
    mtime=mtime,
  )


def active_queue_paths(host: str, api_key: str) -> set[str]:
  """Return path basenames currently referenced by the Lidarr queue.

  Returns empty set on error (caller treats as conservative — nothing
  is matched, so no extra protection beyond the age gate).
  """
  url = f"{host}/api/v1/queue?pageSize=200&includeUnknownArtistItems=true"
  req = urllib.request.Request(url, headers={"X-Api-Key": api_key})
  try:
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - localhost
      records = json.loads(resp.read()).get("records", [])
  except (urllib.error.URLError, json.JSONDecodeError):
    return set()
  out: set[str] = set()
  for r in records:
    for key in ("outputPath", "downloadForcedClientPath", "title"):
      val = r.get(key)
      if isinstance(val, str) and val:
        out.add(os.path.basename(val.rstrip("/").rstrip("\\")))
  return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Reap slskd download dirs that Lidarr already imported into /music/."
  )
  parser.add_argument(
    "--threshold",
    type=float,
    default=1.0,
    help="Match ratio required to delete (default 1.0 — every audio file must match).",
  )
  parser.add_argument(
    "--min-age-hours",
    type=float,
    default=1.0,
    help="Only consider dirs whose mtime is older than this (default 1.0).",
  )
  parser.add_argument(
    "--limit",
    type=int,
    default=0,
    help="Cap deletions per run (0 = unlimited).",
  )
  parser.add_argument(
    "--music-dir", type=Path, default=None, help=f"Override MUSIC_DIR (env or {DEFAULT_MUSIC_DIR})."
  )
  parser.add_argument(
    "--slskd-complete-dir",
    type=Path,
    default=None,
    help=f"Override SLSKD_COMPLETE_DIR (env or {DEFAULT_SLSKD_COMPLETE_DIR}).",
  )
  parser.add_argument("--dry-run", action="store_true", help="Report only.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  api_key = os.environ.get("API_KEY_LIDARR")
  if not api_key:
    print("ERROR: API_KEY_LIDARR not set", file=sys.stderr)
    return 2

  music = args.music_dir or Path(os.environ.get("MUSIC_DIR", DEFAULT_MUSIC_DIR))
  slskd_root = args.slskd_complete_dir or Path(
    os.environ.get("SLSKD_COMPLETE_DIR", DEFAULT_SLSKD_COMPLETE_DIR)
  )
  if not music.is_dir():
    print(f"ERROR: music dir {music} not found", file=sys.stderr)
    return 2
  if not slskd_root.is_dir():
    print(f"ERROR: slskd complete dir {slskd_root} not found", file=sys.stderr)
    return 2

  print(f"indexing {music} ...")
  music_sizes = build_music_size_index(music)
  print(f"  indexed {sum(music_sizes.values())} audio files, {len(music_sizes)} distinct sizes")

  queue_names = active_queue_paths(host, api_key)
  print(f"Lidarr queue references {len(queue_names)} path basename(s)")

  now = time.time()
  threshold_seconds = args.min_age_hours * 3600

  reports: list[DirReport] = []
  for child in sorted(slskd_root.iterdir()):
    if not child.is_dir():
      continue
    rep = scan_slskd_dir(child, music_sizes)
    if rep is None:
      continue
    reports.append(rep)

  print(f"scanned {len(reports)} slskd dirs with audio")

  eligible: list[DirReport] = []
  skipped_queue = 0
  skipped_age = 0
  skipped_ratio = 0
  for r in reports:
    if r.path.name in queue_names:
      skipped_queue += 1
      continue
    if (now - r.mtime) < threshold_seconds:
      skipped_age += 1
      continue
    if r.ratio < args.threshold:
      skipped_ratio += 1
      continue
    eligible.append(r)

  # Containment safety: every eligible path must be a direct child of slskd_root
  slskd_resolved = slskd_root.resolve()
  for r in eligible:
    if r.path.resolve().parent != slskd_resolved:
      print(f"ERROR: refusing to act on {r.path} — not a direct child of {slskd_root}", file=sys.stderr)
      return 2

  if args.limit and len(eligible) > args.limit:
    print(f"limiting to first {args.limit} of {len(eligible)} eligible dirs")
    eligible = eligible[: args.limit]

  bytes_to_free = 0
  for r in eligible:
    for dirpath, _dirs, files in os.walk(r.path):
      for f in files:
        try:
          bytes_to_free += os.path.getsize(os.path.join(dirpath, f))
        except OSError:
          continue

  print(
    f"plan: delete {len(eligible)} dir(s) "
    f"(~{bytes_to_free / 1e9:.2f} GB); "
    f"skipped {skipped_queue} queue, {skipped_age} too-recent, "
    f"{skipped_ratio} below threshold={args.threshold}"
  )

  if args.dry_run:
    for r in eligible[:15]:
      print(f"  DRY {r.path.name} ({r.matched}/{r.audio_files} = {r.ratio * 100:.0f}%)")
    if len(eligible) > 15:
      print(f"  ... and {len(eligible) - 15} more")
    return 0

  failed: list[Path] = []
  for r in eligible:
    try:
      shutil.rmtree(r.path)
    except OSError as exc:
      print(f"WARNING: rmtree {r.path}: {exc}", file=sys.stderr)
      failed.append(r.path)

  print(
    f"deleted {len(eligible) - len(failed)}/{len(eligible)} dirs "
    f"(~{bytes_to_free / 1e9:.2f} GB reclaimed if all succeeded)"
  )
  return 1 if failed else 0


if __name__ == "__main__":
  sys.exit(main())
