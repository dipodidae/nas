"""Unit tests for the shared import-acceptance policy (lidarr_import_lib)."""

import importlib.util
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "lidarr_import_lib.py"
    spec = importlib.util.spec_from_file_location("lidarr_import_lib", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


lib = _load_module()


# --- classify_reasons: the queue-salvage policy (strict) -------------------

QUEUE = dict(accept_missing_tracks=False, block_fewer_tracks=True, accept_min_match=70.0)


def test_edition_mismatch_is_salvageable():
    ok, blockers = lib.classify_reasons(["Album release not requested"], **QUEUE)
    assert ok is True
    assert blockers == []


def test_unmatched_tracks_is_salvageable():
    ok, _ = lib.classify_reasons(["Has unmatched tracks"], **QUEUE)
    assert ok is True


def test_close_match_at_or_above_floor_salvageable():
    ok, _ = lib.classify_reasons(
        ["Album match is not close enough: 72.0 % vs 80 % [album]"], **QUEUE
    )
    assert ok is True


def test_close_match_below_floor_blocked():
    ok, blockers = lib.classify_reasons(
        ["Album match is not close enough: 64.6 % vs 80 % [album, year]"], **QUEUE
    )
    assert ok is False
    assert blockers and "64.6" in blockers[0]


def test_missing_tracks_blocked_for_queue():
    ok, _ = lib.classify_reasons(["Has missing tracks"], **QUEUE)
    assert ok is False


def test_fewer_tracks_blocked_for_queue():
    ok, _ = lib.classify_reasons(["Has fewer tracks than existing release"], **QUEUE)
    assert ok is False


def test_hard_blockers_always_block():
    for reason in (
        "Not an upgrade for existing album file(s)",
        "Couldn't find similar album for [/downloads/x]",
        "Destination already exists",
    ):
        ok, _ = lib.classify_reasons([reason], **QUEUE)
        assert ok is False, reason


def test_compound_edition_plus_blocker_blocks():
    ok, _ = lib.classify_reasons(
        ["Album release not requested", "Has missing tracks"], **QUEUE
    )
    assert ok is False


def test_unknown_reason_does_not_block():
    ok, _ = lib.classify_reasons(["Some novel future reason"], **QUEUE)
    assert ok is True


# --- classify_reasons: the orphan-importer policy (lenient, preserved) -----

ORPHAN = dict(accept_missing_tracks=True, block_fewer_tracks=False, accept_min_match=80.0)


def test_orphan_policy_accepts_missing_tracks():
    ok, _ = lib.classify_reasons(["Has missing tracks"], **ORPHAN)
    assert ok is True


def test_orphan_policy_floor_is_80():
    ok, _ = lib.classify_reasons(
        ["Album match is not close enough: 72.0 % vs 80 %"], **ORPHAN
    )
    assert ok is False  # 72 < 80


# --- build_import_item -----------------------------------------------------

def _good_entry():
    return {
        "path": "/downloads/complete/slskd/X",
        "artist": {"id": 5, "artistName": "A"},
        "album": {"id": 9, "title": "T", "releases": [{"id": 3, "trackCount": 10}]},
        "albumReleaseId": 3,
        "tracks": [{"id": 1}, {"id": 2}],
        "quality": {"q": 1},
    }


def test_build_import_item_ok():
    item = lib.build_import_item(_good_entry())
    assert item is not None
    assert item["artistId"] == 5 and item["albumId"] == 9
    assert item["trackIds"] == [1, 2]
    assert item["disableReleaseSwitching"] is False


def test_build_import_item_missing_ids_returns_none():
    e = _good_entry()
    e["album"] = {}
    assert lib.build_import_item(e) is None


def test_build_import_item_no_track_ids_returns_none():
    e = _good_entry()
    e["tracks"] = [{}, {}]
    assert lib.build_import_item(e) is None


# --- stub_coverage ---------------------------------------------------------

def test_stub_coverage_partial():
    imported, total, frac = lib.stub_coverage({3: 2}, {3: 10})
    assert (imported, total) == (2, 10)
    assert abs(frac - 0.2) < 1e-9


def test_stub_coverage_unknown_release_size_never_blocks():
    _, total, frac = lib.stub_coverage({3: 2}, {3: 0})
    assert total == 0 and frac == 1.0


# --- select_importable_items ----------------------------------------------

def test_select_accepts_edition_mismatch():
    e = _good_entry()
    # complete album: 2 tracks present, release is a 2-track release.
    e["album"]["releases"] = [{"id": 3, "trackCount": 2}]
    e["rejections"] = [{"reason": "Album release not requested"}]
    items, stub = lib.select_importable_items([e])
    assert stub is None
    assert len(items) == 1


def test_select_rejects_below_floor():
    e = _good_entry()
    e["rejections"] = [{"reason": "Album match is not close enough: 50 % vs 80 %"}]
    items, stub = lib.select_importable_items([e])
    assert items == [] and stub is None


def test_select_stub_guard_blocks_incomplete():
    # 2 importable tracks of a known 10-track release => 20% < 50% floor.
    e = _good_entry()
    e["rejections"] = []  # no rejection, but only 2 of 10 tracks present
    items, stub = lib.select_importable_items([e], min_track_fraction=0.5)
    assert items == []
    assert stub is not None and "stub" in stub
