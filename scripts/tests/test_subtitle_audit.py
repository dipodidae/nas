import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
  root = Path(__file__).resolve().parents[2]
  script_path = root / "scripts" / "subtitle_audit.py"
  spec = importlib.util.spec_from_file_location("subtitle_audit", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


sa = _load_module()

SRT = """1
00:00:01,000 --> 00:00:03,500
<i>Hello there,</i> Hastings.

2
00:01:00,250 --> 00:01:02,000
[DOOR CREAKS]
Mon ami, the little grey cells!
"""

LINES = [f"line number {i} has several distinct words {i * 7}" for i in range(40)]


def _window(at, lines_at, offset, scale=1.0, extra_lines=0):
  """A window where each spoken line is found in the subtitle at (t - offset) / scale."""
  w = sa.Window(at)
  w.lines = len(lines_at) + extra_lines
  w.pairs = [((t - offset) / scale, t) for t in lines_at]
  return w


# ---- parsing / writing ----------------------------------------------------


def test_parse_srt_normalises_tags_and_hi_annotations():
  cues = sa.parse_srt(SRT)
  assert [(c.start, c.end) for c in cues] == [(1.0, 3.5), (60.25, 62.0)]
  assert cues[0].text == "hello there hastings"
  assert cues[1].text == "mon ami the little grey cells"


def test_retime_srt_applies_linear_map_and_keeps_text():
  out = sa.retime_srt(SRT, 1.04, -0.5)
  assert "00:00:00,540 --> 00:00:03,140" in out
  assert "00:01:02,160 --> 00:01:03,980" in out
  assert "<i>Hello there,</i> Hastings." in out and "[DOOR CREAKS]" in out


def test_retime_never_goes_negative():
  assert "00:00:00,000 --> 00:00:00,500" in sa.retime_srt(SRT, 1.0, -3.0)


@pytest.mark.parametrize(
  ("name", "lang"),
  [
    ("Show - S01E01 - X Bluray-1080p.en.srt", "en"),
    ("Show - S01E01 - X Bluray-1080p.en.hi.srt", "en"),
    ("Show - S01E01.nl.forced.srt", "nl"),
    ("The Office - S01E01 - Downsize WEBDL-1080p.eng.0.srt", "en"),
    ("Movie.srt", None),
  ],
)
def test_sub_language(name, lang):
  assert sa.sub_language(Path(name)) == lang


def test_video_for_handles_dotted_stems(tmp_path):
  v = tmp_path / "Mr. Robot - S01E01 - eps1.0 WEBDL.mkv"
  v.write_text("")
  (tmp_path / "Mr. Robot - S01E02 - other.mkv").write_text("")
  assert sa.video_for(tmp_path / "Mr. Robot - S01E01 - eps1.0 WEBDL.en.srt") == v


# ---- matching ---------------------------------------------------------------


def test_match_pairs_ignores_short_lines_and_finds_offset_cue():
  cues = [sa.Cue(100.0, 102.0, "you will send the account to my address")]
  segs = [
    {"start": 5.0, "text": "Yes."},
    {"start": 12.0, "text": "You will send the account to my address."},
  ]
  lines, pairs = sa.match_pairs(segs, cues, at=100.0)
  assert lines == 1
  assert pairs == [(100.0, 112.0)]


def test_onset_offset_recovers_constant_shift():
  starts = [sum(3.0 + (i * i * 7919 % 13) for i in range(k)) for k in range(30)]  # aperiodic
  cues = [sa.Cue(s, s + 1, "x") for s in starts]
  segs = [
    {"start": s - 100.0 + 7.5, "text": "words here now"} for s in starts if 100 <= s + 7.5 <= 160
  ]
  off, hit = sa.onset_offset(segs, cues, at=100.0)
  assert off == pytest.approx(-7.5, abs=0.11)
  assert hit == 1.0


# ---- classification ------------------------------------------------------


def test_good_when_every_window_is_aligned():
  ws = [_window(at, [at + i * 5 for i in range(8)], 0.2) for at in (600, 1500, 2400)]
  assert sa.classify_text(ws).kind == "GOOD"


def test_outlier_pairs_do_not_break_a_good_subtitle():
  ws = [_window(at, [at + i * 5 for i in range(8)], 0.1) for at in (600, 1500, 2400)]
  ws[0].pairs.append((ws[0].at + 200.0, ws[0].at + 3.0))  # "yes" matched a cue 200 s away
  assert sa.classify_text(ws).kind == "GOOD"


def test_pal_drift_is_fixable_with_the_right_factor():
  # A 25 fps subtitle on a 24 fps video: audio time = sub time * 25/24.
  ws = [_window(at, [at + i * 5 for i in range(8)], 0.0, scale=25 / 24) for at in (600, 1500, 2400)]
  v = sa.classify_text(ws)
  assert v.kind == "FIXABLE"
  assert v.a == pytest.approx(25 / 24, rel=1e-6)
  assert v.b == pytest.approx(0.0, abs=1e-6)


def test_constant_offset_is_fixable():
  ws = [_window(at, [at + i * 5 for i in range(8)], -25.6) for at in (600, 1500, 2400)]
  v = sa.classify_text(ws)
  assert (v.kind, round(v.a, 6), round(v.b, 2)) == ("FIXABLE", 1.0, -25.6)


def test_wrong_episode_when_speech_is_not_in_the_subtitle():
  ws = [_window(at, [at + 1], 0.0, extra_lines=15) for at in (600, 1500, 2400)]
  assert sa.classify_text(ws).kind == "WRONG"


def test_silence_is_unsure_not_wrong():
  ws = [_window(at, [], 0.0, extra_lines=2) for at in (600, 1500, 2400)]
  assert sa.classify_text(ws).kind == "UNSURE"


def test_single_misaligned_window_is_unsure():
  ws = [_window(600, [600 + i * 5 for i in range(8)], 30.0), _window(1500, [], 0, extra_lines=1)]
  assert sa.classify_text(ws).kind in ("UNSURE", "MOSTLY")


def test_onset_verdicts_never_act():
  w = sa.Window(600)
  w.onset_offset, w.onset_hit = 30.0, 0.9
  assert sa.classify_onsets([w, w]).kind == "UNSURE"
  w.onset_offset = 0.3
  assert sa.classify_onsets([w, w]).kind == "GOOD"


# ---- memory ---------------------------------------------------------------


def test_needs_check_remembers_verdicts_per_fingerprint():
  now = 1_000_000_000.0
  assert sa.needs_check(None, "1:2", now)
  assert not sa.needs_check({"fp": "1:2", "verdict": "GOOD", "at": now}, "1:2", now)
  assert sa.needs_check({"fp": "1:2", "verdict": "GOOD", "at": now}, "9:9", now)
  assert not sa.needs_check({"fp": "1:2", "verdict": "UNSURE", "at": now - 86400}, "1:2", now)
  assert sa.needs_check({"fp": "1:2", "verdict": "UNSURE", "at": now - 31 * 86400}, "1:2", now)
  assert sa.needs_check({"fp": "1:2", "verdict": "ERROR", "at": now}, "1:2", now)


def test_a_locally_different_cut_is_left_alone():
  # Five Little Pigs: -1.3 s at 19 min, spot on after. Not a drift; a line would move the good parts.
  ws = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((1140, -1.3), (2940, 0.1), (4680, 0.0))
  ]
  assert sa.classify_text(ws).kind == "MOSTLY"


def test_two_windows_agreeing_on_a_shift_is_fixable():
  ws = [_window(at, [at + i * 5 for i in range(8)], 8.98) for at in (600, 2400)]
  v = sa.classify_text(ws)
  assert (v.kind, v.a, round(v.b, 2)) == ("FIXABLE", 1.0, 8.98)


def test_off_in_every_window_but_not_linearly_is_unsure():
  ws = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((600, 5.0), (1500, -9.0), (2400, 6.0))
  ]
  assert sa.classify_text(ws).kind == "UNSURE"


def test_actionable_verdicts_are_remeasured_but_unfixable_is_sticky():
  now = 1_000_000_000.0
  for kind in ("FIXABLE", "WRONG"):
    assert sa.needs_check({"fp": "1:2", "verdict": kind, "at": now}, "1:2", now)
  assert not sa.needs_check({"fp": "1:2", "verdict": "UNFIXABLE", "at": now}, "1:2", now)
  assert not sa.needs_check({"fp": "1:2", "verdict": "MOSTLY", "at": now}, "1:2", now)


def test_latest_download_matches_language_not_the_stale_path():
  en = {"code2": "en"}
  hist = [
    {
      "action": 5,
      "provider": None,
      "subs_id": None,
      "language": en,
      "parsed_timestamp": "09/25/26 11:41:34",
    },
    {
      "action": 3,
      "provider": "subf2m",
      "subs_id": "b",
      "language": en,
      "parsed_timestamp": "05/28/26 00:36:17",
    },
    {
      "action": 1,
      "provider": "subdl",
      "subs_id": "a",
      "language": en,
      "parsed_timestamp": "12/01/25 10:00:00",
    },
    {
      "action": 1,
      "provider": "opensubtitlescom",
      "subs_id": "n",
      "language": {"code2": "nl"},
      "parsed_timestamp": "09/25/26 12:00:00",
    },
  ]
  assert sa.latest_download(hist, "en")["subs_id"] == "b"
  assert sa.latest_download(hist, "fr") is None


def test_two_windows_fix_a_drift_only_on_a_real_framerate_ratio():
  pal = [_window(at, [at + i * 5 for i in range(8)], 3.0, scale=24 / 25) for at in (660, 1620)]
  v = sa.classify_text(pal)
  assert v.kind == "FIXABLE" and v.a == pytest.approx(24 / 25, rel=1e-6)
  odd = [_window(at, [at + i * 5 for i in range(8)], 3.0, scale=0.93) for at in (660, 1620)]
  assert sa.classify_text(odd).kind == "UNSURE"


def test_retime_verification_needs_held_out_windows_all_aligned():
  ok = [_window(at, [at + i * 5 for i in range(8)], 0.1) for at in (300, 1050, 1950, 2700)]
  assert sa.verify_retime(ok).kind == "GOOD"
  # Spanish Chest after its PAL retime: right at 27 min, 5 s off at 37 min.
  cut = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((318, -1.44), (960, 2.15), (1602, 0.14), (2244, 5.01))
  ]
  assert sa.verify_retime(cut).kind == "UNSURE"
  too_few = [_window(at, [at + i * 5 for i in range(8)], 0.0) for at in (300, 1050)]
  assert sa.verify_retime(too_few).kind == "UNSURE"


def test_video_for_never_pairs_scene_names_by_their_first_dot(tmp_path):
  for n in (1, 2, 3):
    (tmp_path / f"the.wire.s01e0{n}.1080p.bluray.x264-rovers.mkv").write_text("")
  sub = tmp_path / "the.wire.s01e02.1080p.bluray.x264-rovers.en.srt"
  assert sa.video_for(sub).name == "the.wire.s01e02.1080p.bluray.x264-rovers.mkv"
  assert sa.video_for(tmp_path / "the.wire.s01e09.en.srt") is None


def test_video_for_prefers_the_longest_stem(tmp_path):
  (tmp_path / "Show - S01E01.mkv").write_text("")
  (tmp_path / "Show - S01E01.Part 2.mkv").write_text("")
  assert sa.video_for(tmp_path / "Show - S01E01.Part 2.en.srt").name == "Show - S01E01.Part 2.mkv"
