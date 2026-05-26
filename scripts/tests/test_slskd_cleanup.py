import datetime as _dt
import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "slskd_cleanup.py"
  spec = importlib.util.spec_from_file_location("slskd_cleanup", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


cleanup = _load_module()


def test_trailing_segment_backslash():
  assert cleanup._trailing_segment("albums\\Killing Joke\\Democracy") == "Democracy"


def test_trailing_segment_forwardslash():
  assert cleanup._trailing_segment("music/Albums/Burzum/Filosofem") == "Filosofem"


def test_trailing_segment_mixed():
  assert cleanup._trailing_segment("a\\b/c\\d") == "d"


def test_trailing_segment_single():
  assert cleanup._trailing_segment("Democracy") == "Democracy"


def test_trailing_segment_trailing_sep():
  assert cleanup._trailing_segment("a\\b\\") == "b"


def test_trailing_segment_empty():
  assert cleanup._trailing_segment("") == ""


def _make_downloads():
  return [
    {
      "username": "alice",
      "directories": [
        {
          "directory": "music\\Burzum\\Filosofem",
          "files": [
            {
              "id": "id-1",
              "state": "Completed, Succeeded",
              "endedAt": "2026-05-24T00:00:00",
            },
            {
              "id": "id-2",
              "state": "Completed, Errored",
              "endedAt": "2026-05-23T12:00:00",
            },
            {"id": "id-3", "state": "InProgress"},  # not completed -> skip
          ],
        },
      ],
    },
    {
      "username": "bob",
      "directories": [
        {
          "directory": "albums/Killing Joke/Democracy",
          "files": [
            {
              "id": "id-4",
              "state": "Completed, Rejected",
              "endedAt": "2026-05-24T08:00:00",
            }
          ],
        },
        {
          "directory": "",  # empty directory string -> empty local dirname
          "files": [{"id": "id-5", "state": "Completed, Succeeded"}],  # no endedAt
        },
      ],
    },
  ]


def test_collect_stale_filters_completed_only():
  stale = cleanup.collect_stale(_make_downloads())
  ids = sorted(t.transfer_id for t in stale)
  assert ids == ["id-1", "id-2", "id-4", "id-5"]
  by_id = {t.transfer_id: t for t in stale}
  assert by_id["id-1"].local_dirname == "Filosofem"
  assert by_id["id-4"].local_dirname == "Democracy"
  assert by_id["id-5"].local_dirname == ""
  assert by_id["id-1"].username == "alice"
  assert by_id["id-4"].username == "bob"


def test_collect_stale_empty():
  assert cleanup.collect_stale([]) == []


def test_remove_orphan_dirs_respects_allowlist(tmp_path):
  # Two music dirs slskd reported + one qBittorrent-looking dir not in the set.
  (tmp_path / "Filosofem").mkdir()
  (tmp_path / "Filosofem" / "01.mp3").write_text("x")
  (tmp_path / "Democracy").mkdir()
  (tmp_path / "The.Wire.S01.1080p.BluRay.x264-ROVERS").mkdir()
  (tmp_path / "The.Wire.S01.1080p.BluRay.x264-ROVERS" / "ep01.mkv").write_text("y")

  removed, skipped, errors = cleanup.remove_orphan_dirs(
    tmp_path, {"Filosofem", "Democracy"}, min_age_hours=0
  )
  assert removed == 2
  assert skipped == 0
  assert errors == []
  assert not (tmp_path / "Filosofem").exists()
  assert not (tmp_path / "Democracy").exists()
  # The qBittorrent-style dir was NOT in the allowlist -> untouched.
  assert (tmp_path / "The.Wire.S01.1080p.BluRay.x264-ROVERS").exists()


def test_remove_orphan_dirs_min_age_skip(tmp_path):
  (tmp_path / "Recent").mkdir()
  # mtime defaults to now -> too recent for min_age_hours=1
  removed, skipped, errors = cleanup.remove_orphan_dirs(
    tmp_path, {"Recent"}, min_age_hours=1
  )
  assert removed == 0
  assert skipped == 1
  assert errors == []
  assert (tmp_path / "Recent").exists()


def test_remove_orphan_dirs_min_age_zero_removes(tmp_path):
  (tmp_path / "Recent").mkdir()
  removed, skipped, errors = cleanup.remove_orphan_dirs(
    tmp_path, {"Recent"}, min_age_hours=0
  )
  assert removed == 1
  assert skipped == 0
  assert not (tmp_path / "Recent").exists()


def test_remove_orphan_dirs_missing_name_ignored(tmp_path):
  removed, skipped, errors = cleanup.remove_orphan_dirs(
    tmp_path, {"DoesNotExist", ""}, min_age_hours=0
  )
  assert removed == 0
  assert skipped == 0
  assert errors == []


def test_remove_orphan_dirs_file_not_dir(tmp_path):
  # A regular file with the same name -> we only target dirs, so leave it alone.
  (tmp_path / "weird").write_text("not a dir")
  removed, _, errors = cleanup.remove_orphan_dirs(
    tmp_path, {"weird"}, min_age_hours=0
  )
  assert removed == 0
  assert errors == []
  assert (tmp_path / "weird").exists()


def test_parse_iso_basic():
  d = cleanup._parse_iso("2026-05-24T09:19:21")
  assert d == _dt.datetime(2026, 5, 24, 9, 19, 21)


def test_parse_iso_with_fractional_overflow():
  # slskd emits sub-second precision longer than Python's 6-digit cap.
  d = cleanup._parse_iso("2026-05-24T09:19:21.7392604")
  assert d is not None
  assert d.year == 2026 and d.month == 5 and d.day == 24
  assert d.hour == 9 and d.minute == 19 and d.second == 21


def test_parse_iso_missing():
  assert cleanup._parse_iso(None) is None
  assert cleanup._parse_iso("") is None
  assert cleanup._parse_iso("not a date") is None


def test_filter_old_enough_splits_by_age():
  now = _dt.datetime(2026, 5, 24, 10, 0, 0)
  stale = cleanup.collect_stale(_make_downloads())
  eligible, skipped = cleanup.filter_old_enough(stale, min_age_hours=1, now=now)
  ids = {t.transfer_id for t in eligible}
  # id-1 ended 10h ago — eligible
  # id-2 ended 22h ago — eligible
  # id-4 ended 2h ago — eligible
  # id-5 has no endedAt — conservatively skipped
  assert ids == {"id-1", "id-2", "id-4"}
  assert {t.transfer_id for t in skipped} == {"id-5"}


def test_filter_old_enough_within_window():
  now = _dt.datetime(2026, 5, 24, 8, 30, 0)
  stale = cleanup.collect_stale(_make_downloads())
  eligible, skipped = cleanup.filter_old_enough(stale, min_age_hours=1, now=now)
  # id-4 ended at 08:00 (30min ago) -> within window -> skipped
  # id-1 ended at 00:00 (8.5h ago)  -> eligible
  # id-2 ended at prev-day 12:00 (~20.5h ago) -> eligible
  # id-5 no endedAt -> skipped
  ids_eligible = {t.transfer_id for t in eligible}
  ids_skipped = {t.transfer_id for t in skipped}
  assert ids_eligible == {"id-1", "id-2"}
  assert ids_skipped == {"id-4", "id-5"}


def test_partition_by_gate_no_active_names_returns_all_deletable():
  stale = cleanup.collect_stale(_make_downloads())
  deletable, deferred = cleanup.partition_by_gate(stale, active_names=set())
  assert {t.transfer_id for t in deletable} == {"id-1", "id-2", "id-4", "id-5"}
  assert deferred == []


def test_partition_by_gate_none_active_names_returns_all_deletable():
  stale = cleanup.collect_stale(_make_downloads())
  deletable, deferred = cleanup.partition_by_gate(stale, active_names=None)
  assert {t.transfer_id for t in deletable} == {"id-1", "id-2", "id-4", "id-5"}
  assert deferred == []


def test_partition_by_gate_only_defers_matched_succeeded():
  # _make_downloads() has:
  #   id-1: `Completed, Succeeded` local_dirname=Filosofem
  #   id-2: `Completed, Errored`   (terminal failure — always deletable)
  #   id-4: `Completed, Rejected`  (terminal failure — always deletable)
  #   id-5: `Completed, Succeeded` local_dirname="" (empty -> can never match)
  # Active name "Filosofem" should defer id-1 only.
  stale = cleanup.collect_stale(_make_downloads())
  deletable, deferred = cleanup.partition_by_gate(stale, active_names={"Filosofem"})
  assert {t.transfer_id for t in deletable} == {"id-2", "id-4", "id-5"}
  assert {t.transfer_id for t in deferred} == {"id-1"}


def test_partition_by_gate_unmatched_succeeded_is_deletable():
  # Active names that match nothing in the slskd batch -> nothing deferred,
  # all rows (including Succeeded) returned as deletable.
  stale = cleanup.collect_stale(_make_downloads())
  deletable, deferred = cleanup.partition_by_gate(
    stale, active_names={"SomeOtherAlbum"}
  )
  assert {t.transfer_id for t in deletable} == {"id-1", "id-2", "id-4", "id-5"}
  assert deferred == []


def test_partition_by_gate_terminal_failure_never_deferred_even_if_matched():
  # Even if a terminal-failure record's name happens to be in active_names,
  # it never has a Tubifarry-side actor, so it's still deletable.
  stale = cleanup.collect_stale(_make_downloads())
  deletable, deferred = cleanup.partition_by_gate(
    stale, active_names={"Filosofem", "Democracy"}
  )
  # id-4 (Rejected) has local_dirname=Democracy — but Rejected is terminal,
  # so it stays deletable. Only id-1 (Succeeded, Filosofem) is deferred.
  assert {t.transfer_id for t in deferred} == {"id-1"}
  assert "id-4" in {t.transfer_id for t in deletable}


def test_active_lidarr_states_includes_importFailed():
  # importFailed must be in the gate so we yield the Completed,Succeeded
  # transfer to lidarr_queue_unstick.py — otherwise both scripts could race
  # to DELETE the same slskd record (us directly, it via Tubifarry).
  assert "importFailed" in cleanup.ACTIVE_LIDARR_STATES


def test_filter_old_enough_zero_age_passes_everything_with_timestamp():
  now = _dt.datetime(2026, 5, 24, 10, 0, 0)
  stale = cleanup.collect_stale(_make_downloads())
  eligible, skipped = cleanup.filter_old_enough(stale, min_age_hours=0, now=now)
  # min_age=0 still skips records with no endedAt
  ids = {t.transfer_id for t in eligible}
  assert "id-5" not in ids
  assert ids == {"id-1", "id-2", "id-4"}
  assert {t.transfer_id for t in skipped} == {"id-5"}
