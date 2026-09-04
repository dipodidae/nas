"""Tests for scripts/lidarr_jellyfin_bridge.py — pure-logic, no network."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "lidarr_jellyfin_bridge.py"
    spec = importlib.util.spec_from_file_location("lidarr_jellyfin_bridge", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


bridge = _load_module()


_ID = iter(range(1000, 100000))


def _imported(date, path, rid=None):
    return {
        "id": rid if rid is not None else next(_ID),
        "date": date,
        "eventType": "trackFileImported",
        "data": {"importedPath": path},
    }


def _renamed(date, old, new, rid=None):
    return {
        "id": rid if rid is not None else next(_ID),
        "date": date,
        "eventType": "trackFileRenamed",
        "data": {"path": new, "sourcePath": old},
    }


def _cur(rid=None, date="2026-09-01T00:00:00Z"):
    return bridge.Cursor(rid, date)


# --- translate ---


def test_translate_rewrites_the_media_root():
    got = bridge.translate(["/music/Bathory/1988 - Blood Fire Death"], "/music", "/data/movies/music")
    assert got == ["/data/movies/music/Bathory/1988 - Blood Fire Death"]


def test_translate_drops_paths_outside_the_root():
    """An untranslated path is the exact silent no-op this script exists to fix."""
    got = bridge.translate(["/downloads/complete/slskd/x", "/music/A/B"], "/music", "/data/movies/music")
    assert got == ["/data/movies/music/A/B"]


def test_translate_does_not_match_a_partial_directory_name():
    got = bridge.translate(["/musicvideos/A/B"], "/music", "/data/movies/music")
    assert got == []


def test_translate_handles_the_root_itself():
    got = bridge.translate(["/music"], "/music", "/data/movies/music")
    assert got == ["/data/movies/music"]


def test_translate_tolerates_trailing_slashes_in_config():
    got = bridge.translate(["/music/A/B"], "/music/", "/data/movies/music/")
    assert got == ["/data/movies/music/A/B"]


# --- changed_folders ---


def test_takes_the_album_folder_not_the_track_file():
    records = [_imported("2026-09-01T14:13:31Z", "/music/Tensal/2025 - X/01 - Aczio.mp3", rid=5)]
    folders, cursor = bridge.changed_folders(records, _cur(4))
    assert folders == ["/music/Tensal/2025 - X"]
    assert cursor.high_water_id == 5


def test_deduplicates_the_ten_tracks_of_one_album():
    records = [
        _imported("2026-09-01T14:13:31Z", f"/music/A/2025 - B/{n:02d} - t.mp3") for n in range(1, 11)
    ]
    folders, _ = bridge.changed_folders(records, _cur(0))
    assert folders == ["/music/A/2025 - B"]


def test_records_at_or_before_the_cursor_id_are_skipped():
    records = [
        _imported("2026-09-01T10:00:00Z", "/music/Old/2020 - X/01.mp3", rid=10),
        _imported("2026-09-01T12:00:00Z", "/music/New/2021 - Y/01.mp3", rid=11),
    ]
    folders, cursor = bridge.changed_folders(records, _cur(10))
    assert folders == ["/music/New/2021 - Y"]
    assert cursor.high_water_id == 11


def test_records_sharing_the_cursors_second_are_still_processed():
    """600 live records held 125 distinct dates; one second was shared by 22.

    A date cursor skipped every record in its own second, including ones written
    after the cursor was taken. An id cursor cannot.
    """
    same_second = "2026-09-03T20:12:50Z"
    records = [_imported(same_second, f"/music/A/Alb/{n:02d}.mp3", rid=100 + n) for n in range(5)]
    # cursor sits on record 102 -- same timestamp, lower id
    folders, cursor = bridge.changed_folders(records, bridge.Cursor(102, same_second))
    assert folders == ["/music/A/Alb"]
    assert cursor.high_water_id == 104


def test_rename_reports_both_source_and_destination_folders():
    """A rename across folders leaves stale metadata behind unless both refresh."""
    records = [_renamed("2026-09-01T16:47:55Z", "/music/A/Old Album/09.mp3", "/music/A/New Album/09.mp3")]
    folders, _ = bridge.changed_folders(records, _cur(0))
    assert set(folders) == {"/music/A/Old Album", "/music/A/New Album"}


def test_non_file_events_advance_the_cursor_without_reporting_paths():
    records = [
        {"id": 20, "date": "2026-09-01T14:13:32Z", "eventType": "downloadImported", "data": {}},
        {"id": 21, "date": "2026-09-01T14:14:00Z", "eventType": "grabbed", "data": {}},
    ]
    folders, cursor = bridge.changed_folders(records, _cur(19))
    assert folders == []
    assert cursor.high_water_id == 21


def test_cursor_is_unchanged_when_nothing_is_new():
    folders, cursor = bridge.changed_folders([], _cur(77))
    assert folders == []
    assert cursor.high_water_id == 77


def test_folders_come_back_oldest_first_by_id_not_input_order():
    records = [
        _imported("2026-09-01T12:00:00Z", "/music/B/x/01.mp3", rid=31),
        _imported("2026-09-01T11:00:00Z", "/music/A/x/01.mp3", rid=30),
    ]
    folders, _ = bridge.changed_folders(records, _cur(29))
    assert folders == ["/music/A/x", "/music/B/x"]


def test_a_date_only_cursor_still_works_for_the_v0_migration():
    """The pre-2026-09-04 state file has no id; one run must still make progress."""
    records = [_imported("2026-09-01T12:00:00Z", "/music/A/x/01.mp3", rid=42)]
    folders, cursor = bridge.changed_folders(records, bridge.Cursor(None, "2026-09-01T00:00:00Z"))
    assert folders == ["/music/A/x"]
    assert cursor.high_water_id == 42  # upgraded in place


def test_v0_migration_seeds_the_id_from_already_covered_records():
    """Otherwise high_water_id stays null and date comparison keeps being used."""
    records = [
        _imported("2026-09-01T09:00:00Z", "/music/Old/x/01.mp3", rid=40),  # covered by date
        _imported("2026-09-01T12:00:00Z", "/music/New/x/01.mp3", rid=41),  # new
    ]
    folders, cursor = bridge.changed_folders(records, bridge.Cursor(None, "2026-09-01T10:00:00Z"))
    assert folders == ["/music/New/x"]
    assert cursor.high_water_id == 41


def test_v0_migration_with_no_new_records_still_adopts_an_id():
    records = [_imported("2026-09-01T09:00:00Z", "/music/Old/x/01.mp3", rid=40)]
    folders, cursor = bridge.changed_folders(records, bridge.Cursor(None, "2026-09-01T10:00:00Z"))
    assert folders == []
    assert cursor.high_water_id == 40


# --- translate across the /music -> /data/music repath (ADR-0003) ---


def test_translate_accepts_several_media_roots():
    """Lidarr's root moved /music -> /data/music; history holds both spellings."""
    got = bridge.translate(
        ["/data/music/Kraftwerk/1974 - Autobahn", "/music/Bathory/1988 - Blood Fire Death"],
        ["/data/music", "/music"],
        "/data/movies/music",
    )
    assert got == [
        "/data/movies/music/Kraftwerk/1974 - Autobahn",
        "/data/movies/music/Bathory/1988 - Blood Fire Death",
    ]


def test_translate_matches_the_longest_root_first():
    """/data would otherwise swallow /data/music and emit .../music/music/..."""
    got = bridge.translate(["/data/music/A/B"], ["/data", "/data/music"], "/data/movies/music")
    assert got == ["/data/movies/music/A/B"]


def test_default_roots_cover_both_sides_of_the_repath():
    assert set(bridge.DEFAULT_MAP_FROM) == {"/data/music", "/music"}


def test_translate_still_accepts_a_single_root_as_a_string():
    got = bridge.translate(["/music/A/B"], "/music", "/data/movies/music")
    assert got == ["/data/movies/music/A/B"]


# --- an unknown media root must be loud, not a silent exit 0 ---


def _stub_run(monkeypatch, tmp_path, folders, reported, state=None):
    monkeypatch.setenv("API_KEY_LIDARR", "k")
    monkeypatch.setenv("API_KEY_JELLYFIN", "k")
    monkeypatch.setattr(bridge, "fetch_history", lambda *a, **k: [])
    monkeypatch.setattr(
        bridge, "changed_folders", lambda *a, **k: (folders, bridge.Cursor(99, "2026-09-03T12:18:03Z"))
    )
    monkeypatch.setattr(
        bridge, "report_to_jellyfin", lambda *a, **k: reported.append(a[-1]) or True
    )
    path = state if state is not None else tmp_path / "s.json"
    if not path.exists():
        bridge.save_state(path, bridge.Cursor(1, "2026-09-01T00:00:00Z"))
    return ["--state", str(path)]


def test_unknown_media_root_exits_fatal_so_cron_alerts(monkeypatch, tmp_path):
    """cron_job.py treats 0 and 1 as fine, so a dropped album must exit 2."""
    reported: list = []
    argv = _stub_run(monkeypatch, tmp_path, ["/somewhere/else/A/B"], reported)
    assert bridge.main(argv) == 2
    assert reported == []


def test_unknown_media_root_does_not_advance_the_cursor(monkeypatch, tmp_path):
    """Advancing past a dropped album loses it forever; the next run must retry."""
    reported: list = []
    state = tmp_path / "s.json"
    argv = _stub_run(monkeypatch, tmp_path, ["/somewhere/else/A/B"], reported, state=state)
    bridge.main(argv)
    assert bridge.load_state(state, 30, bootstrap=False).high_water_id == 1


def test_a_fully_translatable_batch_still_exits_clean(monkeypatch, tmp_path):
    reported: list = []
    argv = _stub_run(monkeypatch, tmp_path, ["/data/music/A/B"], reported)
    assert bridge.main(argv) == 0
    assert reported == [["/data/movies/music/A/B"]]


def test_a_clean_batch_commits_the_new_cursor(monkeypatch, tmp_path):
    reported: list = []
    state = tmp_path / "s.json"
    argv = _stub_run(monkeypatch, tmp_path, ["/data/music/A/B"], reported, state=state)
    assert bridge.main(argv) == 0
    assert bridge.load_state(state, 30, bootstrap=False).high_water_id == 99


# --- the state file must never be guessed (all four were exit 0 before) ---


def test_absent_state_file_is_fatal_not_a_silent_lookback(tmp_path):
    with pytest.raises(bridge.StateError, match="does not exist"):
        bridge.load_state(tmp_path / "nope.json", 30, bootstrap=False)


def test_absent_state_file_is_allowed_only_when_bootstrapping(tmp_path):
    cursor = bridge.load_state(tmp_path / "nope.json", 30, bootstrap=True)
    assert cursor.high_water_id is None
    assert cursor.date.endswith("Z")


def test_truncated_state_file_is_fatal(tmp_path):
    """Exactly what a non-atomic write_text leaves behind on a crash."""
    state = tmp_path / "s.json"
    state.write_text('{"cursor": "2026-09-0')
    with pytest.raises(bridge.StateError, match="not valid JSON"):
        bridge.load_state(state, 30, bootstrap=False)


def test_empty_state_file_is_fatal(tmp_path):
    state = tmp_path / "s.json"
    state.write_text("")
    with pytest.raises(bridge.StateError, match="empty"):
        bridge.load_state(state, 30, bootstrap=False)


def test_future_schema_version_is_fatal(tmp_path):
    """Reading a newer format as a stale cursor would re-dispatch history."""
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"schema_version": 99, "high_water_id": 5, "cursor_date": "x"}))
    with pytest.raises(bridge.StateError, match="newer than"):
        bridge.load_state(state, 30, bootstrap=False)


def test_bootstrap_does_not_rescue_a_corrupt_file(tmp_path):
    """--bootstrap covers 'no file', never 'unreadable file'."""
    state = tmp_path / "s.json"
    state.write_text("{{{")
    with pytest.raises(bridge.StateError):
        bridge.load_state(state, 30, bootstrap=True)


def test_v0_date_only_state_file_is_read_not_rejected(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({"cursor": "2026-09-04T07:57:19Z"}))
    cursor = bridge.load_state(state, 30, bootstrap=False)
    assert cursor == bridge.Cursor(None, "2026-09-04T07:57:19Z")


def test_cursor_renders_its_id_or_falls_back_to_its_date():
    assert str(bridge.Cursor(848483, "2026-09-04T07:57:19Z")) == "id=848483"
    assert str(bridge.Cursor(None, "2026-09-04T07:57:19Z")) == "date=2026-09-04T07:57:19Z"


def test_state_round_trips_and_upgrades_to_the_current_schema(tmp_path):
    state = tmp_path / "s.json"
    bridge.save_state(state, bridge.Cursor(848483, "2026-09-04T07:57:19Z"))
    assert json.loads(state.read_text())["schema_version"] == bridge.STATE_SCHEMA_VERSION
    assert bridge.load_state(state, 30, bootstrap=False) == bridge.Cursor(
        848483, "2026-09-04T07:57:19Z"
    )


def test_save_state_leaves_no_temp_file_behind(tmp_path):
    state = tmp_path / "s.json"
    bridge.save_state(state, bridge.Cursor(1, "2026-09-01T00:00:00Z"))
    assert [f.name for f in tmp_path.iterdir()] == ["s.json"]


# --- running out of pages must not skip the gap ---


def _paged(monkeypatch, pages):
    """Stub _get_json so fetch_history sees `pages` full pages and never the cursor."""
    calls = {"n": 0}

    def fake(url, headers, timeout=60):
        calls["n"] += 1
        page = int(url.split("page=")[1].split("&")[0])
        if page > pages:
            return {"records": []}
        base = 100000 - (page - 1) * bridge.HISTORY_PAGE_SIZE
        return {
            "records": [
                {"id": base - i, "date": "2026-09-04T00:00:00Z", "eventType": "grabbed", "data": {}}
                for i in range(bridge.HISTORY_PAGE_SIZE)
            ]
        }

    monkeypatch.setattr(bridge, "_get_json", fake)
    return calls


def test_exhausting_the_page_cap_raises_instead_of_returning_a_partial_set(monkeypatch):
    """The old code warned to stderr, dispatched 2000 records and advanced past the rest."""
    _paged(monkeypatch, pages=bridge.MAX_HISTORY_PAGES + 5)
    with pytest.raises(bridge.HistoryExhausted):
        bridge.fetch_history("http://x", "k", bridge.Cursor(1, "2020-01-01T00:00:00Z"))


def test_exhaustion_is_exit_2_and_holds_the_cursor(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY_LIDARR", "k")
    monkeypatch.setenv("API_KEY_JELLYFIN", "k")
    state = tmp_path / "s.json"
    bridge.save_state(state, bridge.Cursor(1, "2020-01-01T00:00:00Z"))
    _paged(monkeypatch, pages=bridge.MAX_HISTORY_PAGES + 5)
    assert bridge.main(["--state", str(state)]) == 2
    assert bridge.load_state(state, 30, bootstrap=False).high_water_id == 1


def test_reaching_the_cursor_inside_the_cap_returns_normally(monkeypatch):
    _paged(monkeypatch, pages=bridge.MAX_HISTORY_PAGES + 5)
    # page 1 covers ids 100000..99801, page 2 covers 99800..99601. A cursor of
    # 99700 is inside page 2, so page 2 is the first to hold a non-new record
    # and fetching must stop there -- two pages, not the whole cap.
    got = bridge.fetch_history("http://x", "k", bridge.Cursor(99700, "2026-09-04T00:00:00Z"))
    assert len(got) == 2 * bridge.HISTORY_PAGE_SIZE


def test_exhaustion_alert_states_the_backlog_age_and_the_remedy(tmp_path):
    """An alert that repeats with no remedy in it gets muted, which is failure #3."""
    exc = bridge.HistoryExhausted(
        bridge.Cursor(800000, "2026-07-01T00:00:00Z"), 848483, "2026-09-01T11:56:37Z"
    )
    assert exc.backlog() == 48483
    assert exc.age_hours() > 1000
    text = exc.remedy(tmp_path / "s.json")
    assert "records behind  : 48483" in text
    assert "--since-min" in text and str(tmp_path / "s.json") in text
    assert "jellyfin_library_scan.py --library Music" in text


def test_exhaustion_alert_degrades_gracefully_without_an_id():
    """A v0 cursor has no id, so the backlog cannot be counted -- say so."""
    exc = bridge.HistoryExhausted(bridge.Cursor(None, "2026-07-01T00:00:00Z"), None, "?")
    assert exc.backlog() is None
    assert "unknown" in exc.remedy(None)


def test_exhaustion_alert_survives_an_unparseable_cursor_date():
    exc = bridge.HistoryExhausted(bridge.Cursor(1, "not-a-date"), 5, "?")
    assert exc.age_hours() is None
    assert "This will not clear on its own" in exc.remedy(None)


def test_a_mid_run_lidarr_failure_is_not_a_partial_success(monkeypatch):
    """Returning what was collected so far is the same silent truncation."""

    def fake(url, headers, timeout=60):
        page = int(url.split("page=")[1].split("&")[0])
        if page == 1:
            return {"records": [{"id": 9, "date": "2026-09-04T00:00:00Z", "eventType": "grabbed"}]}
        return None

    monkeypatch.setattr(bridge, "_get_json", fake)
    assert bridge.fetch_history("http://x", "k", bridge.Cursor(1, "2020-01-01T00:00:00Z")) is None
