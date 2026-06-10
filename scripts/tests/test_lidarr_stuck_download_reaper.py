import datetime as _dt
import importlib.util
import sys
from pathlib import Path

UTC = _dt.UTC


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "lidarr_stuck_download_reaper.py"
  spec = importlib.util.spec_from_file_location("lidarr_stuck_download_reaper", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


reaper = _load_module()

NOW = _dt.datetime(2026, 6, 9, 12, 0, 0, tzinfo=UTC)


def _iso(hours_ago):
  return (NOW - _dt.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "")


def _dl(username, directory, files):
  return {"username": username, "directories": [{"directory": directory, "files": files}]}


def _f(state, hours_ago, bytes_transferred=0, fid="x"):
  return {
    "id": fid,
    "state": state,
    "enqueuedAt": _iso(hours_ago),
    "bytesTransferred": bytes_transferred,
  }


# ---- collect_stuck -------------------------------------------------------


def test_collect_stuck_flags_old_zero_byte_remote_queue():
  payload = [_dl("alice", "music\\Burzum\\Filosofem", [_f("Queued, Remotely", 20)])]
  stuck = reaper.collect_stuck(payload, stuck_hours=12, now=NOW)
  assert len(stuck) == 1
  assert stuck[0].local_dirname == "Filosofem"
  assert stuck[0].username == "alice"


def test_collect_stuck_ignores_recent_remote_queue():
  payload = [_dl("alice", "d", [_f("Queued, Remotely", 3)])]
  assert reaper.collect_stuck(payload, stuck_hours=12, now=NOW) == []


def test_collect_stuck_ignores_in_progress_and_started():
  # InProgress is never stuck; a started-then-paused remote queue (bytes>0) is alive.
  payload = [_dl("a", "d", [
    _f("InProgress", 30),
    _f("Queued, Remotely", 30, bytes_transferred=1024),
  ])]
  assert reaper.collect_stuck(payload, stuck_hours=12, now=NOW) == []


def test_collect_stuck_ignores_completed():
  payload = [_dl("a", "d", [_f("Completed, Succeeded", 99), _f("Completed, Errored", 99)])]
  assert reaper.collect_stuck(payload, stuck_hours=12, now=NOW) == []


def test_collect_stuck_includes_locally_queued_when_old():
  payload = [_dl("a", "d", [_f("Queued, Locally", 20)])]
  assert len(reaper.collect_stuck(payload, stuck_hours=12, now=NOW)) == 1


def test_collect_stuck_skips_missing_timestamp_conservatively():
  payload = [_dl("a", "d", [{"id": "x", "state": "Queued, Remotely", "bytesTransferred": 0}])]
  assert reaper.collect_stuck(payload, stuck_hours=12, now=NOW) == []


# ---- lidarr matching -----------------------------------------------------


def test_build_lidarr_map_indexes_basenames():
  records = [
    {"id": 5, "title": "Filosofem", "outputPath": "/music/Burzum/Filosofem"},
    {"id": 6, "title": "Other Album"},
  ]
  m = reaper.build_lidarr_map(records)
  assert m["Filosofem"] == 5
  assert m["Other Album"] == 6


# ---- plan_reap -----------------------------------------------------------


def _stuck(dirname, username="u", fid="f1"):
  return reaper.StuckTransfer(
    username=username, transfer_id=fid, state="Queued, Remotely",
    slskd_dir=f"d\\{dirname}", local_dirname=dirname,
    enqueued_at=NOW, bytes_transferred=0,
  )


def test_plan_reap_routes_matched_to_lidarr_and_orphans_to_slskd():
  stuck = [_stuck("Filosofem", fid="t1"), _stuck("Orphaned", fid="t2")]
  lidarr_map = {"Filosofem": 5}
  plan = reaper.plan_reap(stuck, lidarr_map, max_actions=10)
  assert plan.lidarr_deletes == [5]
  assert [t.transfer_id for t in plan.slskd_cancels] == ["t2"]


def test_plan_reap_dedupes_lidarr_rows_across_files():
  # two stuck files of the same album -> one Lidarr delete
  stuck = [_stuck("Filosofem", fid="t1"), _stuck("Filosofem", fid="t2")]
  plan = reaper.plan_reap(stuck, {"Filosofem": 5}, max_actions=10)
  assert plan.lidarr_deletes == [5]


def test_plan_reap_respects_max_actions():
  stuck = [_stuck(f"Album{i}", fid=f"t{i}") for i in range(10)]
  lidarr_map = {f"Album{i}": i for i in range(10)}
  plan = reaper.plan_reap(stuck, lidarr_map, max_actions=3)
  assert len(plan.lidarr_deletes) == 3
  assert plan.capped == 7
