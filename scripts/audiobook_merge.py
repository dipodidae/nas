#!/usr/bin/env python3
"""Merge each multi-file audiobook into one chaptered M4B, so Jellyfin shows it as ONE book.

Why
---
Jellyfin does not stack a multi-file audiobook into one item: its AudioResolver
skips any audiobook with more than one file, "until we sort out naming for
multi-part books" (Emby.Server.Implementations, still on master in 2026-09).
Every chapter MP3 became its own book -- "001", "Chapter 1. The Sacrifice
Poles" -- 30 items for 5 books. No folder or file naming fixes that; one file
per book does, and a chaptered M4B is what audiobook players expect anyway:
chapter markers, an embedded cover, resume position.

What it does, for every bookshelf-audio book held as more than one audio file:

  1. probe the parts (ordered by path -- Bookshelf names them "... (01)" etc.,
     ADR-0059) for duration, bitrate, channels and title tag;
  2. encode them, in order, into ONE M4B (libfdk_aac at the source bitrate,
     floored at 48k stereo / 32k mono, capped at 128k, source channels), one chapter per part, titled from
     the part's own tag when it means something ("Chapter 4. The Bomb Circle")
     and "Part N" when it does not ("001"); cover and book metadata embedded;
  3. verify: duration within 1% of the parts' sum, one chapter per part --
     otherwise nothing is kept;
  4. swap it in THROUGH Bookshelf: the parts are deleted via its API (so they
     land in its 14-day recycle bin, ADR-0059) and the M4B is ManualImported as
     that book. The torrent's own copy under downloads/ is a separate hardlink
     and keeps seeding, untouched.

The encode runs in a throwaway container from the jellyfin image already on
disk (jellyfin-ffmpeg carries libfdk_aac), as ${PUID}:${PGID}, CPU-capped and
niced. Work happens in ${SHARE_DIRECTORY}/downloads/audiobook-merge, outside
the library, so Bookshelf's folder watcher never sees a half-written file.

Dry-run is the DEFAULT; pass --apply to change anything.

Exit codes
----------
  0  nothing to merge, or every merge succeeded (or the dry run)
  1  partial: at least one book failed; its parts are untouched
  2  fatal: Bookshelf/Docker unavailable, or a required env var unset
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

if "API_KEY_BOOKSHELF_AUDIO" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO / ".env")
  except ImportError:
    pass

BOOKSHELF = "http://localhost:8788/api/v1"
AUDIO_EXT = {".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus", ".aac", ".wma"}
WORK_SUBDIR = Path("downloads") / "audiobook-merge"
FFMPEG = "/usr/lib/jellyfin-ffmpeg/ffmpeg"
FFPROBE = "/usr/lib/jellyfin-ffmpeg/ffprobe"
# AAC-LC floors: below these LC audibly smears speech. 48k stereo / 32k mono
# from a ~40k MP3 source is transparent for the spoken word.
MIN_KBPS_STEREO, MIN_KBPS_MONO, MAX_KBPS = 48, 32, 128
DURATION_TOLERANCE = 0.01


@dataclass(frozen=True)
class Part:
  host_path: Path
  duration: float     # seconds
  kbps: int
  channels: int
  title: str


# --- pure --------------------------------------------------------------------


def part_number(path: str) -> int:
  """'... (07).mp3' -> 7; unnumbered sorts last in path order."""
  m = re.search(r"\((\d+)\)\.[^.]+$", path)
  return int(m.group(1)) if m else 10**6


def target_kbps(parts: list[Part]) -> int:
  """The parts' own bitrate, clamped: AAC-LC at an MP3's bitrate is no worse
  above the LC floor, and spoken word gains nothing above 128k."""
  src = max((p.kbps for p in parts if p.kbps), default=64)
  floor = MIN_KBPS_STEREO if max((p.channels for p in parts), default=2) > 1 else MIN_KBPS_MONO
  return max(floor, min(MAX_KBPS, src))


def meaningful(title: str) -> bool:
  """A part's own tag is worth a chapter name unless it is a bare number,
  empty, or 'Track 3'-style filler."""
  t = (title or "").strip()
  return bool(t) and not re.fullmatch(r"(?i)(track|part|disc|cd)?[\s_-]*\d+", t)


def chapter_titles(parts: list[Part]) -> list[str]:
  return [p.title.strip() if meaningful(p.title) else f"Part {i}"
          for i, p in enumerate(parts, 1)]


def ffmetadata(title: str, author: str, year: str, parts: list[Part]) -> str:
  """FFMETADATA1 with global tags and one chapter per part (ms timebase)."""
  def esc(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s or "")
  out = [";FFMETADATA1", f"title={esc(title)}", f"album={esc(title)}",
         f"artist={esc(author)}", f"album_artist={esc(author)}", "genre=Audiobook"]
  if year:
    out.append(f"date={esc(year)}")
  start = 0
  for name, p in zip(chapter_titles(parts), parts, strict=True):
    end = start + round(p.duration * 1000)
    out += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={esc(name)}"]
    start = end
  return "\n".join(out) + "\n"


def concat_list(container_paths: list[str]) -> str:
  return "".join("file '" + p.replace("'", "'\\''") + "'\n" for p in container_paths)


def duration_ok(expected: float, actual: float) -> bool:
  return expected > 0 and abs(actual - expected) <= expected * DURATION_TOLERANCE


# --- I/O -----------------------------------------------------------------------


def _api(method: str, path: str, key: str, body: object | None = None, timeout: float = 120):
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request(BOOKSHELF + path, data=data, method=method, headers={
    "X-Api-Key": key, "Content-Type": "application/json"})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    raw = resp.read()
  return json.loads(raw) if raw else None


def jellyfin_image() -> str:
  out = subprocess.run(["docker", "inspect", "-f", "{{.Config.Image}}", "jellyfin"],
                       capture_output=True, text=True, check=True)
  return out.stdout.strip()


def in_container(image: str, share: Path, args: list[str], cpus: str = "4") -> subprocess.CompletedProcess:
  """Run a jellyfin-ffmpeg binary against the share, mounted at /share."""
  uid, gid = os.environ.get("PUID", "1000"), os.environ.get("PGID", "1000")
  return subprocess.run(
    ["docker", "run", "--rm", "--network", "none", "--user", f"{uid}:{gid}",
     "--cpus", cpus, "--entrypoint", "nice", "-v", f"{share}:/share", image,
     "-n", "10", *args],
    capture_output=True, text=True, check=False)


def to_container(share: Path, host: Path) -> str:
  return "/share/" + host.relative_to(share).as_posix()


def probe(image: str, share: Path, host: Path) -> Part:
  res = in_container(image, share, [FFPROBE, "-v", "quiet", "-print_format", "json",
                                    "-show_format", "-show_streams", to_container(share, host)], cpus="1")
  data = json.loads(res.stdout or "{}")
  audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), {})
  fmt = data.get("format") or {}
  tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
  bit_rate = int(audio.get("bit_rate") or fmt.get("bit_rate") or 0)
  return Part(host, float(fmt.get("duration") or 0), bit_rate // 1000,
              int(audio.get("channels") or 2), tags.get("title", ""))


def merge_book(book: dict, author: dict, files: list[dict], key: str, image: str,
               share: Path, apply: bool) -> str:
  """Returns '' on success (or a successful dry run), else the reason."""
  hosts = sorted((share / Path(f["path"]).relative_to("/data") for f in files),
                 key=lambda p: (part_number(str(p)), str(p)))
  parts = [probe(image, share, h) for h in hosts]
  if any(p.duration <= 0 for p in parts):
    return "a part has no readable duration"
  total = sum(p.duration for p in parts)
  kbps, channels = target_kbps(parts), max(p.channels for p in parts)
  label = f"{author['authorName']} — {book['title']}"
  print(f"  {label}: {len(parts)} parts, {total / 3600:.1f} h -> one M4B at {kbps}k/{channels}ch"
        + ("" if apply else "  (dry run)"))
  if not apply:
    return ""

  work = share / WORK_SUBDIR / str(book["id"])
  shutil.rmtree(work, ignore_errors=True)
  work.mkdir(parents=True)
  try:
    (work / "list.txt").write_text(concat_list([to_container(share, h) for h in hosts]))
    (work / "meta.txt").write_text(ffmetadata(book["title"], author["authorName"],
                                              (book.get("releaseDate") or "")[:4], parts))
    cover = hosts[0].parent / "cover.jpg"
    out = work / "out.m4b"
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", to_container(share, work / "list.txt"),
            "-i", to_container(share, work / "meta.txt")]
    if cover.exists():
      args += ["-i", to_container(share, cover)]
    args += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if cover.exists():
      args += ["-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    # AAC-LC. HE-AAC would suit ~40k speech better, but this jellyfin-ffmpeg's
    # libfdk writes no packets for ANY aac_he/aac_he_v2 setting (measured), so
    # the floor in target_kbps is what keeps LC clean instead.
    args += ["-c:a", "libfdk_aac", "-b:a", f"{kbps}k", "-ac", str(channels),
             "-movflags", "+faststart", "-f", "ipod", to_container(share, out)]
    res = in_container(image, share, args)
    if res.returncode != 0 or not out.exists():
      return f"ffmpeg failed: {res.stderr.strip()[-300:]}"

    check = in_container(image, share, [FFPROBE, "-v", "quiet", "-print_format", "json",
                                        "-show_format", "-show_chapters", to_container(share, out)], cpus="1")
    info = json.loads(check.stdout or "{}")
    got = float((info.get("format") or {}).get("duration") or 0)
    if not duration_ok(total, got):
      return f"merged duration {got:.0f}s != parts {total:.0f}s"
    if len(info.get("chapters") or []) != len(parts):
      return f"merged file has {len(info.get('chapters') or [])} chapters, expected {len(parts)}"

    # Swap: the M4B into the book folder, the parts out through Bookshelf.
    final = hosts[0].parent / f"{author['authorName']} - {book['title']}.m4b"
    shutil.move(str(out), final)
    for f in files:
      _api("DELETE", f"/bookfile/{f['id']}", key)
    container_final = "/data/" + final.relative_to(share).as_posix()
    items = _api("GET", "/manualimport?folder=" + urllib.parse.quote(str(Path(container_final).parent))
                 + "&filterExistingFiles=false", key) or []
    item = next((i for i in items if i.get("path") == container_final), None)
    if item is None:
      return f"Bookshelf does not see {container_final} (parts already recycled; import it by hand)"
    edition = next((e for e in _api("GET", f"/edition?bookId={book['id']}", key) or []
                    if e.get("monitored")), None)
    cmd = _api("POST", "/command", key, {
      "name": "ManualImport", "importMode": "move", "replaceExistingFiles": True,
      "files": [{"path": container_final, "authorId": book["authorId"], "bookId": book["id"],
                 "foreignEditionId": (edition or {}).get("foreignEditionId") or book.get("foreignEditionId"),
                 "quality": item.get("quality"), "indexerFlags": 0, "downloadId": "",
                 "disableReleaseSwitching": True}]})
    for _ in range(60):
      time.sleep(2)
      if (_api("GET", f"/command/{cmd['id']}", key) or {}).get("status") in ("completed", "failed"):
        break
    now = _api("GET", f"/bookfile?bookId={book['id']}", key) or []
    if len(now) != 1 or not now[0]["path"].endswith(".m4b"):
      return f"after import Bookshelf holds {len(now)} file(s) for this book"
    return ""
  finally:
    shutil.rmtree(work, ignore_errors=True)
    with contextlib.suppress(OSError):
      (share / WORK_SUBDIR).rmdir()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("--apply", action="store_true", help="actually merge (default: dry run)")
  ap.add_argument("--max-books", type=int, default=5, help="books merged per run (default 5)")
  return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  key, share = os.environ.get("API_KEY_BOOKSHELF_AUDIO"), os.environ.get("SHARE_DIRECTORY")
  if not key or not share:
    print("FATAL: API_KEY_BOOKSHELF_AUDIO and SHARE_DIRECTORY must be set", file=sys.stderr)
    return 2
  try:
    image = jellyfin_image()
    authors = {a["id"]: a for a in _api("GET", "/author", key)}
    books = {b["id"]: b for b in _api("GET", "/book", key)}
  except (OSError, subprocess.CalledProcessError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: {exc}", file=sys.stderr)
    return 2

  todo = []
  for author_id in authors:
    by_book: dict[int, list[dict]] = {}
    for f in _api("GET", f"/bookfile?authorId={author_id}", key) or []:
      if Path(f["path"]).suffix.lower() in AUDIO_EXT:
        by_book.setdefault(f["bookId"], []).append(f)
    todo += [(books[b], authors[author_id], fs) for b, fs in by_book.items()
             if len(fs) > 1 and b in books]
  print(f"{len(todo)} multi-file audiobook(s)" + ("" if args.apply else " -- dry run"))

  failed = 0
  for book, author, files in todo[: args.max_books]:
    try:
      reason = merge_book(book, author, files, key, image, Path(share), args.apply)
    except (OSError, urllib.error.URLError, ValueError, subprocess.SubprocessError) as exc:
      reason = f"{type(exc).__name__}: {exc}"
    if reason:
      failed += 1
      print(f"  FAILED {author['authorName']} — {book['title']}: {reason}")
    elif args.apply:
      print(f"  merged {author['authorName']} — {book['title']}")
  return 1 if failed else 0


if __name__ == "__main__":
  sys.exit(main())
