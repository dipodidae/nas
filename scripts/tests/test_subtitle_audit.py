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


def test_off_in_every_window_but_not_linearly_is_badsync():
  ws = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((600, 5.0), (1500, -9.0), (2400, 6.0))
  ]
  assert sa.classify_text(ws).kind == "BADSYNC"


def test_actionable_verdicts_are_remeasured_but_unfixable_is_sticky():
  now = 1_000_000_000.0
  for kind in ("FIXABLE", "WRONG"):
    assert sa.needs_check({"fp": "1:2", "verdict": kind, "at": now}, "1:2", now)
  assert not sa.needs_check({"fp": "1:2", "verdict": "UNFIXABLE", "at": now}, "1:2", now)
  assert not sa.needs_check({"fp": "1:2", "verdict": "MOSTLY", "at": now}, "1:2", now)


def test_latest_download_matches_language_and_skips_sync_rows():
  rows = [
    {"provider": None, "subs_id": None, "language": "en", "timestamp": "2026-09-25 11:41:34"},
    {"provider": "subf2m", "subs_id": "b", "language": "en:hi", "timestamp": "2026-05-28 00:36:17"},
    {"provider": "subdl", "subs_id": "a", "language": "en", "timestamp": "2025-12-01 10:00:00"},
    {
      "provider": "opensubtitlescom",
      "subs_id": "n",
      "language": "nl",
      "timestamp": "2026-09-25 12:00:00",
    },
  ]
  assert sa.latest_download(rows, "en")["subs_id"] == "b"
  assert sa.latest_download(rows, "fr") is None


def test_history_rows_reads_the_db_read_only(tmp_path):
  import sqlite3

  db = tmp_path / "bazarr.db"
  con = sqlite3.connect(db)
  con.execute(
    "CREATE TABLE table_history (sonarrEpisodeId INT, provider TEXT, subs_id TEXT, language TEXT, timestamp TEXT)"
  )
  con.execute(
    "INSERT INTO table_history VALUES (30, 'subdl', 'x-2329921.zip/Q', 'en', '2026-09-25 13:07:23')"
  )
  con.commit()
  con.close()
  assert sa.history_rows("episode", 30, db) == [
    {
      "provider": "subdl",
      "subs_id": "x-2329921.zip/Q",
      "language": "en",
      "timestamp": "2026-09-25 13:07:23",
    }
  ]


def test_two_windows_fix_a_drift_only_on_a_real_framerate_ratio():
  pal = [_window(at, [at + i * 5 for i in range(8)], 3.0, scale=24 / 25) for at in (660, 1620)]
  v = sa.classify_text(pal)
  assert v.kind == "FIXABLE" and v.a == pytest.approx(24 / 25, rel=1e-6)
  odd = [_window(at, [at + i * 5 for i in range(8)], 3.0, scale=0.93) for at in (660, 1620)]
  assert sa.classify_text(odd).kind == "BADSYNC"  # no fps ratio, and off everywhere


def test_retime_verification_needs_held_out_windows_all_aligned():
  ok = [_window(at, [at + i * 5 for i in range(8)], 0.1) for at in (300, 1050, 1950, 2700)]
  assert sa.verify_retime(ok).kind == "GOOD"
  # Spanish Chest after its PAL retime: 5 s off at 37 min -- a different cut, reverted.
  cut = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((318, -1.44), (960, 2.15), (1602, 0.14), (2244, 5.01))
  ]
  assert sa.verify_retime(cut).kind == "UNSURE"
  one = [_window(300, [300 + i * 5 for i in range(8)], 0.0)]
  assert sa.verify_retime(one).kind == "UNSURE"


def test_a_retime_right_almost_everywhere_is_kept_as_mostly():
  # Wasps' Nest after its PAL retime.
  wasps = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((300, 0.02), (1140, 0.05), (2040, -1.69), (2880, -0.24))
  ]
  assert sa.verify_retime(wasps).kind == "MOSTLY"
  two = [
    _window(at, [at + i * 5 for i in range(8)], off) for at, off in ((300, 0.07), (2880, -1.66))
  ]
  assert sa.verify_retime(two).kind == "MOSTLY"


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


# ---- replacement loop -----------------------------------------------------


def test_pack_key_links_a_pack_across_providers_and_reuploads():
  subf2m = "https://subf2m.co/subtitles/agatha-christies-poirot-third-season/english/2329921"
  subdl = "agatha-christies-poirot-third-season_english-2329921.zip/Csw5YxSHnR"
  assert sa.pack_key(subf2m) == sa.pack_key(subdl) == "2329921"
  assert sa.pack_key("abc/def") == "abc"


def _cand(provider, score, url, hi="False", rel=""):
  return {
    "provider": provider,
    "score": score,
    "url": url,
    "hearing_impaired": hi,
    "release_info": [rel],
    "subtitle": url,
  }


def test_choose_candidate_skips_bad_packs_and_tried_and_ignores_min_score():
  cands = [
    _cand("subdl", 93, "https://dl.subdl.com/x/2329921-a.zip"),
    _cand("gestdown", 86, "https://api.gestdown.info/subtitles/download/ebd6"),
    _cand("gestdown", 86, "https://api.gestdown.info/subtitles/download/7b54", hi="True"),
    _cand("opensubtitlescom", 80, "https://os/1"),
  ]
  pick = sa.choose_candidate(cands, [], ["2329921"])
  assert pick["url"].endswith("ebd6")  # best non-HI outside the bad pack, though below 90
  tried = [sa._cand_id(pick)]
  assert sa.choose_candidate(cands, tried, ["2329921"])["url"] == "https://os/1"  # non-HI before HI
  tried.append("opensubtitlescom|https://os/1")
  assert sa.choose_candidate(cands, tried, ["2329921"])["hearing_impaired"] == "True"
  tried.append(sa._cand_id(cands[2]))
  assert sa.choose_candidate(cands, tried, ["2329921"]) is None


def test_track_replacement_accumulates_bad_packs(tmp_path):
  state = {}
  sub, video = tmp_path / "a.en.srt", tmp_path / "a.mkv"
  sa.track_replacement(state, sub, video, "en", "x/english/2329921", 100.0)
  sa.track_replacement(state, sub, video, "en", "pack-2329921.zip/Q", 200.0)
  sa.track_replacement(state, sub, video, "en", "other-555555.zip/R", 300.0)
  rep = state[sa.REPLACING][str(sub)]
  assert rep["bad_packs"] == ["2329921", "555555"] and rep["last"] == 300.0


def test_next_step_waits_for_bazarr_then_requests_then_rests_a_day():
  rep = {"attempts": 0, "last": 1000.0}
  assert sa.next_step(rep, True, 99999.0) == "idle"
  assert sa.next_step(rep, False, 1000.0 + 60) == "wait"
  assert sa.next_step(rep, False, 1000.0 + sa.REQUEST_WAIT_S + 1) == "request"
  done = {"attempts": sa.MAX_ATTEMPTS, "last": 0.0, "exhausted_at": 5000.0}
  assert sa.next_step(done, False, 5000.0 + 3600) == "exhausted"
  assert sa.next_step(done, False, 5000.0 + sa.RETRY_EXHAUSTED_S + 1) == "request"


def test_backup_keeps_every_version(tmp_path, monkeypatch):
  share = tmp_path / "share"
  sub = share / "series" / "a.en.srt"
  sub.parent.mkdir(parents=True)
  monkeypatch.setattr(sa, "SHARE", share)
  sub.write_text("first")
  b1 = sa.backup(sub, tmp_path / "bak")
  sub.write_text("second")
  b2 = sa.backup(sub, tmp_path / "bak")
  assert b1 != b2 and b1.read_text() == "first" and b2.read_text() == "second"


def test_neighbours_are_same_season_nearest_first(tmp_path):
  for n in (1, 2, 3, 4, 5, 6):
    (tmp_path / f"Poirot - S03E0{n} - x.mkv").write_text("")
  (tmp_path / "Poirot - S04E04 - y.mkv").write_text("")
  names = [p.name[9:15] for p in sa.neighbours(tmp_path / "Poirot - S03E04 - x.mkv")]
  assert names == ["S03E05", "S03E03", "S03E06", "S03E02"]


def test_rehome_moves_a_wrong_subtitle_to_the_episode_it_belongs_to(tmp_path, monkeypatch):
  v7, v8 = tmp_path / "P - S03E07 - a.mkv", tmp_path / "P - S03E08 - b.mkv"
  for v in (v7, v8):
    v.write_text("")
  monkeypatch.setattr(sa, "SHARE", tmp_path)
  monkeypatch.setattr(sa, "BACKUP_DIR", tmp_path / "bak")
  monkeypatch.setattr(sa, "belongs_to", lambda video, cues: video == v8)
  state = {}
  msg = sa.rehome(tmp_path / "P - S03E07 - a.en.srt", v7, SRT, "en", state)
  assert msg == "it belongs to S03E08: moved there"
  assert (tmp_path / "P - S03E08 - b.en.srt").read_text() == SRT


def test_rehome_never_overwrites_a_verified_subtitle(tmp_path, monkeypatch):
  v7, v8 = tmp_path / "P - S03E07 - a.mkv", tmp_path / "P - S03E08 - b.mkv"
  for v in (v7, v8):
    v.write_text("")
  good = tmp_path / "P - S03E08 - b.en.srt"
  good.write_text("verified")
  monkeypatch.setattr(sa, "belongs_to", lambda video, cues: True)
  state = {str(good): {"verdict": "GOOD"}}
  assert sa.rehome(tmp_path / "P - S03E07 - a.en.srt", v7, SRT, "en", state) == ""
  assert good.read_text() == "verified"


def test_minutes_off_everywhere_is_badsync_not_unsure():
  # Plymouth Express: the right words at -88 / -141 / -188 s -- not on one line.
  plymouth = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((660, -88.0), (1560, -141.0), (2520, -120.0))
  ]
  assert sa.classify_text(plymouth).kind == "BADSYNC"
  # Marsdon Manor: two windows, a slope that is no framerate ratio.
  jumpy = [
    _window(at, [at + i * 5 for i in range(8)], off) for at, off in ((600, 77.5), (1600, 103.2))
  ]
  assert sa.classify_text(jumpy).kind == "BADSYNC"
  mild = [
    _window(at, [at + i * 5 for i in range(8)], off)
    for at, off in ((600, 1.0), (1500, -1.5), (2400, 1.2))
  ]
  assert sa.classify_text(mild).kind == "UNSURE"
