import datetime as _dt
import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "lidarr_queue_unstick.py"
  spec = importlib.util.spec_from_file_location("lidarr_queue_unstick", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


unstick = _load_module()


def _records():
  return [
    {
      "id": 1,
      "title": "Artist A — Album A",
      "status": "completed",
      "trackedDownloadState": "importFailed",
      "added": "2026-05-25T08:00:00Z",
    },
    {
      "id": 2,
      "title": "Artist B — Album B",
      "status": "completed",
      "trackedDownloadState": "importFailed",
      "added": "2026-05-25T11:30:00Z",
    },
    {
      "id": 3,
      "title": "Artist C — Album C",
      "status": "queued",
      "trackedDownloadState": "downloading",
      "added": "2026-05-25T07:00:00Z",
    },
    {
      "id": 4,
      "title": "Artist D — Album D",
      "status": "completed",
      "trackedDownloadState": "importFailed",
      # no 'added' field
    },
    {
      # missing id -> skipped
      "title": "Artist E — Album E",
      "trackedDownloadState": "importFailed",
      "added": "2026-05-25T01:00:00Z",
    },
  ]


def test_collect_wedged_only_import_failed():
  wedged = unstick.collect_wedged(_records())
  ids = sorted(w.queue_id for w in wedged)
  # id=1, 2, 4 are importFailed with valid int ids.
  # id=3 is downloading -> filtered out.
  # last record has no id -> filtered out.
  assert ids == [1, 2, 4]


def test_collect_wedged_empty():
  assert unstick.collect_wedged([]) == []


def test_filter_old_enough_splits_by_age():
  # "now" = 2026-05-25T12:00:00 UTC
  now = _dt.datetime(2026, 5, 25, 12, 0, 0)
  wedged = unstick.collect_wedged(_records())
  eligible, skipped = unstick.filter_old_enough(wedged, min_age_hours=1.0, now=now)
  # id=1 added 4h ago -> eligible
  # id=2 added 30min ago -> skipped (too recent)
  # id=4 no added -> skipped
  assert {w.queue_id for w in eligible} == {1}
  assert {w.queue_id for w in skipped} == {2, 4}


def test_filter_old_enough_zero_age_keeps_skipping_missing_added():
  now = _dt.datetime(2026, 5, 25, 12, 0, 0)
  wedged = unstick.collect_wedged(_records())
  eligible, skipped = unstick.filter_old_enough(wedged, min_age_hours=0, now=now)
  # min_age=0 makes both timestamped items eligible; record without 'added'
  # is still skipped conservatively.
  assert {w.queue_id for w in eligible} == {1, 2}
  assert {w.queue_id for w in skipped} == {4}


def test_parse_iso_basic():
  d = unstick._parse_iso("2026-05-25T11:58:44Z")
  assert d == _dt.datetime(2026, 5, 25, 11, 58, 44)


def test_parse_iso_with_fractional():
  d = unstick._parse_iso("2026-05-25T11:58:44.1234567Z")
  assert d is not None
  assert d.year == 2026 and d.month == 5 and d.day == 25
  assert d.hour == 11 and d.minute == 58 and d.second == 44


def test_parse_iso_missing():
  assert unstick._parse_iso(None) is None
  assert unstick._parse_iso("") is None
  assert unstick._parse_iso("not a date") is None


def test_main_missing_api_key(monkeypatch, capsys):
  monkeypatch.delenv("API_KEY_LIDARR", raising=False)
  # Prevent dotenv side-effects from re-injecting the key from a project .env.
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: [])
  assert unstick.main([]) == 2
  err = capsys.readouterr().err
  assert "API_KEY_LIDARR" in err


def test_main_nothing_to_clean(monkeypatch, capsys):
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: [])
  assert unstick.main([]) == 0
  assert "nothing to clean" in capsys.readouterr().out


def test_main_dry_run_lists_plan(monkeypatch, capsys):
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: _records())
  # Force "now" so the 4h-old record is eligible.
  fixed_now = _dt.datetime(2026, 5, 25, 12, 0, 0)

  class _FixedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
      return fixed_now

  monkeypatch.setattr(unstick._dt, "datetime", _FixedDateTime)
  called: list[tuple] = []
  monkeypatch.setattr(unstick, "delete_item", lambda *a, **k: called.append((a, k)) or True)
  assert unstick.main(["--dry-run", "--min-age-hours", "1"]) == 0
  out = capsys.readouterr().out
  assert "plan:" in out
  assert "DRY remove #1" in out
  # Dry-run must not call delete_item.
  assert called == []


def test_main_delete_succeeds(monkeypatch, capsys):
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: _records())
  fixed_now = _dt.datetime(2026, 5, 25, 12, 0, 0)

  class _FixedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
      return fixed_now

  monkeypatch.setattr(unstick._dt, "datetime", _FixedDateTime)
  deleted: list[int] = []

  def _ok(host, key, item, blocklist=True, skip_redownload=False):
    deleted.append(item.queue_id)
    assert blocklist is True
    assert skip_redownload is False
    return True

  monkeypatch.setattr(unstick, "delete_item", _ok)
  assert unstick.main(["--min-age-hours", "1"]) == 0
  assert deleted == [1]
  assert "removed 1/1" in capsys.readouterr().out


def test_main_partial_failure_returns_1(monkeypatch, capsys):
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: _records())
  fixed_now = _dt.datetime(2026, 5, 25, 12, 0, 0)

  class _FixedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
      return fixed_now

  monkeypatch.setattr(unstick._dt, "datetime", _FixedDateTime)
  monkeypatch.setattr(unstick, "delete_item", lambda *a, **k: False)
  assert unstick.main(["--min-age-hours", "1"]) == 1
  assert "WARNING" in capsys.readouterr().err
