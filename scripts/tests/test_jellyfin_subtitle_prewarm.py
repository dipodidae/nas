import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  script_path = root / "scripts" / "jellyfin_subtitle_prewarm.py"
  spec = importlib.util.spec_from_file_location("jellyfin_subtitle_prewarm", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


jsp = _load_module()

ID = "12f7c5ef689a19755973f92f8688d114"


def _item(streams, **kw):
  return {
    "Id": ID,
    "Name": "Ep",
    "MediaSources": [{"Id": ID, "Size": 4e9, "MediaStreams": streams}],
    **kw,
  }


def _sub(index, codec, external=False):
  return {"Type": "Subtitle", "Index": index, "Codec": codec, "IsExternal": external}


# ---- candidates_from_items ----------------------------------------------


def test_only_embedded_text_tracks_count():
  items = [
    _item(
      [
        {"Type": "Video", "Index": 0},
        _sub(2, "subrip"),
        _sub(3, "PGSSUB"),
        _sub(4, "ass"),
        _sub(9, "subrip", external=True),
      ]
    )
  ]
  (c,) = jsp.candidates_from_items(items)
  assert c.text_indexes == (2, 4)
  assert c.size_gb == 4.0


def test_image_only_or_external_only_items_are_skipped():
  items = [
    _item([_sub(2, "PGSSUB")]),
    _item([_sub(0, "subrip", external=True)]),
    {"Id": ID, "Name": "x"},
  ]
  assert jsp.candidates_from_items(items) == []


def test_series_name_is_prefixed():
  (c,) = jsp.candidates_from_items([_item([_sub(2, "subrip")], SeriesName="Mayday")])
  assert c.name == "Mayday - Ep"


# ---- cache detection ----------------------------------------------------


def test_cache_dir_uses_dashed_guid():
  assert jsp.cache_dir(ID, Path("/c")) == Path("/c/12/12f7c5ef-689a-1975-5973-f92f8688d114")


def test_is_cached_needs_every_text_track(tmp_path):
  (c,) = jsp.candidates_from_items([_item([_sub(2, "subrip"), _sub(3, "subrip")])])
  assert not jsp.is_cached(c, tmp_path)
  d = jsp.cache_dir(ID, tmp_path)
  d.mkdir(parents=True)
  (d / "2.srt").write_text("x")
  (d / "3.srt.meta").write_text("x")  # a .meta alone is not an extracted track
  assert not jsp.is_cached(c, tmp_path)
  (d / "3.srt").write_text("x")
  assert jsp.is_cached(c, tmp_path)


# ---- playback guard -----------------------------------------------------


def test_someone_is_playing_ignores_paused_and_idle_sessions():
  assert jsp.someone_is_playing([{"UserName": "tom"}]) is None
  paused = {"UserName": "tom", "NowPlayingItem": {"Name": "A"}, "PlayState": {"IsPaused": True}}
  assert jsp.someone_is_playing([paused]) is None
  playing = {"UserName": "tom", "NowPlayingItem": {"Name": "A"}, "PlayState": {"IsPaused": False}}
  assert jsp.someone_is_playing([paused, playing]) == "tom: A"


# ---- failure memory -----------------------------------------------------


def test_failures_round_trip_and_tolerate_garbage(tmp_path):
  p = tmp_path / "s" / "f.json"
  assert jsp.load_failures(p) == {}
  jsp.save_failures({ID: 2}, p)
  assert jsp.load_failures(p) == {ID: 2}
  p.write_text("not json")
  assert jsp.load_failures(p) == {}
