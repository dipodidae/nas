import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "process_soulseek_imports.py"
  spec = importlib.util.spec_from_file_location("process_soulseek_imports", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


psi = _load_module()


def _file_info(release_id, track_count):
  return {
    "albumReleaseId": release_id,
    "album": {"releases": [{"id": release_id, "trackCount": track_count}]},
  }


def test_release_track_count_matches_selected_release():
  fi = {
    "albumReleaseId": 27195,
    "album": {
      "releases": [
        {"id": 27193, "trackCount": 13},
        {"id": 27195, "trackCount": 9},
      ]
    },
  }
  assert psi._release_track_count(fi) == 9


def test_release_track_count_unknown_release_returns_zero():
  fi = {"albumReleaseId": 999, "album": {"releases": [{"id": 1, "trackCount": 9}]}}
  assert psi._release_track_count(fi) == 0
  assert psi._release_track_count({}) == 0


def test_stub_coverage_full_album():
  # 9 of 9 tracks of release 27195
  imported, total, frac = psi.stub_coverage({27195: 9}, {27195: 9})
  assert (imported, total) == (9, 9)
  assert frac == 1.0


def test_stub_coverage_stub():
  # 1 of 9 tracks -> 11%
  imported, total, frac = psi.stub_coverage({27195: 1}, {27195: 9})
  assert imported == 1 and total == 9
  assert frac < 0.5


def test_stub_coverage_dedup_not_penalised():
  # 9 importable tracks of a 9-track release, even if the folder had 18 files
  _, _, frac = psi.stub_coverage({100: 9}, {100: 9})
  assert frac == 1.0


def test_stub_coverage_unknown_total_does_not_block():
  # release size unknown (0) -> fraction 1.0 so the guard never fires
  _, total, frac = psi.stub_coverage({100: 2}, {100: 0})
  assert total == 0
  assert frac == 1.0


def test_stub_coverage_dominant_release_chosen():
  # most files mapped to release 2 (a stub there); release 1 is incidental
  imported, total, frac = psi.stub_coverage({1: 1, 2: 2}, {1: 1, 2: 12})
  assert (imported, total) == (2, 12)
  assert frac < 0.5


def test_stub_coverage_empty():
  assert psi.stub_coverage({}, {}) == (0, 0, 0.0)
