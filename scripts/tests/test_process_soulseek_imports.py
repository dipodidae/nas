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


# --- _is_not_upgrade --------------------------------------------------------


def test_is_not_upgrade_all_not_upgrade():
  assert psi._is_not_upgrade(["Not an upgrade for existing track file(s)"]) is True
  assert psi._is_not_upgrade(
    ["Not an upgrade for existing album file(s)", "not an upgrade for existing track file(s)"]
  ) is True


def test_is_not_upgrade_empty_is_false():
  assert psi._is_not_upgrade([]) is False


def test_is_not_upgrade_mixed_blocker_is_false():
  # a "couldn't find similar" track may be genuinely missing — not safe to purge
  assert psi._is_not_upgrade(
    ["Not an upgrade for existing track file(s)", "Couldn't find similar album for ..."]
  ) is False


# --- process_folder purge pass ---------------------------------------------


class _FakeLog:
  def info(self, *a, **k):
    pass

  def warning(self, *a, **k):
    pass

  def error(self, *a, **k):
    pass


class _FakeClient:
  """Minimal LidarrClient stand-in: returns canned manual-import items."""

  def __init__(self, items):
    self._items = items
    self.posted = False

  def get_manual_import(self, folder, filter_existing=True):
    return self._items

  def post_manual_import(self, items, import_mode="copy"):
    self.posted = True
    return {"id": 1}

  def wait_for_command(self, command_id, timeout=300):
    return {"status": "completed", "result": "successful"}


def _rejected_item(reason):
  return {
    "path": "/downloads/complete/slskd/X/01.mp3",
    "rejections": [{"reason": reason}],
    "artist": {"id": 1, "artistName": "A"},
    "album": {"id": 2, "title": "B"},
    "tracks": [{"id": 3}],
  }


def _make_folder(tmp_path):
  d = tmp_path / "Dead Album"
  d.mkdir()
  (d / "01.mp3").write_bytes(b"x")
  return d


def test_purge_deletes_pure_not_upgrade(tmp_path):
  d = _make_folder(tmp_path)
  client = _FakeClient([_rejected_item("Not an upgrade for existing track file(s)")])
  res = psi.process_folder(
    client, "/downloads/complete/slskd", d.name,
    execute=True, purge_not_upgrade=True, host_folder=d, log=_FakeLog(),
  )
  assert res.status == "purged"
  assert not d.exists()
  assert client.posted is False  # never tried to import


def test_purge_dry_run_keeps_folder(tmp_path):
  d = _make_folder(tmp_path)
  client = _FakeClient([_rejected_item("Not an upgrade for existing track file(s)")])
  res = psi.process_folder(
    client, "/downloads/complete/slskd", d.name,
    execute=False, purge_not_upgrade=True, host_folder=d, log=_FakeLog(),
  )
  assert res.status == "purged"
  assert d.exists()  # dry-run must not delete


def test_purge_skips_when_flag_off(tmp_path):
  d = _make_folder(tmp_path)
  client = _FakeClient([_rejected_item("Not an upgrade for existing track file(s)")])
  res = psi.process_folder(
    client, "/downloads/complete/slskd", d.name,
    execute=True, purge_not_upgrade=False, host_folder=d, log=_FakeLog(),
  )
  assert res.status == "skipped"
  assert d.exists()


def test_purge_skips_mixed_rejections(tmp_path):
  d = _make_folder(tmp_path)
  client = _FakeClient([
    _rejected_item("Not an upgrade for existing track file(s)"),
    _rejected_item("Couldn't find similar album for /downloads/complete/slskd/X"),
  ])
  res = psi.process_folder(
    client, "/downloads/complete/slskd", d.name,
    execute=True, purge_not_upgrade=True, host_folder=d, log=_FakeLog(),
  )
  assert res.status == "skipped"  # one track might be genuinely missing
  assert d.exists()
