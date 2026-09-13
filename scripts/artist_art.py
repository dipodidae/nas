#!/usr/bin/env python3
"""Download missing artist images (folder.jpg) for the music library, from Deezer.

The gap this closes
-------------------
``album_art.py`` (sacad) fills *album* covers and is very good at it: 96.3% of
16,882 album folders have a ``folder.jpg``. Nothing filled *artist* images, and
it showed -- 1,647 of Jellyfin's 3,331 artists (49%) had no primary image, and
only 756 of 2,741 artist directories held one on disk.

The reason is source coverage, not tooling, and it was measured before this
script was written. On a random sample of the image-less artists:

  Deezer (exact-name artist picture)   25/30   83%
  fanart.tv (the Jellyfin plugin)       3/40    7.5%   (1 of those a thumb)
  Wikidata P18 via a MusicBrainz rel    1/30    3%

This library is deep-catalogue underground metal and experimental, which the
metadata-curation sites have barely touched but which Deezer, being a shop,
carries anyway. So: Deezer, matched by name and **verified against the albums
actually on disk**.

Why the album cross-check is not optional
-----------------------------------------
An artist folder named ``33`` or ``Beware`` matches something on Deezer no
matter what. A wrong artist image is worse than none -- it looks correct, and
nothing downstream ever flags it. So a candidate must share at least one album
title with the folder on disk before its picture is written; ``--no-verify``
turns that off, and the report always says how many were rejected by it.

Writes go to the host filesystem, not through Jellyfin: Jellyfin mounts the
share read-only (ADR-0016) and reads ``folder.jpg`` from an artist directory as
that artist's primary image on the next library scan.

Exit codes
----------
  0  success (or dry-run / nothing to do)
  1  partial (some downloads failed)
  2  fatal (music directory missing, unexpected error)

Environment
-----------
  SHARE_DIRECTORY   Base share path (default: /mnt/drive). The music root
                    resolves to ``$SHARE_DIRECTORY/music`` unless --music-dir.

Usage
-----
  python scripts/artist_art.py                      # dry-run plan (the default)
  python scripts/artist_art.py --apply --limit 300  # the cron mode
  python scripts/artist_art.py --apply --artist 'Xasthur'
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEEZER_SEARCH = "https://api.deezer.com/search/artist"
DEEZER_ALBUMS = "https://api.deezer.com/artist/{id}/albums"
USER_AGENT = "nas-artist-art/1.0 (homelab; contact via repo)"
COVER_NAME = "folder.jpg"
DEFAULT_STATE = "logs/artist_art.json"
AUDIO_SUFFIXES = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wma", ".wav", ".aac", ".alac", ".aiff"}
# Deezer's own "no picture" placeholder; every artist without art returns it.
PLACEHOLDER_MARKERS = ("/images/artist//", "d41d8cd98f00b204e9800998ecf8427e")


@dataclass(frozen=True)
class Artist:
  path: Path
  name: str
  albums: tuple[str, ...]


def normalise(text: str) -> str:
  """Fold a title/name to something two catalogues can agree on. Pure.

  Strips accents, bracketed suffixes ("(Remastered)", "[2004]"), a leading
  "YYYY - " release-year prefix, punctuation and case.
  """
  text = unicodedata.normalize("NFKD", text)
  text = "".join(ch for ch in text if not unicodedata.combining(ch))
  text = re.sub(r"^\s*\d{4}\s*-\s*", "", text)
  text = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", text)
  text = re.sub(r"[^0-9a-zA-Z]+", " ", text)
  return " ".join(text.split()).casefold()


def folder_to_name(folder: str) -> str:
  """Undo the characters an *arr replaces when it creates a directory. Pure.

  Lidarr writes ``AC+DC`` for ``AC/DC`` and strips characters illegal on other
  filesystems, so the directory name is not always the artist's name.
  """
  return folder.replace("+", "/") if "+" in folder and "/" not in folder else folder


def discover_artists(music_root: Path) -> list[Artist]:
  """Every depth-1 directory that holds albums, with its album titles.

  A directory counts as an artist if it contains a subdirectory with audio in
  it; a directory holding audio directly is a single-album artist and counts
  too, with itself as its one album.
  """
  artists: list[Artist] = []
  try:
    entries = sorted(p for p in music_root.iterdir() if p.is_dir())
  except OSError:
    return []
  for path in entries:
    albums: list[str] = []
    has_own_audio = False
    try:
      for child in sorted(path.iterdir()):
        if child.is_dir():
          if any(f.suffix.lower() in AUDIO_SUFFIXES for f in child.iterdir() if f.is_file()):
            albums.append(child.name)
        elif child.suffix.lower() in AUDIO_SUFFIXES:
          has_own_audio = True
    except OSError:
      continue
    if albums or has_own_audio:
      artists.append(Artist(path=path, name=folder_to_name(path.name), albums=tuple(albums)))
  return artists


def needs_cover(artist: Artist, cover_name: str = COVER_NAME) -> bool:
  """True if the artist directory has no cover file (either case). Pure-ish."""
  return not (
    (artist.path / cover_name).exists() or (artist.path / cover_name.upper()).exists()
  )


def select_targets(
  artists: list[Artist],
  attempts: dict[str, float],
  now: float,
  cooldown_days: float,
  limit: int,
) -> list[Artist]:
  """Artists missing a cover and off cooldown, least-recently-tried first. Pure."""
  cooldown_s = cooldown_days * 86400

  def due_at(artist: Artist) -> float:
    """Last attempt, or -inf for one never tried so it can never be benched."""
    return attempts.get(str(artist.path), float("-inf"))

  due = [a for a in artists if needs_cover(a) and (now - due_at(a)) >= cooldown_s]
  due.sort(key=due_at)
  return due[:limit]


def is_placeholder(url: str) -> bool:
  """Deezer serves a generic silhouette for artists with no photo. Pure."""
  return not url or any(marker in url for marker in PLACEHOLDER_MARKERS)


def pick_candidate(
  artist: Artist,
  candidates: list[dict],
  album_titles: dict[int, list[str]],
  *,
  verify: bool,
) -> tuple[dict | None, str]:
  """Choose the Deezer artist to trust, with the reason. Pure.

  Returns (candidate, reason). ``candidate`` is None when nothing is safe to
  use, and the reason names which gate rejected it so the report can count
  them separately -- "no exact name match" and "name matched but no shared
  album" are very different problems.
  """
  wanted = normalise(artist.name)
  exact = [c for c in candidates if normalise(c.get("name", "")) == wanted]
  if not exact:
    return None, "no exact name match"
  usable = [c for c in exact if not is_placeholder(c.get("picture_xl", ""))]
  if not usable:
    return None, "matched, but Deezer has no photo"
  if not verify:
    return usable[0], "name match (verification off)"

  local = {normalise(t) for t in artist.albums}
  if not local:
    return usable[0], "name match (artist has no album folders to verify against)"
  best: tuple[int, dict | None] = (0, None)
  for cand in usable:
    remote = {normalise(t) for t in album_titles.get(cand.get("id", -1), [])}
    overlap = len(local & remote)
    if overlap > best[0]:
      best = (overlap, cand)
  if best[1] is None:
    return None, "name matched but no shared album"
  return best[1], f"name + {best[0]} shared album(s)"


def _get_json(url: str, timeout: float) -> dict:
  req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
  with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
    return json.loads(resp.read().decode("utf-8", "replace"))


def _download(url: str, dest: Path, timeout: float) -> int:
  req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
  with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
    data = resp.read()
  if len(data) < 1024:
    raise OSError(f"image suspiciously small ({len(data)} bytes)")
  tmp = dest.with_suffix(dest.suffix + ".part")
  tmp.write_bytes(data)
  tmp.replace(dest)
  return len(data)


def load_state(path: Path) -> dict[str, float]:
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError):
    return {}
  attempts = data.get("attempts")
  return attempts if isinstance(attempts, dict) else {}


def save_state(path: Path, attempts: dict[str, float]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(path.suffix + ".tmp")
  tmp.write_text(json.dumps({"attempts": attempts, "written": time.time()}), encoding="utf-8")
  tmp.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument(
    "--music-dir",
    default=None,
    help="Music root (default: $SHARE_DIRECTORY/music)",
  )
  ap.add_argument("--apply", action="store_true", help="Actually download (default: dry-run)")
  ap.add_argument("--limit", type=int, default=300, help="Max artists to process per run")
  ap.add_argument("--delay", type=float, default=0.34, help="Seconds between Deezer calls")
  ap.add_argument(
    "--cooldown-days",
    type=float,
    default=45.0,
    help="Do not retry an artist attempted within this many days",
  )
  ap.add_argument(
    "--no-verify",
    dest="verify",
    action="store_false",
    help="Accept an exact name match without a shared album (not recommended)",
  )
  ap.add_argument("--artist", help="Only this artist directory name (ignores cooldown and limit)")
  ap.add_argument("--cover-name", default=COVER_NAME)
  ap.add_argument("--state", default=DEFAULT_STATE)
  ap.add_argument("--timeout", type=float, default=25.0)
  return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  share = os.getenv("SHARE_DIRECTORY", "/mnt/drive")
  music_root = Path(args.music_dir) if args.music_dir else Path(share) / "music"
  if not music_root.is_dir():
    print(f"Music directory not found: {music_root}", file=sys.stderr)
    return 2

  print(f"Scanning {music_root} for artist directories…")
  artists = discover_artists(music_root)
  gaps = [a for a in artists if needs_cover(a, args.cover_name)]
  print(f"Found {len(artists)} artist directories.")
  print(f"  {len(artists) - len(gaps)} already have {args.cover_name}.")
  print(f"  {len(gaps)} missing {args.cover_name}.")

  state_path = Path(args.state)
  attempts = load_state(state_path)
  now = time.time()

  if args.artist:
    targets = [a for a in artists if a.path.name == args.artist]
    if not targets:
      print(f"No artist directory named {args.artist!r}", file=sys.stderr)
      return 2
  else:
    targets = select_targets(artists, attempts, now, args.cooldown_days, args.limit)

  print(
    f"  {len(targets)} due this run (limit {args.limit}, cooldown {args.cooldown_days:g}d, "
    f"album verification {'on' if args.verify else 'OFF'})."
  )
  if not args.apply:
    for artist in targets[:10]:
      print(f"  would look up {artist.name!r} ({len(artist.albums)} albums)")
    if len(targets) > 10:
      print(f"  … and {len(targets) - 10} more")
    print(f"DRY-RUN: nothing downloaded. Pass --apply to fetch {args.cover_name}.")
    return 0

  wrote = 0
  failures = 0
  reasons: dict[str, int] = {}
  for artist in targets:
    attempts[str(artist.path)] = now
    query = urllib.parse.urlencode({"limit": 8, "q": artist.name})
    try:
      found = _get_json(f"{DEEZER_SEARCH}?{query}", args.timeout).get("data", [])
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
      failures += 1
      print(f"  ! search failed for {artist.name!r}: {exc}", file=sys.stderr)
      time.sleep(args.delay)
      continue

    album_titles: dict[int, list[str]] = {}
    if args.verify:
      for cand in found:
        if normalise(cand.get("name", "")) != normalise(artist.name):
          continue
        try:
          data = _get_json(DEEZER_ALBUMS.format(id=cand["id"]), args.timeout)
          album_titles[cand["id"]] = [a.get("title", "") for a in data.get("data", [])]
        except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
          album_titles[cand.get("id", -1)] = []
        time.sleep(args.delay)

    cand, reason = pick_candidate(artist, found, album_titles, verify=args.verify)
    if cand is None:
      reasons[reason] = reasons.get(reason, 0) + 1
      time.sleep(args.delay)
      continue

    url = cand.get("picture_xl") or cand.get("picture_big") or ""
    dest = artist.path / args.cover_name
    try:
      size = _download(url, dest, args.timeout)
      wrote += 1
      print(f"  + {artist.name}  ({reason}, {size / 1024:.0f} KB)")
    except (urllib.error.URLError, OSError) as exc:
      failures += 1
      print(f"  ! download failed for {artist.name!r}: {exc}", file=sys.stderr)
    time.sleep(args.delay)

  save_state(state_path, attempts)
  print(f"Wrote {wrote} artist image(s); {failures} failure(s).")
  for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
    print(f"  skipped {count}: {reason}")
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
