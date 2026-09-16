#!/usr/bin/env python3
"""Export Jellyfin's audio playlists as .m3u8 files Navidrome imports (ADR-0050).

Jellyfin keeps playlists in its own config as `playlist.xml` with absolute paths
in JELLYFIN's namespace (`/data/movies/music/...`, ADR-0016). Nothing else can
read that. This writes each one out as a portable `.m3u8` with paths RELATIVE to
the music root, into `${SHARE_DIRECTORY}/music/Playlists/`, where:

  * Navidrome auto-imports them (`autoimportplaylists`, on by default) and
    serves them over Subsonic -- which is how they reach Symfonium, substreamer,
    DSub and play:Sub. Measured: an imported playlist is owned by the first
    admin (`tom`) and carries `sync=1`, so re-running this updates it in place.
  * any other player reads them directly -- VLC, foobar2000, mpd, Poweramp --
    as long as the `Playlists/` folder stays beside the music tree.

Three things this has to get right, each measured rather than assumed:

1. **The path namespace.** The prefix is not hardcoded: it is read from
   Jellyfin's own music library location (`/Library/VirtualFolders`, the entry
   whose `CollectionType` is `music`). ADR-0003's repath broke two consumers
   that had stored a prefix, so a tool that writes paths gets its prefix from
   the service that owns it. A playlist whose tracks match NO known prefix is
   exit 2, never a warning: a path this cannot translate is a repath nobody
   told it about, and writing the untranslated path would produce a playlist
   that resolves for nobody.

2. **The playlist name is not the filename.** `#PLAYLIST:` wins in Navidrome
   (measured), so names keep their `/`, `✦` and Cyrillic while the filename is
   sanitised down to something a filesystem and a sync client can both hold.

3. **Relative paths, not absolute.** The same tree is `/data/movies/music` to
   Jellyfin, `/music` to Navidrome, `/data/music` to Lidarr and `/mnt/drive/music`
   on the host. Only a relative path is true in all four.

A `Playlists/` directory inside the music tree is invisible to every existing
consumer, which is why it is safe to put it there: `album_art.py` keys off AUDIO
file extensions, and Lidarr reports no unmapped folder for it (both verified
2026-09-16).

This is a ONE-OFF migration tool, kept for the record and for re-running if
Jellyfin ever holds audio playlists again. Jellyfin has had no music library
since 2026-09-16 (ADR-0051), so a run today finds nothing to export.

Usage
-----
    python scripts/export_jellyfin_playlists.py              # dry run, default
    python scripts/export_jellyfin_playlists.py --apply
    python scripts/export_jellyfin_playlists.py --apply --prune
    python scripts/export_jellyfin_playlists.py --apply --rescan   # tell Navidrome

Exit codes
----------
  0  every playlist exported with every track resolved
  1  partial -- some tracks were skipped (missing on disk, or outside the
     music library); the playlists were still written
  2  fatal -- Jellyfin unreachable, no music library, an untranslatable path
     namespace, or the music root does not exist
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_JELLYFIN_HOST = "http://localhost:8096"
DEFAULT_NAVIDROME_HOST = "http://localhost:4533"
# Relative to the music root. Navidrome scans subfolders for playlists, so this
# does not need ND_PLAYLISTSPATH set (measured against 0.64.0).
PLAYLIST_SUBDIR = "Playlists"
# Jellyfin's tick is 100 ns.
TICKS_PER_SECOND = 10_000_000
# Written into every file so --prune can tell our files from a hand-made one.
# Players ignore `#` lines they do not recognise.
MARKER_PREFIX = "#EXT-X-JELLYFIN-PLAYLIST-ID:"
# Characters a filename cannot hold, or that break sync clients and Subsonic
# clients in ways that are tedious to debug. The real name survives in
# `#PLAYLIST:`, so this is allowed to be lossy.
_UNSAFE = set('/\\:*?"<>|')


@dataclass(frozen=True)
class Track:
  """One resolved playlist entry, in the music root's own namespace."""

  relative_path: str
  title: str
  artist: str
  seconds: int


@dataclass
class Playlist:
  """A Jellyfin playlist flattened to what an .m3u8 needs."""

  jellyfin_id: str
  name: str
  tracks: list[Track] = field(default_factory=list)
  skipped: list[str] = field(default_factory=list)


@dataclass
class Outcome:
  """What a run did, so main() can print and exit without re-deriving it."""

  written: list[str] = field(default_factory=list)
  unchanged: list[str] = field(default_factory=list)
  pruned: list[str] = field(default_factory=list)
  skipped_tracks: int = 0
  total_tracks: int = 0


def _get_json(url: str, token: str) -> dict | list | None:
  """Decoded JSON from Jellyfin, or None when it is unreachable."""
  req = urllib.request.Request(
    url, headers={"Authorization": f'MediaBrowser Token="{token}"'}
  )
  try:
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def music_library_roots(host: str, token: str) -> list[str] | None:
  """Jellyfin's OWN music library paths, or None if it cannot be asked.

  Read rather than hardcoded on purpose: ADR-0003's repath broke every consumer
  that had a prefix compiled into it. Longest first, so a broad root cannot
  swallow a more specific one.
  """
  folders = _get_json(f"{host.rstrip('/')}/Library/VirtualFolders", token)
  if not isinstance(folders, list):
    return None
  roots = [
    location
    for folder in folders
    if folder.get("CollectionType") == "music"
    for location in folder.get("Locations", [])
  ]
  return sorted(roots, key=len, reverse=True)


def audio_playlists(host: str, token: str) -> list[dict] | None:
  """Every Audio playlist Jellyfin holds, or None if it cannot be asked."""
  query = urllib.parse.urlencode(
    {"Recursive": "true", "IncludeItemTypes": "Playlist", "Fields": "Path"}
  )
  body = _get_json(f"{host.rstrip('/')}/Items?{query}", token)
  if not isinstance(body, dict):
    return None
  return [i for i in body.get("Items", []) if i.get("MediaType") == "Audio"]


def playlist_items(host: str, token: str, user_id: str, playlist_id: str) -> list[dict]:
  """The ordered items of one playlist. `userId` is REQUIRED -- without it the
  endpoint answers 400, and /Items?ParentId= answers 200 with an empty list."""
  query = urllib.parse.urlencode({"userId": user_id, "Fields": "Path"})
  body = _get_json(
    f"{host.rstrip('/')}/Playlists/{playlist_id}/Items?{query}", token
  )
  if not isinstance(body, dict):
    return []
  return body.get("Items", [])


def to_relative(jellyfin_path: str, roots: list[str]) -> str | None:
  """A Jellyfin absolute path as one relative to the music root, or None.

  None means the track is not under any music library -- a video dropped into an
  audio playlist, or a path left behind by a repath. The caller counts these; it
  must not silently write them, because an absolute Jellyfin path in an .m3u8
  resolves for nobody.
  """
  for root in roots:
    prefix = root.rstrip("/") + "/"
    if jellyfin_path.startswith(prefix):
      return jellyfin_path[len(prefix) :]
  return None


def sanitize_filename(name: str, fallback: str) -> str:
  """A filename that survives ext4, Subsonic clients and a sync to a phone.

  Lossy on purpose: the real name is carried in `#PLAYLIST:`, which Navidrome
  prefers over the filename (measured), so nothing is lost by being strict here.
  """
  normalized = unicodedata.normalize("NFC", name)
  cleaned = "".join(
    " " if (ch in _UNSAFE or unicodedata.category(ch)[0] == "C") else ch
    for ch in normalized
  )
  # Collapse runs of whitespace, and refuse names a filesystem treats specially.
  cleaned = " ".join(cleaned.split()).strip(" .")
  return cleaned[:120].strip() or fallback


def unique_filenames(playlists: list[Playlist]) -> dict[str, str]:
  """{jellyfin_id: filename}, with collisions broken deterministically.

  Two Jellyfin playlists really can sanitise to the same filename, and silently
  letting the second overwrite the first would lose a playlist while reporting
  success.
  """
  assigned: dict[str, str] = {}
  taken: set[str] = set()
  for playlist in sorted(playlists, key=lambda p: (p.name, p.jellyfin_id)):
    stem = sanitize_filename(playlist.name, fallback=playlist.jellyfin_id[:8])
    candidate = stem
    suffix = 2
    while candidate.casefold() in taken:
      candidate = f"{stem} ({suffix})"
      suffix += 1
    taken.add(candidate.casefold())
    assigned[playlist.jellyfin_id] = f"{candidate}.m3u8"
  return assigned


def render_m3u(playlist: Playlist, depth: int = 1) -> str:
  """The .m3u8 body. `depth` is how many levels below the music root it sits."""
  up = "../" * depth
  lines = [
    "#EXTM3U",
    f"#PLAYLIST:{playlist.name}",
    f"{MARKER_PREFIX}{playlist.jellyfin_id}",
  ]
  for track in playlist.tracks:
    label = f"{track.artist} - {track.title}" if track.artist else track.title
    lines.append(f"#EXTINF:{track.seconds},{label}")
    lines.append(f"{up}{track.relative_path}")
  return "\n".join(lines) + "\n"


def exported_files(directory: Path) -> dict[str, Path]:
  """{jellyfin_id: path} for the .m3u8 files THIS tool wrote.

  Keyed off the marker, so --prune can never delete a playlist someone made by
  hand and dropped in the same folder.
  """
  found: dict[str, Path] = {}
  if not directory.is_dir():
    return found
  for candidate in sorted(directory.glob("*.m3u8")):
    try:
      text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
      continue
    for line in text.splitlines():
      if line.startswith(MARKER_PREFIX):
        found[line[len(MARKER_PREFIX) :].strip()] = candidate
        break
  return found


def _write_atomic(target: Path, body: str) -> None:
  """Write via a temp file + os.replace, so a reader never sees half a file."""
  tmp = target.with_name(target.name + ".tmp")
  tmp.write_text(body, encoding="utf-8")
  os.replace(tmp, target)


def collect(host: str, token: str, user_id: str, roots: list[str], music_root: Path) -> list[Playlist]:
  """Every Audio playlist, resolved to relative paths that exist on disk."""
  collected: list[Playlist] = []
  for entry in audio_playlists(host, token) or []:
    playlist = Playlist(jellyfin_id=entry["Id"], name=entry.get("Name") or entry["Id"])
    for item in playlist_items(host, token, user_id, playlist.jellyfin_id):
      raw = item.get("Path") or ""
      relative = to_relative(raw, roots)
      if relative is None or not (music_root / relative).exists():
        playlist.skipped.append(raw or "<no path>")
        continue
      artists = item.get("Artists") or []
      playlist.tracks.append(
        Track(
          relative_path=relative,
          title=item.get("Name") or Path(relative).stem,
          artist=", ".join(artists) or (item.get("AlbumArtist") or ""),
          seconds=int((item.get("RunTimeTicks") or 0) // TICKS_PER_SECOND),
        )
      )
    collected.append(playlist)
  return collected


def trigger_navidrome_scan(host: str, user: str, password: str) -> bool:
  """Ask Navidrome to pick the files up now instead of at the next sweep."""
  query = urllib.parse.urlencode(
    {"u": user, "p": password, "v": "1.16.1", "c": "nas-playlist-export", "f": "json"}
  )
  try:
    with urllib.request.urlopen(  # noqa: S310 - localhost
      f"{host.rstrip('/')}/rest/startScan.view?{query}", timeout=30
    ) as resp:
      body = json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError):
    return False
  return body.get("subsonic-response", {}).get("status") == "ok"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument("--host", default=os.getenv("JELLYFIN_HOST", DEFAULT_JELLYFIN_HOST))
  parser.add_argument(
    "--navidrome-host", default=os.getenv("NAVIDROME_HOST", DEFAULT_NAVIDROME_HOST)
  )
  parser.add_argument(
    "--out",
    type=Path,
    default=None,
    help="where to write (default: ${SHARE_DIRECTORY}/music/Playlists)",
  )
  parser.add_argument("--apply", action="store_true", help="write files (default: dry run)")
  parser.add_argument(
    "--prune",
    action="store_true",
    help="delete exported playlists Jellyfin no longer has (marker-gated)",
  )
  parser.add_argument(
    "--rescan", action="store_true", help="ask Navidrome to scan once the files are written"
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911 - one exit per fatal cause
  args = parse_args(argv)

  token = os.getenv("API_KEY_JELLYFIN")
  user_id = os.getenv("JELLYFIN_USER_ID")
  if not token or not user_id:
    print("!!! API_KEY_JELLYFIN and JELLYFIN_USER_ID must both be set", file=sys.stderr)
    return 2

  share = os.getenv("SHARE_DIRECTORY")
  if not share:
    print("!!! SHARE_DIRECTORY is not set", file=sys.stderr)
    return 2
  music_root = Path(share) / "music"
  if not music_root.is_dir():
    print(f"!!! {music_root} does not exist", file=sys.stderr)
    return 2
  out_dir = args.out or (music_root / PLAYLIST_SUBDIR)

  roots = music_library_roots(args.host, token)
  if roots is None:
    print(f"!!! Jellyfin unreachable at {args.host}", file=sys.stderr)
    return 2
  if not roots:
    print(
      "!!! Jellyfin has no library with CollectionType=music, so there is no\n"
      "    path namespace to translate from. ADR-0016",
      file=sys.stderr,
    )
    return 2

  playlists = collect(args.host, token, user_id, roots, music_root)
  if not playlists:
    print("no audio playlists in Jellyfin -- nothing to export")
    return 0

  resolved = sum(len(p.tracks) for p in playlists)
  skipped = sum(len(p.skipped) for p in playlists)
  if resolved == 0 and skipped > 0:
    print(
      f"!!! not one of {skipped} tracks is under {roots}. The music library path\n"
      "    has moved and every path written would resolve for nobody. Refusing\n"
      "    to write. ADR-0016, ADR-0003",
      file=sys.stderr,
    )
    return 2

  filenames = unique_filenames(playlists)
  existing = exported_files(out_dir)
  outcome = Outcome(skipped_tracks=skipped, total_tracks=resolved + skipped)

  depth = 1 if out_dir.parent == music_root else 0
  for playlist in playlists:
    target = out_dir / filenames[playlist.jellyfin_id]
    body = render_m3u(playlist, depth=depth)
    current = target.read_text(encoding="utf-8") if target.is_file() else None
    if current == body:
      outcome.unchanged.append(target.name)
      continue
    outcome.written.append(target.name)
    if args.apply:
      out_dir.mkdir(parents=True, exist_ok=True)
      _write_atomic(target, body)

  if args.prune:
    live = set(filenames)
    for jellyfin_id, path in existing.items():
      if jellyfin_id in live and path.name == filenames[jellyfin_id]:
        continue
      outcome.pruned.append(path.name)
      if args.apply:
        path.unlink(missing_ok=True)

  verb = "wrote" if args.apply else "would write"
  print(f"{len(playlists)} audio playlists -> {out_dir}")
  print(f"  {verb} {len(outcome.written)}, unchanged {len(outcome.unchanged)}")
  if outcome.pruned:
    print(f"  {'pruned' if args.apply else 'would prune'} {len(outcome.pruned)}: "
          f"{', '.join(sorted(outcome.pruned)[:5])}")
  print(f"  tracks resolved {resolved}/{outcome.total_tracks}")
  for playlist in playlists:
    if playlist.skipped:
      print(f"    - {playlist.name!r}: {len(playlist.skipped)} skipped "
            f"(first: {playlist.skipped[0]})")

  if not args.apply:
    print("\ndry run -- nothing written. Re-run with --apply.")
    return 1 if skipped else 0

  if args.rescan:
    user = os.getenv("NAVIDROME_LIDARR_USER")
    password = os.getenv("NAVIDROME_LIDARR_PASSWORD")
    if user and password and trigger_navidrome_scan(args.navidrome_host, user, password):
      print("  navidrome: scan requested")
    else:
      print("  navidrome: could not request a scan (it will pick them up on its own)")

  return 1 if skipped else 0


if __name__ == "__main__":
  sys.exit(main())
