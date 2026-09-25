#!/usr/bin/env python3
"""Check every external subtitle against the audio, and fix or replace the bad ones.

Why this exists (2026-09-25). Bazarr scores a subtitle by its *metadata* -- series,
season, episode, release name -- and nothing it does can see whether the file matches
the video. On Agatha Christie's Poirot that produced three kinds of silent failure,
every one of them with a healthy score and a "downloaded" history line:

* **drift**: a 25 fps PAL-DVD subtitle on the 24 fps Blu-ray. ffsubsync measured a
  0.960 / 1.036 framerate factor; the dialogue slides ~2 min out by the end credits;
* **offset**: a different cut, with every line a constant 20-60 s early or late;
* **wrong content**: S03E07 carried S03E08's subtitle (OpenSubtitles lists it as
  S03E07). Bazarr then "synchronised" it, which can only make it wronger.

This job measures instead of trusting. For each subtitle it cuts three short clips at
20 / 50 / 80 % of the runtime (seeking, not reading the file), transcribes them with
the local whisper container, and matches the spoken lines against the subtitle's
cues. Whisper is used purely as a measuring instrument here: nothing it transcribes is
ever written as a subtitle.

From the matched (subtitle time, audio time) pairs it fits ``audio = a * sub + b``:

* GOOD     -- every window within ``GOOD_TOLERANCE_S``: nothing to do;
* FIXABLE  -- a clean linear fit (drift and/or offset): the cue times are rewritten
              with that fit, the original is kept under ``backups/subtitle-audit/``,
              and the result is re-measured at four OTHER points (a fit always
              passes the points it came from). If those are not all aligned it is
              a different cut: the original is restored and, if Bazarr owns it,
              blacklisted and re-searched (UNFIXABLE);
* WRONG    -- plenty of speech, almost none of it in the subtitle: the subtitle is
              blacklisted in Bazarr, which deletes it and searches again, and can
              never pick that exact file again. A subtitle Bazarr did not download
              is moved into the backup dir instead;
* MOSTLY   -- right in most windows, off in one: a locally different cut. Reported,
              never retimed, because a straight line through it would make the
              good parts worse;
* UNSURE   -- too little speech to judge, or an inconsistent fit: left alone.

Subtitles in a language other than the one being spoken cannot be text-matched, so
they are checked by speech onsets (when each spoken line starts) instead. That is a
weaker test, so a failed onset check is reported but never acted on.

Every verdict is remembered against the file's size + mtime, so an unchanged file is
measured once, not every night. A replacement or a retime changes the file, so it
gets measured again.

Exit: 0 ok / 1 some items errored / 2 whisper or Bazarr unreachable.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

WHISPER_URL = os.environ.get("WHISPER_URL", "http://localhost:9000")
BAZARR_URL = os.environ.get("BAZARR_URL", "http://localhost:6767")
BAZARR_CONFIG = Path(os.environ.get("BAZARR_CONFIG", ".docker-config/bazarr/config/config.yaml"))
SHARE = Path(os.environ.get("SHARE_DIRECTORY", "/mnt/drive"))
# The container that runs ffmpeg, and where it sees SHARE. The host has no ffmpeg.
FFMPEG_CONTAINER = os.environ.get("SUBTITLE_AUDIT_FFMPEG_CONTAINER", "bazarr")
CONTAINER_SHARE = "/data"
BACKUP_DIR = SHARE / "backups" / "subtitle-audit"
STATE_FILE = Path("logs/cron-state/subtitle-audit.json")

VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v")
WINDOWS = (0.2, 0.5, 0.8)
# A retime is verified on DIFFERENT points than it was fitted on: a fit always passes
# the windows it came from, which is how a DVD-cut subtitle 5 s off at 37 min first
# "verified" at the 11 and 27 min points it was fitted to.
VERIFY_WINDOWS = (0.1, 0.35, 0.65, 0.9)
MIN_VERIFY_WINDOWS = 3
WINDOW_S = 60
MIN_WORDS = 3  # a spoken line shorter than this matches too easily to count
TEXT_MATCH_RATIO = 0.6
SEARCH_RADIUS_S = 400  # widest misplacement we try to recognise
GOOD_TOLERANCE_S = 0.75  # whisper's segment starts carry ~0.3 s of jitter
FIT_MAX_RESIDUAL_S = 0.5  # per WINDOW: the fit must explain every window, not the average
WRONG_MIN_LINES = 12
WRONG_MAX_MATCH = 0.12
MIN_PAIRS_PER_WINDOW = 3
RECHECK_UNSURE_DAYS = 30
MIN_LANG_VOTES = 2  # windows that must hear the subtitle's language before text-matching
# audio/sub time ratios a framerate conversion produces: PAL 25, film 24, NTSC 23.976.
FPS_RATIOS = tuple(x / y for x in (25, 24, 24000 / 1001) for y in (25, 24, 24000 / 1001) if x != y)
FPS_RATIO_TOLERANCE = 0.004  # measured 0.9626 for a 0.9600 PAL case; nearest other ratio is 0.999

# ISO 639-1 as Bazarr names files, mapped to what whisper reports.
LANG_ALIASES = {
  "eng": "en",
  "dut": "nl",
  "nld": "nl",
  "fre": "fr",
  "ger": "de",
  "por": "pt",
  "spa": "es",
}

OK, PARTIAL, FATAL = 0, 1, 2


# ---- subtitle parsing ------------------------------------------------------

TIMING = re.compile(
  r"(\d+):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass
class Cue:
  start: float
  end: float
  text: str  # normalised


def _secs(h: str, m: str, s: str, ms: str) -> float:
  return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def normalise(text: str) -> str:
  text = re.sub(r"<[^>]+>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)", " ", text.lower())
  return " ".join(re.sub(r"[^\w' ]", " ", text).split())


def parse_srt(content: str) -> list[Cue]:
  cues = []
  for block in re.split(r"\n\s*\n", content.replace("\r", "")):
    m = TIMING.search(block)
    if m:
      g = m.groups()
      cues.append(Cue(_secs(*g[:4]), _secs(*g[4:]), normalise(block[m.end() :])))
  return cues


def _fmt(t: float) -> str:
  t = max(t, 0.0)
  ms = round(t * 1000)
  h, ms = divmod(ms, 3_600_000)
  m, ms = divmod(ms, 60_000)
  s, ms = divmod(ms, 1000)
  return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def retime_srt(content: str, a: float, b: float) -> str:
  """Rewrite every cue time t as a*t + b, leaving everything else byte-identical."""

  def sub(m: re.Match) -> str:
    g = m.groups()
    return f"{_fmt(a * _secs(*g[:4]) + b)} --> {_fmt(a * _secs(*g[4:]) + b)}"

  return TIMING.sub(sub, content)


def sub_language(path: Path) -> str | None:
  """`Show - S01E01.en.hi.srt` -> 'en'; None when the name carries no language."""
  for part in reversed(path.name.split(".")[1:-1]):
    p = part.lower()
    if p in ("hi", "sdh", "cc", "forced") or p.isdigit():
      continue
    p = LANG_ALIASES.get(p, p)
    return p if len(p) == 2 and p.isalpha() else None
  return None


def video_for(sub: Path) -> Path | None:
  """The video this subtitle belongs to: the one whose full stem prefixes its name.

  Never a looser match. Matching on the text before the first dot paired every
  `the.wire.s01eNN….en.srt` with S01E01's video, and a WRONG verdict on that pairing
  would blacklist a correct subtitle. The longest stem wins, so `Show - S01E01.mkv`
  cannot claim `Show - S01E01 - Part 2.en.srt`.
  """
  videos = [
    c
    for c in sub.parent.iterdir()
    if c.suffix.lower() in VIDEO_EXTS and sub.name.startswith(c.stem + ".")
  ]
  return max(videos, key=lambda c: len(c.stem), default=None)


# ---- measuring -------------------------------------------------------------


@dataclass
class Window:
  at: float
  lines: int = 0
  pairs: list[tuple[float, float]] = field(default_factory=list)  # (sub_t, audio_t)
  onset_offset: float | None = None
  onset_hit: float = 0.0


def match_pairs(
  segments: list[dict], cues: list[Cue], at: float
) -> tuple[int, list[tuple[float, float]]]:
  """Pair each spoken line with the subtitle cue that says the same thing."""
  lines, pairs = 0, []
  for seg in segments:
    text = normalise(seg.get("text", ""))
    if len(text.split()) < MIN_WORDS:
      continue
    lines += 1
    audio_t = at + float(seg["start"])
    best, best_t = 0.0, 0.0
    for c in cues:
      if abs(c.start - audio_t) > SEARCH_RADIUS_S or not c.text:
        continue
      r = difflib.SequenceMatcher(None, text, c.text).ratio()
      if r > best:
        best, best_t = r, c.start
    if best >= TEXT_MATCH_RATIO:
      pairs.append((best_t, audio_t))
  return lines, pairs


def onset_offset(segments: list[dict], cues: list[Cue], at: float) -> tuple[float | None, float]:
  """Language-blind alignment: the shift that makes the most cue starts land on speech starts."""
  onsets = [at + float(s["start"]) for s in segments if normalise(s.get("text", ""))]
  if len(onsets) < 5:
    return None, 0.0
  starts = [
    c.start for c in cues if at - SEARCH_RADIUS_S <= c.start <= at + WINDOW_S + SEARCH_RADIUS_S
  ]
  hits = {}
  for step in range(-SEARCH_RADIUS_S * 10, SEARCH_RADIUS_S * 10 + 1):
    off = step / 10
    hits[step] = sum(1 for o in onsets if any(abs(s - off - o) <= 0.4 for s in starts))
  top = max(hits.values())
  if not top:
    return None, 0.0
  # Nearest top-scoring shift (regular dialogue rhythm aliases), then the centre of its
  # plateau: every shift within the +-0.4 s tolerance scores the same.
  step = min((k for k, v in hits.items() if v == top), key=abs)
  lo = hi = step
  while hits.get(lo - 1) == top:
    lo -= 1
  while hits.get(hi + 1) == top:
    hi += 1
  return (lo + hi) / 20, top / len(onsets)


def fit_line(pairs: list[tuple[float, float]]) -> tuple[float, float, float]:
  """Least-squares audio = a*sub + b over all pairs; returns (a, b, median |residual|)."""
  xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
  mx, my = statistics.fmean(xs), statistics.fmean(ys)
  var = sum((x - mx) ** 2 for x in xs)
  a = sum((x - mx) * (y - my) for x, y in pairs) / var if var else 1.0
  b = my - a * mx
  res = statistics.median(abs(y - (a * x + b)) for x, y in pairs)
  return a, b, res


INLIER_S = 1.5  # a pair further than this from its window's median is a false match


def _median_offset(w: Window) -> float:
  return statistics.median(a - s for s, a in w.pairs)


def inliers(w: Window) -> list[tuple[float, float]]:
  """Drop pairs that matched the wrong cue ("yes", "thank you" recur all episode long)."""
  if not w.pairs:
    return []
  m = _median_offset(w)
  return [(s, a) for s, a in w.pairs if abs((a - s) - m) <= INLIER_S]


@dataclass
class Verdict:
  kind: str  # GOOD / MOSTLY / FIXABLE / WRONG / UNSURE
  detail: str
  a: float = 1.0
  b: float = 0.0


def classify_text(windows: list[Window]) -> Verdict:
  lines = sum(w.lines for w in windows)
  matched = sum(len(w.pairs) for w in windows)
  if lines >= WRONG_MIN_LINES and matched / lines <= WRONG_MAX_MATCH:
    return Verdict("WRONG", f"{matched}/{lines} spoken lines found in the subtitle")
  usable = [w for w in windows if len(inliers(w)) >= MIN_PAIRS_PER_WINDOW]
  if not usable:
    return Verdict("UNSURE", f"only {matched}/{lines} lines matched")
  offsets = ", ".join(f"{w.at / 60:.0f}m {_median_offset(w):+.2f}s" for w in usable)
  if all(abs(_median_offset(w)) <= GOOD_TOLERANCE_S for w in usable):
    return Verdict("GOOD", f"{matched}/{lines} matched; {offsets}")
  meds = [_median_offset(w) for w in usable]
  if sum(abs(m) <= GOOD_TOLERANCE_S for m in meds) * 2 >= len(meds):
    # Right in most places, off in one: a locally different cut. A retime would trade
    # the good windows for the bad one, so this is reported and left alone.
    return Verdict("MOSTLY", f"aligned except in places ({offsets})")
  if len(usable) == 2 and abs(meds[0] - meds[1]) <= FIT_MAX_RESIDUAL_S:
    b = statistics.fmean(meds)
    return Verdict("FIXABLE", f"constant shift {b:+.2f}s ({offsets})", 1.0, b)
  if len(usable) < 2:
    return Verdict("UNSURE", f"misaligned, but only one window to measure from ({offsets})")
  a, b, _ = fit_line([p for w in usable for p in inliers(w)])
  if len(usable) < 3:
    # Two windows fit any line exactly, so a drift is only believed when its slope is
    # a real framerate conversion (Spanish Chest on its PAL-DVD subtitle: 0.9604).
    if not any(abs(a - r) <= FPS_RATIO_TOLERANCE for r in FPS_RATIOS):
      return Verdict("UNSURE", f"misaligned, too few windows to fit a drift (a={a:.4f}; {offsets})")
    return Verdict("FIXABLE", f"framerate audio = {a:.5f}*sub {b:+.2f}s ({offsets})", a, b)
  # A different CUT (an extra scene, a trimmed recap) is off in one place and right in
  # the others. A straight line through that would move the good parts too, so the fit
  # has to explain every window on its own.
  worst = max(abs(statistics.median(y - (a * x + b) for x, y in inliers(w))) for w in usable)
  if worst > FIT_MAX_RESIDUAL_S or not 0.9 <= a <= 1.1:
    return Verdict(
      "UNSURE",
      f"off in places, not a drift or shift (a={a:.4f}, worst window {worst:.2f}s; {offsets})",
    )
  return Verdict(
    "FIXABLE", f"audio = {a:.5f}*sub {b:+.2f}s (worst window {worst:.2f}s; {offsets})", a, b
  )


def verify_retime(windows: list[Window]) -> Verdict:
  """GOOD only if enough held-out windows were measurable and every one is aligned."""
  usable = [w for w in windows if len(inliers(w)) >= MIN_PAIRS_PER_WINDOW]
  offsets = ", ".join(f"{w.at / 60:.0f}m {_median_offset(w):+.2f}s" for w in usable)
  if len(usable) < MIN_VERIFY_WINDOWS:
    return Verdict("UNSURE", f"only {len(usable)} held-out windows measurable ({offsets})")
  if all(abs(_median_offset(w)) <= GOOD_TOLERANCE_S for w in usable):
    return Verdict("GOOD", f"held-out {offsets}")
  return Verdict("UNSURE", f"held-out windows still off ({offsets})")


def classify_onsets(windows: list[Window]) -> Verdict:
  good = [w for w in windows if w.onset_offset is not None and w.onset_hit >= 0.5]
  if len(good) < 2:
    return Verdict("UNSURE", "onset check inconclusive (subtitle language differs from the audio)")
  offs = ", ".join(f"{w.at / 60:.0f}m {w.onset_offset:+.1f}s ({w.onset_hit:.0%})" for w in good)
  if all(abs(w.onset_offset) <= 1.0 for w in good):
    return Verdict("GOOD", f"onsets aligned: {offs}")
  return Verdict("UNSURE", f"onsets misaligned, not acted on: {offs}")


# ---- side effects ------------------------------------------------------------


def to_container(p: Path) -> str:
  return CONTAINER_SHARE + str(p)[len(str(SHARE)) :]


def duration_s(video: Path) -> float:
  out = subprocess.check_output(
    [
      "docker",
      "exec",
      FFMPEG_CONTAINER,
      "ffprobe",
      "-v",
      "error",
      "-show_entries",
      "format=duration",
      "-of",
      "csv=p=0",
      to_container(video),
    ],
    timeout=120,
  )
  return float(out.strip())


def transcribe(video: Path, at: float, language: str | None) -> tuple[list[dict], str | None]:
  wav = subprocess.check_output(
    [
      "docker",
      "exec",
      FFMPEG_CONTAINER,
      "ffmpeg",
      "-v",
      "fatal",  # seeking into an mp3 track logs "Header missing" at error level, harmlessly
      "-ss",
      f"{at:.1f}",
      "-t",
      str(WINDOW_S),
      "-i",
      to_container(video),
      "-map",
      "0:a:0",
      "-ac",
      "1",
      "-ar",
      "16000",
      "-f",
      "wav",
      "-",
    ],
    timeout=900,  # a seek + 60 s decode; slow only when the disk is being hammered
  )
  boundary = uuid.uuid4().hex
  body = (
    f'--{boundary}\r\nContent-Disposition: form-data; name="audio_file"; filename="a.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
    + wav
    + f"\r\n--{boundary}--\r\n".encode()
  )
  q = {"task": "transcribe", "output": "json", "vad_filter": "true", "encode": "true"}
  if language:
    q["language"] = language
  req = urllib.request.Request(
    f"{WHISPER_URL}/asr?{urllib.parse.urlencode(q)}",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
  )
  with urllib.request.urlopen(req, timeout=900) as resp:
    out = json.load(resp)
  return out.get("segments") or [], out.get("language")


def measure(
  video: Path, sub_text: str, sub_lang: str | None, fractions: tuple[float, ...] = WINDOWS
) -> tuple[list[Window], str | None, bool]:
  """Measure each window; returns (windows, spoken language, text-matched?).

  Every window detects its own language. One wrong guess must not decide the whole
  file: whisper called Vincenzo's Korean Italian in one window and French in another,
  and a non-English show misheard as English would be transcribed as English
  gibberish, match nothing, and have a correct subtitle blacklisted as WRONG. So text
  matching needs MIN_LANG_VOTES windows agreeing with the subtitle's language, and
  anything else falls back to the onset check, which never acts.
  """
  cues = parse_srt(sub_text)
  dur = duration_s(video)
  heard = [(dur * f, *transcribe(video, dur * f, None)) for f in fractions]
  votes = Counter(lang for _, _, lang in heard if lang)
  spoken = votes.most_common(1)[0][0] if votes else None
  text_mode = bool(sub_lang) and votes.get(sub_lang, 0) >= MIN_LANG_VOTES
  windows = []
  for at, segs, lang in heard:
    w = Window(at)
    if text_mode and lang == sub_lang:
      w.lines, w.pairs = match_pairs(segs, cues, at)
    elif not text_mode:
      w.onset_offset, w.onset_hit = onset_offset(segs, cues, at)
    windows.append(w)  # a text-mode window heard as another language counts as silent
  return windows, spoken, text_mode


def latest_download(history: list[dict], lang: str) -> dict | None:
  """Bazarr's most recent download of `lang` for one video.

  Matched by language, not by path: history keeps the filename the subtitle had when it
  was downloaded (`…S03E07.1080p.BluRay.x264-YELLOWBIRD.en.srt`) and a Sonarr rename
  since then does not update it. Sync rows (action 5) carry no provider and are skipped.
  """
  rows = [
    h
    for h in history
    if h.get("provider")
    and h.get("subs_id")
    and isinstance(h.get("language"), dict)
    and h["language"].get("code2") == lang
  ]

  def when(h: dict) -> datetime:
    try:
      return datetime.strptime(h.get("parsed_timestamp") or "", "%m/%d/%y %H:%M:%S")
    except ValueError:
      return datetime.min

  return max(rows, key=when, default=None)


class Bazarr:
  def __init__(self) -> None:
    import yaml  # noqa: PLC0415 -- only this class needs it

    self.key = yaml.safe_load(BAZARR_CONFIG.read_text())["auth"]["apikey"]
    self.by_path: dict[str, tuple[str, dict]] = {}
    for m in self._get("/api/movies")["data"]:
      self.by_path[m["path"]] = ("movie", m)
    for s in self._get("/api/series")["data"]:
      eps = self._get(f"/api/episodes?seriesid[]={s['sonarrSeriesId']}")["data"]
      for e in eps:
        self.by_path[e["path"]] = ("episode", e)

  def _get(self, path: str) -> dict:
    req = urllib.request.Request(BAZARR_URL + path, headers={"X-API-KEY": self.key})
    with urllib.request.urlopen(req, timeout=120) as resp:
      return json.load(resp)

  def _post(self, path: str, form: dict) -> int:
    req = urllib.request.Request(
      BAZARR_URL + path,
      data=urllib.parse.urlencode(form).encode(),
      headers={"X-API-KEY": self.key},
      method="POST",
    )
    with urllib.request.urlopen(req, timeout=900) as resp:
      return resp.status

  def blacklist(self, video: Path, sub: Path) -> str:
    """Blacklist + delete + re-search. Returns a description, or '' if Bazarr does not own it."""
    hit = self.by_path.get(to_container(video))
    if not hit:
      return ""
    kind, item = hit
    csub = to_container(sub)
    if kind == "episode":
      hist = self._get(f"/api/episodes/history?episodeid={item['sonarrEpisodeId']}")["data"]
    else:
      hist = self._get(f"/api/movies/history?radarrid={item['radarrId']}")["data"]
    row = latest_download(hist, sub_language(sub) or "")
    if not row:
      return ""
    code = row["language"]["code2"]
    form = {
      "provider": row["provider"],
      "subs_id": row["subs_id"],
      "language": code,
      "subtitles_path": csub,
    }
    if kind == "episode":
      form |= {"seriesid": item["sonarrSeriesId"], "episodeid": item["sonarrEpisodeId"]}
      self._post("/api/episodes/blacklist", form)
    else:
      form |= {"radarrid": item["radarrId"]}
      self._post("/api/movies/blacklist", form)
    return f"blacklisted {row['provider']} {row['subs_id']} in Bazarr and re-searched"


def backup(sub: Path) -> Path:
  dest = BACKUP_DIR / sub.relative_to(SHARE)
  dest.parent.mkdir(parents=True, exist_ok=True)
  shutil.copy2(sub, dest)
  return dest


def load_state(path: Path = STATE_FILE) -> dict[str, dict]:
  try:
    return json.loads(path.read_text())
  except (OSError, ValueError):
    return {}


def save_state(state: dict, path: Path = STATE_FILE) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")


def fingerprint(p: Path) -> str:
  st = p.stat()
  return f"{st.st_size}:{int(st.st_mtime)}"


def needs_check(entry: dict | None, fp: str, now: float) -> bool:
  if not entry or entry.get("fp") != fp:
    return True
  if entry.get("verdict") == "UNSURE":
    return now - entry.get("at", 0) > RECHECK_UNSURE_DAYS * 86400
  # FIXABLE / WRONG are only ever stored by a report-only run: an --apply run acts on
  # them, which changes or removes the file. UNFIXABLE is sticky on purpose -- a retime
  # that did not verify would not verify tomorrow either, it would just re-read the file.
  return entry.get("verdict") in ("ERROR", "FIXABLE", "WRONG")


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument(
    "--apply", action="store_true", help="retime FIXABLE and replace WRONG (default: report only)"
  )
  ap.add_argument("--budget-min", type=float, default=50)
  ap.add_argument("--limit", type=int, default=0)
  ap.add_argument("--path", help="only subtitles under this directory")
  args = ap.parse_args()

  root = Path(args.path) if args.path else SHARE
  subs = sorted(
    p
    for d in ((root,) if args.path else (SHARE / "series", SHARE / "movies"))
    for p in d.rglob("*.srt")
  )
  state = load_state()
  now = time.time()
  todo = [s for s in subs if needs_check(state.get(str(s)), fingerprint(s), now)]
  print(f"{len(subs)} subtitles, {len(todo)} to measure")

  try:
    urllib.request.urlopen(f"{WHISPER_URL}/docs", timeout=10)
    bazarr = Bazarr() if args.apply else None
  except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
    print(f"whisper/bazarr unreachable: {exc}", file=sys.stderr)
    return FATAL

  deadline = time.monotonic() + args.budget_min * 60
  counts: dict[str, int] = {}
  errors = 0
  for n, sub in enumerate(todo):
    if (args.limit and n >= args.limit) or time.monotonic() > deadline:
      break
    rel = sub.relative_to(SHARE)
    video = video_for(sub)
    if not video:
      continue
    lang = sub_language(sub)
    try:
      text = sub.read_text(encoding="utf-8", errors="replace")
      windows, spoken, same = measure(video, text, lang)
      v = classify_text(windows) if same else classify_onsets(windows)
      action = ""
      if args.apply and same and v.kind == "FIXABLE":
        bak = backup(sub)
        sub.write_text(retime_srt(text, v.a, v.b), encoding="utf-8")
        held_out, _, _ = measure(video, sub.read_text(encoding="utf-8"), lang, VERIFY_WINDOWS)
        after = verify_retime(held_out)
        if after.kind == "GOOD":
          v, action = Verdict("GOOD", f"retimed ({v.detail}); now {after.detail}"), "retimed"
        else:
          # A different cut: the right words, but no single retime lines them up, and
          # seconds-off is no better than nothing. Hand it back to Bazarr like a wrong
          # one -- its original is in the backup dir. One Bazarr did not download is
          # left in place (and remembered), since nothing would replace it.
          shutil.copy2(bak, sub)
          v = Verdict("UNFIXABLE", v.detail)
          action = f"retime did not verify ({after.detail})"
          replaced = bazarr.blacklist(video, sub) if bazarr else ""
          action += f"; {replaced}" if replaced else "; original restored"
      elif args.apply and same and v.kind == "WRONG":
        backup(sub)
        action = bazarr.blacklist(video, sub) if bazarr else ""
        if not action:
          sub.unlink()
          action = f"not Bazarr's; moved to {BACKUP_DIR}"
      counts[v.kind] = counts.get(v.kind, 0) + 1
      print(
        f"  {v.kind:7s} {rel}  [{lang}/{spoken}] {v.detail}" + (f"  -> {action}" if action else "")
      )
      if sub.exists():
        state[str(sub)] = {"fp": fingerprint(sub), "verdict": v.kind, "detail": v.detail, "at": now}
      else:
        state.pop(str(sub), None)
    except (
      subprocess.SubprocessError,
      urllib.error.URLError,
      OSError,
      ValueError,
      KeyError,
    ) as exc:
      errors += 1
      state[str(sub)] = {
        "fp": fingerprint(sub),
        "verdict": "ERROR",
        "detail": str(exc)[:200],
        "at": now,
      }
      print(f"  ERROR   {rel}: {exc}")
    save_state(state)

  print("done: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f", errors {errors}")
  return PARTIAL if errors else OK


if __name__ == "__main__":
  sys.exit(main())
