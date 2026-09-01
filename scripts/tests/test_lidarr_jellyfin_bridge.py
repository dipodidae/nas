"""Tests for scripts/lidarr_jellyfin_bridge.py — pure-logic, no network."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


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


def _imported(date, path):
    return {"date": date, "eventType": "trackFileImported", "data": {"importedPath": path}}


def _renamed(date, old, new):
    return {"date": date, "eventType": "trackFileRenamed", "data": {"path": new, "sourcePath": old}}


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
    records = [_imported("2026-09-01T14:13:31Z", "/music/Tensal/2025 - X/01 - Aczio.mp3")]
    folders, cursor = bridge.changed_folders(records, "2026-09-01T00:00:00Z")
    assert folders == ["/music/Tensal/2025 - X"]
    assert cursor == "2026-09-01T14:13:31Z"


def test_deduplicates_the_ten_tracks_of_one_album():
    records = [
        _imported("2026-09-01T14:13:31Z", f"/music/A/2025 - B/{n:02d} - t.mp3") for n in range(1, 11)
    ]
    folders, _ = bridge.changed_folders(records, "2026-09-01T00:00:00Z")
    assert folders == ["/music/A/2025 - B"]


def test_records_at_or_before_the_cursor_are_skipped():
    records = [
        _imported("2026-09-01T10:00:00Z", "/music/Old/2020 - X/01.mp3"),
        _imported("2026-09-01T12:00:00Z", "/music/New/2021 - Y/01.mp3"),
    ]
    folders, cursor = bridge.changed_folders(records, "2026-09-01T10:00:00Z")
    assert folders == ["/music/New/2021 - Y"]
    assert cursor == "2026-09-01T12:00:00Z"


def test_rename_reports_both_source_and_destination_folders():
    """A rename across folders leaves stale metadata behind unless both refresh."""
    records = [_renamed("2026-09-01T16:47:55Z", "/music/A/Old Album/09.mp3", "/music/A/New Album/09.mp3")]
    folders, _ = bridge.changed_folders(records, "2026-09-01T00:00:00Z")
    assert set(folders) == {"/music/A/Old Album", "/music/A/New Album"}


def test_non_file_events_advance_the_cursor_without_reporting_paths():
    records = [
        {"date": "2026-09-01T14:13:32Z", "eventType": "downloadImported", "data": {}},
        {"date": "2026-09-01T14:14:00Z", "eventType": "grabbed", "data": {}},
    ]
    folders, cursor = bridge.changed_folders(records, "2026-09-01T00:00:00Z")
    assert folders == []
    assert cursor == "2026-09-01T14:14:00Z"


def test_cursor_is_unchanged_when_nothing_is_new():
    folders, cursor = bridge.changed_folders([], "2026-09-01T10:00:00Z")
    assert folders == []
    assert cursor == "2026-09-01T10:00:00Z"


def test_folders_come_back_oldest_first():
    records = [
        _imported("2026-09-01T12:00:00Z", "/music/B/x/01.mp3"),
        _imported("2026-09-01T11:00:00Z", "/music/A/x/01.mp3"),
    ]
    folders, _ = bridge.changed_folders(records, "2026-09-01T00:00:00Z")
    assert folders == ["/music/A/x", "/music/B/x"]
