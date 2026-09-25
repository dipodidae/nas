#!/usr/bin/env python3
"""Pre-extract embedded subtitles into Jellyfin's cache so they load instantly.

Why this exists (2026-09-25). Picking an embedded subtitle in Jellyfin took 30 s to
"never". Subtitle packets are interleaved through the whole container, so to serve a
70 KB ``.srt`` Jellyfin runs ffmpeg over the ENTIRE file before returning a single
cue -- and the media lives on a USB spinning disk (~110 MB/s). Measured on this host:

* a 3.9 GB WEBDL episode: **35 s** cold, 0.7 s from page cache -- pure disk read;
* the same episodes during playback, with the video stream on the same spindle:
  **173 s and 239 s**;
* 12 Angry Men (29.5 GB remux, 46 embedded tracks): extraction finished after
  **5 and 12 minutes**, while the browser had given up at ~60 s (six ``499`` s in
  SWAG's jellyfin access log). That is the "baked-in subs never load" case.

Jellyfin caches the result (``/config/data/subtitles/<id>/<index>.<ext>``) and has
no scheduled task that fills it, so the cost is paid by whoever presses play first.
This job pays it ahead of time: for each video with an embedded TEXT subtitle that
is not cached, it requests one subtitle stream through Jellyfin's own API. Jellyfin
extracts every text track of that file in one ffmpeg pass into its own cache, so the
player later gets the same files it would have produced itself.

Image subtitles (PGS/VobSub) are left alone: Jellyfin extracts those one pass per
TRACK (46 tracks = 46 full reads of 12 Angry Men), so pre-warming them all is not
worth the disk.

Guards, because every item costs a full read of the file:

* nothing starts while someone is playing something -- the extraction would compete
  with that playback for the same disk, which is exactly the 239 s case above;
* a time budget per run, so an hourly cron cannot pile up behind itself;
* an item that fails ``MAX_FAILURES`` times is skipped from then on (state file),
  instead of re-reading a broken 30 GB file every hour forever.

Exit: 0 ok (including "deferred, someone is watching") / 1 some items failed /
2 Jellyfin unreachable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if "API_KEY_JELLYFIN" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://localhost:8096")
CACHE_ROOT = Path(
  os.environ.get(
    "JELLYFIN_SUBTITLE_CACHE",
    ".docker-config/jellyfin/data/data/subtitles",
  )
)
STATE_FILE = Path("logs/cron-state/jellyfin-subtitle-prewarm-failures.json")

# Codecs Jellyfin extracts in one shared pass (MediaEncoding SubtitleEncoder).
TEXT_CODECS = frozenset({"subrip", "srt", "ass", "ssa", "webvtt", "vtt", "mov_text", "text"})
# Matches Jellyfin's own SubtitleExtractionTimeoutMinutes (encoding.xml).
REQUEST_TIMEOUT_S = 30 * 60
MAX_FAILURES = 3

OK, PARTIAL, FATAL = 0, 1, 2


@dataclass(frozen=True)
class Candidate:
  item_id: str
  media_source_id: str
  name: str
  size_gb: float
  text_indexes: tuple[int, ...]


def dashed(item_id: str) -> str:
  """Jellyfin's API ids are bare hex; its cache directories use the dashed GUID."""
  h = item_id.replace("-", "").lower()
  return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def cache_dir(item_id: str, root: Path = CACHE_ROOT) -> Path:
  g = dashed(item_id)
  return root / g[:2] / g


def is_cached(cand: Candidate, root: Path = CACHE_ROOT) -> bool:
  """True when every embedded text track already has an extracted file."""
  d = cache_dir(cand.item_id, root)
  if not d.is_dir():
    return False
  present = {p.name.split(".", 1)[0] for p in d.iterdir() if not p.name.endswith(".meta")}
  return all(str(i) in present for i in cand.text_indexes)


def candidates_from_items(items: list[dict]) -> list[Candidate]:
  """Videos with at least one embedded text subtitle, in the order given."""
  out: list[Candidate] = []
  for it in items:
    sources = it.get("MediaSources") or []
    if not sources:
      continue
    src = sources[0]
    idx = tuple(
      s["Index"]
      for s in src.get("MediaStreams") or it.get("MediaStreams") or []
      if s.get("Type") == "Subtitle"
      and not s.get("IsExternal")
      and (s.get("Codec") or "").lower() in TEXT_CODECS
    )
    if not idx:
      continue
    name = it.get("Name", "?")
    if it.get("SeriesName"):
      name = f"{it['SeriesName']} - {name}"
    out.append(
      Candidate(
        item_id=it["Id"],
        media_source_id=src.get("Id") or it["Id"],
        name=name,
        size_gb=(src.get("Size") or 0) / 1e9,
        text_indexes=idx,
      )
    )
  return out


def someone_is_playing(sessions: list[dict]) -> str | None:
  """Name of what is playing right now, or None."""
  for s in sessions:
    item = s.get("NowPlayingItem")
    if item and not (s.get("PlayState") or {}).get("IsPaused"):
      return f"{s.get('UserName', '?')}: {item.get('Name', '?')}"
  return None


def load_failures(path: Path = STATE_FILE) -> dict[str, int]:
  try:
    return {k: int(v) for k, v in json.loads(path.read_text()).items()}
  except (OSError, ValueError):
    return {}


def save_failures(failures: dict[str, int], path: Path = STATE_FILE) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(failures, indent=2, sort_keys=True) + "\n")


def _request(path: str, token: str, timeout: float) -> bytes:
  req = urllib.request.Request(
    f"{JELLYFIN_URL}{path}",
    headers={"Authorization": f'MediaBrowser Token="{token}"'},
  )
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    return resp.read()


def _api(path: str, token: str) -> object:
  return json.loads(_request(path, token, timeout=60))


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("--dry-run", action="store_true", help="list what would be extracted")
  ap.add_argument("--budget-min", type=float, default=50, help="stop starting new items after this")
  ap.add_argument("--limit", type=int, default=0, help="max items this run (0 = no cap)")
  ap.add_argument("--ignore-playback", action="store_true", help="run even while something plays")
  args = ap.parse_args()

  token = os.environ.get("API_KEY_JELLYFIN", "")
  if not token:
    print("API_KEY_JELLYFIN not set", file=sys.stderr)
    return FATAL

  try:
    items = _api(
      "/Items?Recursive=true&IncludeItemTypes=Movie,Episode"
      "&Fields=MediaSources,MediaStreams&SortBy=DateCreated&SortOrder=Descending",
      token,
    )["Items"]
  except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
    print(f"jellyfin unreachable: {exc}", file=sys.stderr)
    return FATAL

  cands = candidates_from_items(items)
  failures = load_failures()
  todo = [c for c in cands if not is_cached(c)]
  given_up = [c for c in todo if failures.get(c.item_id, 0) >= MAX_FAILURES]
  todo = [c for c in todo if failures.get(c.item_id, 0) < MAX_FAILURES]
  print(
    f"{len(cands)} videos with embedded text subs; {len(cands) - len(todo) - len(given_up)} cached, "
    f"{len(todo)} to do ({sum(c.size_gb for c in todo):.0f} GB), {len(given_up)} given up"
  )
  for c in given_up:
    print(f"  given up after {MAX_FAILURES} failures: {c.name} ({c.item_id})")

  if args.dry_run:
    for c in todo[: args.limit or None]:
      print(f"  would extract {c.name} ({c.size_gb:.1f} GB, tracks {list(c.text_indexes)})")
    return OK

  deadline = time.monotonic() + args.budget_min * 60
  done = failed = 0
  deferred = ""
  for c in todo:
    if args.limit and done + failed >= args.limit:
      break
    if time.monotonic() > deadline:
      deferred = "time budget spent"
      break
    if not args.ignore_playback:
      try:
        playing = someone_is_playing(_api("/Sessions?ActiveWithinSeconds=300", token))
      except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"jellyfin unreachable: {exc}", file=sys.stderr)
        return FATAL
      if playing:
        deferred = f"playback in progress ({playing})"
        break
    t0 = time.monotonic()
    try:
      _request(
        f"/Videos/{c.item_id}/{c.media_source_id}/Subtitles/{c.text_indexes[0]}/0/Stream.srt",
        token,
        timeout=REQUEST_TIMEOUT_S,
      )
      ok = is_cached(c)
      err = "" if ok else "request succeeded but cache is incomplete"
    except (urllib.error.URLError, OSError) as exc:
      ok, err = False, str(exc)
    dt = time.monotonic() - t0
    if ok:
      done += 1
      failures.pop(c.item_id, None)
      print(f"  extracted {c.name} ({c.size_gb:.1f} GB) in {dt:.0f}s")
    else:
      failed += 1
      failures[c.item_id] = failures.get(c.item_id, 0) + 1
      print(f"  FAILED {c.name} ({c.size_gb:.1f} GB) after {dt:.0f}s: {err}")
    save_failures(failures)

  left = len(todo) - done
  print(
    f"done: {done} extracted, {failed} failed, {left} left"
    + (f"; stopped: {deferred}" if deferred else "")
  )
  return PARTIAL if failed else OK


if __name__ == "__main__":
  sys.exit(main())
