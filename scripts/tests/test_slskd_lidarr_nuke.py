import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "slskd_lidarr_nuke.py"
  spec = importlib.util.spec_from_file_location("slskd_lidarr_nuke", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


nuke = _load_module()


# ---- plan_lidarr_nuke ----------------------------------------------------


def test_plan_lidarr_nuke_empty_queue():
  assert nuke.plan_lidarr_nuke([]) == []


def test_plan_lidarr_nuke_selects_all_states():
  records = [
    {"id": 1, "status": "downloading"},
    {"id": 2, "status": "importPending"},
    {"id": 3, "status": "completed"},
    {"id": 4, "status": "warning"},
  ]
  assert nuke.plan_lidarr_nuke(records) == [1, 2, 3, 4]


def test_plan_lidarr_nuke_skips_rows_without_int_id():
  records = [{"id": 1}, {"id": None}, {"title": "no id"}, {"id": "x"}]
  assert nuke.plan_lidarr_nuke(records) == [1]


# ---- collect_slskd_transfers ---------------------------------------------


def _dl(username, directory, files):
  return {"username": username, "directories": [{"directory": directory, "files": files}]}


def test_collect_slskd_transfers_partitions_active_and_terminal():
  payload = [
    _dl("alice", "music\\A\\X", [
      {"id": "t1", "state": "Queued, Remotely"},
      {"id": "t2", "state": "InProgress"},
      {"id": "t3", "state": "Completed, Succeeded"},
      {"id": "t4", "state": "Completed, Errored"},
    ]),
  ]
  active, terminal = nuke.collect_slskd_transfers(payload)
  assert {(t.username, t.transfer_id) for t in active} == {("alice", "t1"), ("alice", "t2")}
  assert terminal == 2


def test_collect_slskd_transfers_empty():
  assert nuke.collect_slskd_transfers([]) == ([], 0)
  assert nuke.collect_slskd_transfers("not a list") == ([], 0)


# ---- spare_basenames -----------------------------------------------------


def test_spare_basenames_extracts_path_basenames():
  records = [
    {"outputPath": "/data/downloads/complete/slskd/Album One"},
    {"downloadForcedClientPath": "music\\Artist\\Album Two\\"},
    {"title": "Album Three"},
    {"outputPath": ""},
  ]
  assert nuke.spare_basenames(records) == {"Album One", "Album Two", "Album Three"}


# ---- plan_folder_sweep ---------------------------------------------------


def test_plan_folder_sweep_selects_unspared_children(tmp_path):
  root = tmp_path / "slskd"
  root.mkdir()
  keep = root / "Importing Now"
  drop = root / "Orphan Album"
  keep.mkdir()
  drop.mkdir()
  (root / "loose.txt").write_text("not a dir")  # files ignored
  targets = nuke.plan_folder_sweep(root, {"Importing Now"})
  assert targets == [drop]


def test_plan_folder_sweep_containment_rejects_escape(tmp_path):
  root = tmp_path / "slskd"
  root.mkdir()
  outside = tmp_path / "outside"
  outside.mkdir()
  (root / "link").symlink_to(outside, target_is_directory=True)
  with pytest.raises(ValueError):
    nuke.plan_folder_sweep(root, set())
