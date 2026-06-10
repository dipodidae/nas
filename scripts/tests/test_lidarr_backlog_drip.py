import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "lidarr_backlog_drip.py"
  spec = importlib.util.spec_from_file_location("lidarr_backlog_drip", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


drip = _load_module()


def _downloads(states):
  """Build a /transfers/downloads payload with one file per given state."""
  return [{"directories": [{"files": [{"state": s} for s in states]}]}]


def test_count_inflight_excludes_completed():
  payload = _downloads([
    "Queued, Remotely", "InProgress", "Initializing",
    "Completed, Succeeded", "Completed, Cancelled", "Completed, Errored",
  ])
  assert drip.count_inflight(payload) == 3


def test_count_inflight_handles_empty_and_bad_shape():
  assert drip.count_inflight([]) == 0
  assert drip.count_inflight(None) == 0
  assert drip.count_inflight("nonsense") == 0


import datetime as _dt  # noqa: E402

_UTC = _dt.UTC
_NOW = _dt.datetime(2026, 6, 9, 12, 0, 0, tzinfo=_UTC)


def _ts(hours_ago):
  return (_NOW - _dt.timedelta(hours=hours_ago)).isoformat().replace("+00:00", "")


def _q(state, hours_ago, bytes_transferred=0):
  return {"state": state, "enqueuedAt": _ts(hours_ago), "bytesTransferred": bytes_transferred}


def test_count_inflight_excludes_dead_remote_queue_entries():
  # 1 old dead remote-queue (excluded) + 1 recent remote-queue + 1 InProgress.
  payload = [{"directories": [{"files": [
    _q("Queued, Remotely", 30),                  # dead -> excluded
    _q("Queued, Remotely", 2),                   # recent -> counts (anti-flood)
    {"state": "InProgress"},                     # live -> counts
  ]}]}]
  assert drip.count_inflight(payload, stale_queued_hours=12, now=_NOW) == 2


def test_count_inflight_counts_started_then_paused_remote_queue():
  # bytes>0 means it actually started — alive even if old.
  payload = [{"directories": [{"files": [_q("Queued, Remotely", 30, bytes_transferred=5)]}]}]
  assert drip.count_inflight(payload, stale_queued_hours=12, now=_NOW) == 1


def test_count_inflight_counts_old_local_queue_and_states_without_timestamps():
  # "Queued, Locally" is never aged out, and a missing timestamp counts as live.
  payload = [{"directories": [{"files": [
    _q("Queued, Locally", 99),
    {"state": "Queued, Remotely"},               # no timestamp -> conservative, counts
  ]}]}]
  assert drip.count_inflight(payload, stale_queued_hours=12, now=_NOW) == 2


def test_select_albums_picks_batch_and_records_state():
  missing = [{"id": i} for i in range(1, 11)]
  ids, state = drip.select_albums(missing, {}, cooldown_hours=12, batch=3, now=1000.0)
  assert ids == [1, 2, 3]
  assert state == {"1": 1000.0, "2": 1000.0, "3": 1000.0}


def test_select_albums_skips_albums_within_cooldown():
  missing = [{"id": 1}, {"id": 2}, {"id": 3}]
  # id 1 searched 1h ago (within 12h cooldown) -> skipped; 2 & 3 picked.
  now = 100000.0
  prior = {"1": now - 3600}
  ids, state = drip.select_albums(missing, prior, cooldown_hours=12, batch=5, now=now)
  assert ids == [2, 3]
  assert state["1"] == now - 3600  # untouched record retained


def test_select_albums_retries_after_cooldown_and_prunes_old():
  missing = [{"id": 1}]
  now = 100000.0
  prior = {"1": now - 13 * 3600, "999": now - 50 * 3600}  # both older than cooldown
  ids, state = drip.select_albums(missing, prior, cooldown_hours=12, batch=5, now=now)
  assert ids == [1]               # eligible again
  assert state["1"] == now        # re-stamped
  assert "999" not in state       # stale entry pruned


def test_select_albums_ignores_non_int_ids():
  missing = [{"id": None}, {"foo": "bar"}, {"id": 7}]
  ids, _ = drip.select_albums(missing, {}, cooldown_hours=12, batch=5, now=1.0)
  assert ids == [7]


def _wire_main(monkeypatch, *, missing_ids, lidarr_post, sleeps, saved):
  """Common monkeypatching for main(): 0 in-flight, fixed missing list."""
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  monkeypatch.setenv("API_KEY_SLSKD", "y")
  monkeypatch.setattr(drip, "_slskd_downloads", lambda *a, **k: _downloads([]))

  def fake_lidarr(host, key, path, method="GET", body=None):
    if method == "GET":
      return {"totalRecords": len(missing_ids),
              "records": [{"id": i} for i in missing_ids]}
    return lidarr_post(body)

  monkeypatch.setattr(drip, "_lidarr", fake_lidarr)
  monkeypatch.setattr(drip, "load_state", lambda p: {})
  monkeypatch.setattr(drip, "save_state", lambda p, s: saved.update(s))
  monkeypatch.setattr(drip.time, "sleep", sleeps.append)


def test_main_paces_searches_one_per_album(monkeypatch):
  posts, sleeps, saved = [], [], {}
  _wire_main(monkeypatch, missing_ids=[1, 2, 3],
             lidarr_post=lambda body: posts.append(body) or {"id": 9},
             sleeps=sleeps, saved=saved)
  assert drip.main(["--batch", "3", "--search-delay", "20", "--threshold", "40"]) == 0
  # one single-id search per album — no 20-wide burst
  assert posts == [
    {"name": "AlbumSearch", "albumIds": [1]},
    {"name": "AlbumSearch", "albumIds": [2]},
    {"name": "AlbumSearch", "albumIds": [3]},
  ]
  # spaced between each, but not after the last
  assert sleeps == [20, 20]


def test_main_burst_mode_when_delay_zero(monkeypatch):
  posts, sleeps, saved = [], [], {}
  _wire_main(monkeypatch, missing_ids=[1, 2, 3],
             lidarr_post=lambda body: posts.append(body) or {"id": 9},
             sleeps=sleeps, saved=saved)
  assert drip.main(["--batch", "3", "--search-delay", "0", "--threshold", "40"]) == 0
  # legacy single command with all ids, no pacing
  assert posts == [{"name": "AlbumSearch", "albumIds": [1, 2, 3]}]
  assert sleeps == []


def test_main_failed_search_left_unstamped_for_retry(monkeypatch):
  posts, sleeps, saved = [], [], {}

  def post(body):
    posts.append(body)
    if body["albumIds"] == [2]:
      raise OSError("boom")
    return {"id": 9}

  _wire_main(monkeypatch, missing_ids=[1, 2, 3],
             lidarr_post=post, sleeps=sleeps, saved=saved)
  # one failure -> exit 1, and album 2 must NOT be stamped (so it retries)
  assert drip.main(["--batch", "3", "--search-delay", "5", "--threshold", "40"]) == 1
  assert "1" in saved and "3" in saved
  assert "2" not in saved
