import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "slskd_complete_sweep.py"
  spec = importlib.util.spec_from_file_location("slskd_complete_sweep", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


sweep = _load_module()

CONTAINER_SLSKD = "/downloads/complete/slskd"
CONTAINER_MUSIC = "/music"


def test_drop_to_folder_rel_simple():
  got = sweep._drop_to_folder_rel(
    "/downloads/complete/slskd/(1979) - Mirrors/01 - Track.mp3", CONTAINER_SLSKD
  )
  assert got == ("(1979) - Mirrors", "01 - Track.mp3")


def test_drop_to_folder_rel_multidisc():
  got = sweep._drop_to_folder_rel(
    "/downloads/complete/slskd/Big Album/CD1/03 - Song.flac", CONTAINER_SLSKD
  )
  assert got == ("Big Album", "CD1/03 - Song.flac")


def test_drop_to_folder_rel_outside_slskd_root():
  # qBittorrent / manual drops must be ignored entirely
  assert sweep._drop_to_folder_rel("/downloads/complete/manual/x/a.mp3", CONTAINER_SLSKD) is None


def test_drop_to_folder_rel_bare_file_no_folder():
  # a drop directly in the root (no album folder) is not reapable-by-folder
  assert sweep._drop_to_folder_rel("/downloads/complete/slskd/loose.mp3", CONTAINER_SLSKD) is None


def test_imported_host_path_translates_music_root(tmp_path):
  got = sweep.imported_host_path("/music/Artist/Album/01.mp3", CONTAINER_MUSIC, tmp_path)
  assert got == tmp_path / "Artist/Album/01.mp3"


def test_imported_host_path_unmapped_prefix(tmp_path):
  assert sweep.imported_host_path("/elsewhere/x.mp3", CONTAINER_MUSIC, tmp_path) is None


def _make_download(slskd_root: Path, folder: str, files: list[str]) -> Path:
  d = slskd_root / folder
  d.mkdir(parents=True)
  for f in files:
    p = d / f
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"audio")
  return d


def test_scan_full_match_reapable(tmp_path):
  slskd_root = tmp_path / "slskd"
  music = tmp_path / "music"
  d = _make_download(slskd_root, "Album", ["01 - a.mp3", "02 - b.mp3"])
  # both files imported AND present on disk under /music
  for rel in ("Artist/Album/01 - a.mp3", "Artist/Album/02 - b.mp3"):
    (music / rel).parent.mkdir(parents=True, exist_ok=True)
    (music / rel).write_bytes(b"x")
  import_map = {
    "Album": {
      "01 - a.mp3": "/music/Artist/Album/01 - a.mp3",
      "02 - b.mp3": "/music/Artist/Album/02 - b.mp3",
    }
  }
  rep = sweep.scan_slskd_dir(d, import_map, CONTAINER_MUSIC, music)
  assert rep is not None
  assert rep.audio_files == 2
  assert rep.matched == 2
  assert rep.ratio == 1.0


def test_scan_imported_but_target_deleted_does_not_count(tmp_path):
  # Lidarr recorded the import, but the file was later removed/upgraded out.
  # If the target no longer exists we must NOT treat it as still-imported.
  slskd_root = tmp_path / "slskd"
  music = tmp_path / "music"
  d = _make_download(slskd_root, "Album", ["01 - a.mp3"])
  import_map = {"Album": {"01 - a.mp3": "/music/Artist/Album/01 - a.mp3"}}
  rep = sweep.scan_slskd_dir(d, import_map, CONTAINER_MUSIC, music)
  assert rep is not None
  assert rep.matched == 0
  assert rep.ratio == 0.0


def test_scan_partial_match_ratio(tmp_path):
  slskd_root = tmp_path / "slskd"
  music = tmp_path / "music"
  d = _make_download(slskd_root, "Album", ["01.mp3", "02.mp3", "03.mp3"])
  (music / "Artist/Album/01.mp3").parent.mkdir(parents=True, exist_ok=True)
  (music / "Artist/Album/01.mp3").write_bytes(b"x")
  import_map = {"Album": {"01.mp3": "/music/Artist/Album/01.mp3"}}
  rep = sweep.scan_slskd_dir(d, import_map, CONTAINER_MUSIC, music)
  assert rep is not None
  assert rep.matched == 1
  assert rep.audio_files == 3
  assert abs(rep.ratio - 1 / 3) < 1e-9


def test_scan_no_history_for_folder_zero_match(tmp_path):
  slskd_root = tmp_path / "slskd"
  music = tmp_path / "music"
  d = _make_download(slskd_root, "Unmatched", ["01.mp3"])
  rep = sweep.scan_slskd_dir(d, {}, CONTAINER_MUSIC, music)
  assert rep is not None
  assert rep.matched == 0


def test_scan_skips_non_audio(tmp_path):
  slskd_root = tmp_path / "slskd"
  music = tmp_path / "music"
  d = _make_download(slskd_root, "Album", ["cover.jpg", "notes.txt"])
  rep = sweep.scan_slskd_dir(d, {}, CONTAINER_MUSIC, music)
  assert rep is None  # no audio files -> not a candidate
