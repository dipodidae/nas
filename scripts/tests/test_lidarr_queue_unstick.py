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


def _wedged(qid, *, output_path="", messages=()):
  return unstick.WedgedItem(
    queue_id=qid,
    title=f"Item {qid}",
    status="completed",
    tracked_state="importFailed",
    added=None,
    output_path=output_path,
    messages=tuple(messages),
  )


def test_flatten_messages():
  rec = {
    "statusMessages": [
      {"title": "t1", "messages": ["Album release not requested", ""]},
      {"title": "t2", "messages": ["Has unmatched tracks"]},
      {"title": "t3"},
    ]
  }
  assert unstick._flatten_messages(rec) == (
    "Album release not requested",
    "Has unmatched tracks",
  )


def test_is_reclaimable_pure_edition_mismatch():
  item = _wedged(
    1,
    output_path="/downloads/complete/slskd/Heir",
    messages=["Album release not requested", "Album release not requested"],
  )
  assert unstick.is_reclaimable(item) is True


def test_is_reclaimable_requires_output_path():
  item = _wedged(1, output_path="", messages=["Album release not requested"])
  assert unstick.is_reclaimable(item) is False


def test_is_reclaimable_blocked_by_hard_blocker():
  # signal present but a fuzzy-match blocker means release switching won't help
  item = _wedged(
    1,
    output_path="/downloads/complete/slskd/Foo",
    messages=[
      "Album release not requested",
      "Album match is not close enough: 52.2 % vs 80 %",
    ],
  )
  assert unstick.is_reclaimable(item) is False


def test_is_reclaimable_no_signal():
  item = _wedged(1, output_path="/x", messages=["Has unmatched tracks"])
  assert unstick.is_reclaimable(item) is False


def test_reclaim_item_success_on_trackfile_increase(monkeypatch):
  item = _wedged(1, output_path="/downloads/x", messages=["Album release not requested"])
  monkeypatch.setattr(
    unstick, "_scan_for_import",
    lambda *a, **k: [{"albumId": 9, "artistId": 4, "path": "/downloads/x/a.mp3"}],
  )
  counts = iter([0, 7])  # before=0, after=7
  monkeypatch.setattr(unstick, "_trackfile_count", lambda *a, **k: next(counts))
  submitted: list[str] = []
  monkeypatch.setattr(
    unstick, "_submit_import",
    lambda h, k, items, mode: submitted.append(mode) or True,
  )
  assert unstick.reclaim_item("h", "k", item) is True
  assert submitted == ["copy"]  # primary import only, no in-place fallback


def test_reclaim_item_falls_back_to_in_place(monkeypatch):
  item = _wedged(1, output_path="/downloads/x", messages=["Album release not requested"])

  def _scan(host, key, folder):
    return [{"albumId": 9, "artistId": 4, "path": f"{folder}/a.mp3"}]

  monkeypatch.setattr(unstick, "_scan_for_import", _scan)
  # before=0, after primary import=0 (no-op), after in-place=7
  counts = iter([0, 0, 7])
  monkeypatch.setattr(unstick, "_trackfile_count", lambda *a, **k: next(counts))
  monkeypatch.setattr(unstick, "_artist_path", lambda *a, **k: "/music/Artist")
  modes: list[str] = []
  monkeypatch.setattr(
    unstick, "_submit_import",
    lambda h, k, items, mode: modes.append(mode) or True,
  )
  assert unstick.reclaim_item("h", "k", item) is True
  assert modes == ["copy", "move"]  # primary copy, then in-place move


def test_reclaim_item_false_when_no_effect(monkeypatch):
  item = _wedged(1, output_path="/downloads/x", messages=["Album release not requested"])
  monkeypatch.setattr(
    unstick, "_scan_for_import",
    lambda *a, **k: [{"albumId": 9, "artistId": 4, "path": "/downloads/x/a.mp3"}],
  )
  monkeypatch.setattr(unstick, "_trackfile_count", lambda *a, **k: 0)  # never increases
  monkeypatch.setattr(unstick, "_artist_path", lambda *a, **k: "/music/Artist")
  monkeypatch.setattr(unstick, "_submit_import", lambda *a, **k: True)
  # No track files ever appear -> must not report success (no false row clear).
  assert unstick.reclaim_item("h", "k", item) is False


def test_reclaim_item_false_on_empty_scan(monkeypatch):
  item = _wedged(1, output_path="/downloads/x", messages=["Album release not requested"])
  monkeypatch.setattr(unstick, "_scan_for_import", lambda *a, **k: [])
  assert unstick.reclaim_item("h", "k", item) is False


def test_main_reclaim_then_clear(monkeypatch, capsys):
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  records = [
    {
      "id": 1,
      "title": "Lamp of Murmuur — Heir",
      "status": "completed",
      "trackedDownloadState": "importFailed",
      "added": "2026-05-25T08:00:00Z",
      "outputPath": "/downloads/complete/slskd/Heir",
      "statusMessages": [
        {"title": "x", "messages": ["Album release not requested"]},
      ],
    },
  ]
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: records)
  fixed_now = _dt.datetime(2026, 5, 25, 12, 0, 0)

  class _FixedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
      return fixed_now

  monkeypatch.setattr(unstick._dt, "datetime", _FixedDateTime)

  reclaimed_ids: list[int] = []
  monkeypatch.setattr(
    unstick,
    "reclaim_item",
    lambda h, k, item, **kw: reclaimed_ids.append(item.queue_id) or True,
  )
  cleared: list[tuple] = []

  def _del(host, key, item, blocklist=True, skip_redownload=False):
    cleared.append((item.queue_id, blocklist, skip_redownload))
    return True

  monkeypatch.setattr(unstick, "delete_item", _del)
  assert unstick.main(["--min-age-hours", "1"]) == 0
  assert reclaimed_ids == [1]
  # cleanup delete must be non-destructive: no blocklist, no re-search
  assert cleared == [(1, False, True)]
  out = capsys.readouterr().out
  assert "reclaimed 1/1" in out


def test_main_reclaim_failure_falls_through_to_delete(monkeypatch, capsys):
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  records = [
    {
      "id": 1,
      "title": "Lamp of Murmuur — Heir",
      "status": "completed",
      "trackedDownloadState": "importFailed",
      "added": "2026-05-25T08:00:00Z",
      "outputPath": "/downloads/complete/slskd/Heir",
      "statusMessages": [
        {"title": "x", "messages": ["Album release not requested"]},
      ],
    },
  ]
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: records)
  fixed_now = _dt.datetime(2026, 5, 25, 12, 0, 0)

  class _FixedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
      return fixed_now

  monkeypatch.setattr(unstick._dt, "datetime", _FixedDateTime)
  monkeypatch.setattr(unstick, "reclaim_item", lambda *a, **k: False)
  deleted: list[tuple] = []

  def _del(host, key, item, blocklist=True, skip_redownload=False):
    deleted.append((item.queue_id, blocklist, skip_redownload))
    return True

  monkeypatch.setattr(unstick, "delete_item", _del)
  assert unstick.main(["--min-age-hours", "1"]) == 0
  # failed reclaim falls through to destructive delete (blocklist on)
  assert deleted == [(1, True, False)]


def test_main_no_reclaim_skips_reclaim_pass(monkeypatch, capsys):
  monkeypatch.setenv("API_KEY_LIDARR", "x")
  records = [
    {
      "id": 1,
      "title": "Lamp of Murmuur — Heir",
      "status": "completed",
      "trackedDownloadState": "importFailed",
      "added": "2026-05-25T08:00:00Z",
      "outputPath": "/downloads/complete/slskd/Heir",
      "statusMessages": [
        {"title": "x", "messages": ["Album release not requested"]},
      ],
    },
  ]
  monkeypatch.setattr(unstick, "fetch_queue", lambda *_a, **_k: records)
  fixed_now = _dt.datetime(2026, 5, 25, 12, 0, 0)

  class _FixedDateTime(_dt.datetime):
    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
      return fixed_now

  monkeypatch.setattr(unstick._dt, "datetime", _FixedDateTime)

  def _boom(*a, **k):
    raise AssertionError("reclaim_item must not be called with --no-reclaim")

  monkeypatch.setattr(unstick, "reclaim_item", _boom)
  monkeypatch.setattr(unstick, "delete_item", lambda *a, **k: True)
  assert unstick.main(["--min-age-hours", "1", "--no-reclaim"]) == 0
  assert "reclaim 0" in capsys.readouterr().out


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
