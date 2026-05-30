import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "lidarr_monitor_sweep.py"
  spec = importlib.util.spec_from_file_location("lidarr_monitor_sweep", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


sweep = _load_module()


def test_monitored_artist_zero_monitored_albums_is_broken():
  artists = [{"id": 1, "artistName": "A", "monitored": True}]
  albums = [{"id": 10, "artistId": 1, "monitored": False},
            {"id": 11, "artistId": 1, "monitored": False}]
  broken = sweep.find_broken_artists(artists, albums)
  assert len(broken) == 1
  assert broken[0].artist_id == 1
  assert sorted(broken[0].album_ids) == [10, 11]


def test_artist_with_a_monitored_album_is_not_broken():
  artists = [{"id": 1, "artistName": "A", "monitored": True}]
  albums = [{"id": 10, "artistId": 1, "monitored": True},
            {"id": 11, "artistId": 1, "monitored": False}]
  assert sweep.find_broken_artists(artists, albums) == []


def test_unmonitored_artist_is_left_alone():
  # deliberate "I don't want this" — never force-monitor
  artists = [{"id": 1, "artistName": "A", "monitored": False}]
  albums = [{"id": 10, "artistId": 1, "monitored": False}]
  assert sweep.find_broken_artists(artists, albums) == []


def test_artist_with_no_albums_is_skipped():
  # discography not pulled yet (or none) — nothing to monitor
  artists = [{"id": 1, "artistName": "A", "monitored": True}]
  assert sweep.find_broken_artists(artists, []) == []


def test_multiple_artists_mixed():
  artists = [
    {"id": 1, "artistName": "broken", "monitored": True},
    {"id": 2, "artistName": "ok", "monitored": True},
    {"id": 3, "artistName": "unmonitored", "monitored": False},
  ]
  albums = [
    {"id": 10, "artistId": 1, "monitored": False},
    {"id": 20, "artistId": 2, "monitored": True},
    {"id": 30, "artistId": 3, "monitored": False},
  ]
  broken = sweep.find_broken_artists(artists, albums)
  assert [b.artist_id for b in broken] == [1]
