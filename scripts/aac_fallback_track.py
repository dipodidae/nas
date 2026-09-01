#!/usr/bin/env python3
"""Add a default stereo AAC track to DTS-only media so browsers Direct Play it.

Why this exists
---------------
Browsers cannot decode DTS, and cannot play MKV at all, so a DTS-only file makes
Jellyfin remux the container *and* transcode the audio for every web client. That
is the exact path the Fargo stutter came down (docs/jellyfin-playback-audit.md):
video was already `-codec:v:0 copy`, only the DTS audio was being converted.

Two shapes of file, handled automatically. Where a browser-safe track already
exists and is simply not flagged default, only the flag is moved — lossless,
no re-encode, no extra size. Where there is none, a stereo AAC track is added
*and* flagged default.

The non-obvious part, and the reason a naive "just add an AAC track" pass does
nothing: **Jellyfin's StreamBuilder evaluates the default audio stream.** With
DTS still flagged default, `/Items/{id}/PlaybackInfo` returns
`SupportsDirectPlay: false` even with a perfectly good AAC track sitting right
next to it. Re-muxing with AAC as the default flips it to
`SupportsDirectPlay: true, TranscodingReasons: null`. So this script always does
both: append the track *and* move the default disposition onto it.

Trade-off worth knowing before running this widely: making the stereo AAC track
default means a client that *could* handle DTS 5.1 — the living-room TV app —
now gets stereo unless the viewer picks the surround track. The original DTS
stream is still there, byte-identical, first in the file, just no longer default.

Nothing is destroyed. The untouched original is moved to --originals-dir with
its path under the media root preserved, so a revert is a plain `mv` back.

Exit codes
----------
  0  every selected file was processed (or was already fine)
  1  partial — at least one file processed and at least one failed
  2  fatal (bad arguments, ffmpeg unavailable, nothing selected)

Environment
-----------
  SHARE_DIRECTORY   (required) media root on the host, e.g. /mnt/drive

Usage
-----
  # default is a dry run: show what would change, touch nothing
  python scripts/aac_fallback_track.py --root "$SHARE_DIRECTORY/series/Fargo/Season 1"
  python scripts/aac_fallback_track.py --root ... --limit 10 --apply
  python scripts/aac_fallback_track.py --file /mnt/drive/series/X/Y.mkv --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if "SHARE_DIRECTORY" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


# There is no ffmpeg on this host and installing one needs root, so the default
# is the exact binary Jellyfin itself uses, run from its own image. Overridable
# for a host that does have ffmpeg on PATH.
JELLYFIN_IMAGE = "lscr.io/linuxserver/jellyfin:latest"
FFMPEG_IN_IMAGE = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE_IN_IMAGE = "/usr/lib/jellyfin-ffmpeg/ffprobe"
VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v"}
# Codecs a browser can Direct Play. A file whose default audio is already one of
# these needs nothing from this script.
BROWSER_SAFE_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac"}
DEFAULT_BITRATE = "256k"
DEFAULT_LIMIT = 10


@dataclass(frozen=True)
class AudioStream:
  index: int
  codec: str
  channels: int
  language: str
  is_default: bool


@dataclass(frozen=True)
class Probe:
  path: Path
  audio: tuple[AudioStream, ...]

  @property
  def default_audio(self) -> AudioStream | None:
    for stream in self.audio:
      if stream.is_default:
        return stream
    return self.audio[0] if self.audio else None


def docker_prefix(binary: str, share: Path) -> list[str]:
  """A `docker run` wrapper exposing the media root read-write to one binary."""
  return [
    "docker", "run", "--rm", "--network", "none",
    "--user", f"{os.getenv('PUID', '1000')}:{os.getenv('PGID', '1000')}",
    "-v", f"{share}:{share}",
    "--entrypoint", binary,
    JELLYFIN_IMAGE,
  ]  # fmt: skip


def probe(ffprobe: list[str], path: Path) -> Probe | None:
  """Read the audio stream layout. None if ffprobe cannot read the file."""
  cmd = [*ffprobe, "-v", "error", "-show_streams", "-select_streams", "a",
         "-of", "json", str(path)]  # fmt: skip
  try:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    streams = json.loads(proc.stdout)["streams"] if proc.returncode == 0 else None
  except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
    return None
  if streams is None:
    return None
  return Probe(
    path=path,
    audio=tuple(
      AudioStream(
        index=position,
        codec=str(s.get("codec_name", "")).lower(),
        channels=int(s.get("channels") or 0),
        language=str((s.get("tags") or {}).get("language") or "und"),
        is_default=bool((s.get("disposition") or {}).get("default")),
      )
      for position, s in enumerate(streams)
    ),
  )


def needs_work(p: Probe) -> bool:
  """True when the default audio stream is not something a browser can play."""
  default = p.default_audio
  return bool(p.audio) and (default is None or default.codec not in BROWSER_SAFE_AUDIO)


def existing_safe_index(p: Probe) -> int | None:
  """Index of an audio stream a browser could already play, if there is one."""
  for stream in p.audio:
    if stream.codec in BROWSER_SAFE_AUDIO:
      return stream.index
  return None


def build_flip_command(ffmpeg: list[str], src: Path, dst: Path, index: int) -> list[str]:
  """Move the default flag onto an existing browser-safe track. No re-encode.

  Some files already carry both a surround track and a stereo AAC one and are
  transcoded anyway purely because the surround track is flagged default. Those
  need nothing added — only the flag moved. Every stream is copied bit for bit,
  so this is lossless and adds no size; it still rewrites the container, since
  there is no mkvpropedit in any image on this host to edit the flag in place.
  """
  return [
    *ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src),
    "-map", "0", "-c", "copy",
    "-disposition:a", "0",
    f"-disposition:a:{index}", "default",
    str(dst),
  ]  # fmt: skip


def build_command(ffmpeg: list[str], src: Path, dst: Path, p: Probe, bitrate: str) -> list[str]:
  """Copy every stream, append a stereo AAC encode of the first audio track.

  `-map 0` keeps video, all original audio, subtitles, chapters and attachments
  untouched; the extra `-map 0:a:0` appends one more audio stream, which is the
  only one re-encoded. Its output index is therefore the original audio count.
  The two `-disposition` flags run in order: clear default from every audio
  stream, then set it on the new one — which is the half that actually changes
  Jellyfin's Direct Play decision.
  """
  new_index = len(p.audio)
  source = p.audio[0]
  return [
    *ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src),
    "-map", "0", "-map", "0:a:0",
    "-c", "copy",
    f"-c:a:{new_index}", "aac", f"-b:a:{new_index}", bitrate, f"-ac:a:{new_index}", "2",
    "-disposition:a", "0",
    f"-disposition:a:{new_index}", "default",
    f"-metadata:s:a:{new_index}", "title=Stereo (AAC) - Direct Play",
    f"-metadata:s:a:{new_index}", f"language={source.language}",
    str(dst),
  ]  # fmt: skip


def select_files(root: Path | None, explicit: list[Path]) -> list[Path]:
  """Video files to consider, sorted so batches are reproducible."""
  if explicit:
    return sorted(explicit)
  if root is None:
    return []
  return sorted(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file())


def preserve_original(src: Path, share: Path, originals: Path) -> Path:
  """Move the untouched original under `originals`, keeping its relative path."""
  try:
    relative = src.relative_to(share)
  except ValueError:
    relative = Path(src.name)
  target = originals / relative
  target.parent.mkdir(parents=True, exist_ok=True)
  shutil.move(str(src), str(target))
  return target


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Add a default stereo AAC track to media whose default audio a browser can't play.",
  )
  parser.add_argument("--root", type=Path, help="Directory to walk for video files.")
  parser.add_argument("--file", type=Path, action="append", default=[], help="Explicit file (repeatable).")
  parser.add_argument("--originals-dir", type=Path, help="Where originals are moved (default: <SHARE>/backups/aac-remux-originals).")
  parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Max files to convert (default {DEFAULT_LIMIT}).")
  parser.add_argument("--bitrate", default=DEFAULT_BITRATE, help=f"AAC bitrate (default {DEFAULT_BITRATE}).")
  parser.add_argument(
    "--flip-only",
    action="store_true",
    help=(
      "Only touch files that already have a browser-safe track and just need the "
      "default flag moved. Skips anything that would need an encode — the free "
      "half of the work, with no stereo-default trade-off to weigh."
    ),
  )
  parser.add_argument("--apply", action="store_true", help="Actually convert. Without it this is a dry run.")
  parser.add_argument("--ffmpeg", help="ffmpeg binary on PATH (default: run Jellyfin's own from its image).")
  parser.add_argument("--ffprobe", help="ffprobe binary on PATH (default: as above).")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:  # noqa: PLR0911
  args = parse_args(argv)
  share_raw = os.getenv("SHARE_DIRECTORY")
  if not share_raw:
    print("ERROR: SHARE_DIRECTORY not set (expected in .env)", file=sys.stderr)
    return 2
  share = Path(share_raw)
  originals = args.originals_dir or (share / "backups" / "aac-remux-originals")

  ffmpeg = [args.ffmpeg] if args.ffmpeg else docker_prefix(FFMPEG_IN_IMAGE, share)
  ffprobe = [args.ffprobe] if args.ffprobe else docker_prefix(FFPROBE_IN_IMAGE, share)

  candidates = select_files(args.root, args.file)
  if not candidates:
    print("ERROR: nothing selected — pass --root DIR or --file FILE", file=sys.stderr)
    return 2

  converted = failed = skipped = 0
  for path in candidates:
    if converted >= args.limit:
      print(f"limit of {args.limit} reached; {len(candidates) - skipped - converted - failed} left unexamined")
      break
    p = probe(ffprobe, path)
    if p is None:
      print(f"ERROR: ffprobe failed on {path}", file=sys.stderr)
      failed += 1
      continue
    if not needs_work(p):
      default = p.default_audio
      print(f"skip  {path.name} (default audio already {default.codec if default else 'none'})")
      skipped += 1
      continue

    default = p.default_audio
    assert default is not None
    safe_index = existing_safe_index(p)
    if safe_index is None and args.flip_only:
      print(f"skip  {path.name} (--flip-only, and this one would need an encode)")
      skipped += 1
      continue
    if safe_index is None:
      plan = "+aac 2ch default"
    else:
      plan = f"flag stream a:{safe_index} ({p.audio[safe_index].codec}) default, no re-encode"
    print(f"{'convert' if args.apply else 'DRY-RUN'}  {path.name} "
          f"(default {default.codec} {default.channels}ch -> {plan})")
    if not args.apply:
      converted += 1
      continue

    # Stage outside the library, and keep the real extension last: ffmpeg picks
    # the muxer from the filename, and a half-written *.mkv sitting in the media
    # folder is something Jellyfin or Sonarr could pick up mid-conversion.
    staging = originals / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / f"{path.stem}.aacfallback{path.suffix}"
    try:
      command = (
        build_flip_command(ffmpeg, path, staged, safe_index)
        if safe_index is not None
        else build_command(ffmpeg, path, staged, p, args.bitrate)
      )
      proc = subprocess.run(command, capture_output=True, text=True, timeout=7200, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
      print(f"ERROR: ffmpeg failed on {path}: {exc}", file=sys.stderr)
      staged.unlink(missing_ok=True)
      failed += 1
      continue
    if proc.returncode != 0 or not staged.exists():
      print(f"ERROR: ffmpeg exit {proc.returncode} on {path}: {proc.stderr.strip()[:300]}", file=sys.stderr)
      staged.unlink(missing_ok=True)
      failed += 1
      continue

    # Verify the staged file before anything irreversible happens to the original.
    verified = probe(ffprobe, staged)
    new_default = verified.default_audio if verified else None
    if new_default is None or new_default.codec not in BROWSER_SAFE_AUDIO:
      print(f"ERROR: {staged.name} did not end up with a browser-safe default track; discarding", file=sys.stderr)
      staged.unlink(missing_ok=True)
      failed += 1
      continue

    kept = preserve_original(path, share, originals)
    os.replace(staged, path)
    print(f"  ok    {len(p.audio)} -> {len(verified.audio)} audio streams, "
          f"default now {new_default.codec} {new_default.channels}ch; original kept at {kept}")
    converted += 1

  print(f"\nsummary: converted={converted} skipped={skipped} failed={failed}"
        f"{' (dry run — nothing changed)' if not args.apply else ''}")
  if failed and converted:
    return 1
  return 2 if failed else 0


if __name__ == "__main__":
  sys.exit(main())
