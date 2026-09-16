"""Tests for scripts/album_art.py — pure-logic unit tests + mocked subprocess."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "album_art.py"
    spec = importlib.util.spec_from_file_location("album_art", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


aa = _load_module()
AUDIO_EXTS = aa.AUDIO_EXTENSIONS


# --- discover_album_dirs ---


def test_discover_finds_album(tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "t.flac").touch()
    assert aa.discover_album_dirs(tmp_path, AUDIO_EXTS) == [album]


def test_discover_ignores_non_audio(tmp_path):
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    (d / "folder.jpg").touch()
    (d / "info.txt").touch()
    assert aa.discover_album_dirs(tmp_path, AUDIO_EXTS) == []


# --- dirs_missing_cover ---


def test_dirs_missing_cover_splits(tmp_path):
    have = tmp_path / "Have"
    have.mkdir()
    (have / "folder.jpg").touch()
    miss = tmp_path / "Miss"
    miss.mkdir()
    result = aa.dirs_missing_cover([have, miss], "folder.jpg")
    assert result == [miss]


def test_dirs_missing_cover_all_present(tmp_path):
    d = tmp_path / "A"
    d.mkdir()
    (d / "folder.jpg").touch()
    assert aa.dirs_missing_cover([d], "folder.jpg") == []


def test_dirs_missing_cover_respects_custom_name(tmp_path):
    d = tmp_path / "A"
    d.mkdir()
    (d / "folder.jpg").touch()
    # cover name is cover.jpg -> folder.jpg does not satisfy it
    assert aa.dirs_missing_cover([d], "cover.jpg") == [d]


# --- build_sacad_cmd ---


def test_build_cmd_defaults(tmp_path):
    cfg = aa.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        size=1000,
        cover_filename="folder.jpg",
        ignore_existing=False,
        overwrite_once=False,
        limit=aa.DEFAULT_LIMIT,
        marker_filename=aa.DEFAULT_MARKER_FILENAME,
    )
    assert aa.build_sacad_cmd(cfg) == ["sacad_r", str(tmp_path), "1000", "folder.jpg"]


def test_build_cmd_ignore_existing(tmp_path):
    cfg = aa.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        size=600,
        cover_filename="cover.jpg",
        ignore_existing=True,
        overwrite_once=False,
        limit=aa.DEFAULT_LIMIT,
        marker_filename=aa.DEFAULT_MARKER_FILENAME,
    )
    cmd = aa.build_sacad_cmd(cfg)
    assert cmd == ["sacad_r", "-i", str(tmp_path), "600", "cover.jpg"]


# --- summarize_plan ---


def test_summarize_empty():
    assert "No album" in aa.summarize_plan([], [], "folder.jpg")


def test_summarize_counts(tmp_path):
    dirs = [tmp_path / f"A{i}" for i in range(5)]
    missing = dirs[:2]
    out = aa.summarize_plan(dirs, missing, "folder.jpg")
    assert "5 album" in out
    assert "3 already have folder.jpg" in out
    assert "2 missing folder.jpg" in out


def test_summarize_truncates(tmp_path):
    dirs = [tmp_path / f"A{i:02d}" for i in range(20)]
    out = aa.summarize_plan(dirs, dirs, "folder.jpg", sample_n=5)
    assert "more" in out
    assert str(dirs[0]) in out
    assert str(dirs[10]) not in out


# --- parse_args / _resolve_config ---


def test_dry_run_is_default(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path)]))
    assert cfg.dry_run is True and cfg.apply is False


def test_apply_disables_dry_run(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path), "--apply"]))
    assert cfg.apply is True and cfg.dry_run is False


def test_size_and_filename(tmp_path):
    cfg = aa._resolve_config(
        aa.parse_args(["--music-dir", str(tmp_path), "--size", "600", "--filename", "cover.jpg"])
    )
    assert cfg.size == 600 and cfg.cover_filename == "cover.jpg"


def test_resolve_config_defaults(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path)]))
    assert cfg.size == aa.DEFAULT_SIZE
    assert cfg.cover_filename == aa.DEFAULT_COVER_FILENAME
    assert cfg.ignore_existing is False


# --- main: exit codes + side effects (sacad_r mocked) ---


def _album(tmp_path, name, with_cover=False):
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "t.flac").touch()
    if with_cover:
        (d / "folder.jpg").touch()
    return d


def test_main_dry_run_never_calls_sacad(tmp_path):
    _album(tmp_path, "Miss")
    with mock.patch.object(aa.subprocess, "run") as run:
        rc = aa.main(["--music-dir", str(tmp_path)])
    assert rc == 0
    run.assert_not_called()


def test_main_apply_missing_sacad_exits_2(tmp_path):
    _album(tmp_path, "Miss")
    with mock.patch.object(aa.shutil, "which", return_value=None):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 2


def test_main_apply_invokes_sacad_and_maps_success(tmp_path):
    _album(tmp_path, "Miss")
    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 0
    run.assert_called_once()
    assert run.call_args.args[0][0] == "sacad_r"


def test_main_apply_maps_nonzero_to_partial(tmp_path):
    _album(tmp_path, "Miss")
    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=3)),
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 1


def test_main_apply_nothing_missing_skips_sacad(tmp_path):
    _album(tmp_path, "Have", with_cover=True)
    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run") as run,
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 0
    run.assert_not_called()


def test_main_missing_music_dir_exits_2(tmp_path):
    rc = aa.main(["--music-dir", str(tmp_path / "nope"), "--apply"])
    assert rc == 2


# --- overwrite-once config plumbing ---


def test_overwrite_once_defaults_off(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path)]))
    assert cfg.overwrite_once is False
    assert cfg.limit == aa.DEFAULT_LIMIT
    assert cfg.marker_filename == aa.DEFAULT_MARKER_FILENAME


def test_overwrite_once_flag_and_limit(tmp_path):
    cfg = aa._resolve_config(
        aa.parse_args(
            ["--music-dir", str(tmp_path), "--apply", "--overwrite-once", "--limit", "50"]
        )
    )
    assert cfg.overwrite_once is True
    assert cfg.apply is True
    assert cfg.limit == 50


def test_marker_override(tmp_path):
    cfg = aa._resolve_config(
        aa.parse_args(["--music-dir", str(tmp_path), "--marker", ".done"])
    )
    assert cfg.marker_filename == ".done"


# --- overwrite-once pure helpers ---


def test_dir_is_marked(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    assert aa.dir_is_marked(d, ".album_art_done") is False
    (d / ".album_art_done").touch()
    assert aa.dir_is_marked(d, ".album_art_done") is True


def test_partition_by_marker(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / ".album_art_done").touch()
    marked, unmarked = aa.partition_by_marker([a, b], ".album_art_done")
    assert marked == [a]
    assert unmarked == [b]


def test_select_batch_caps(tmp_path):
    dirs = [tmp_path / str(i) for i in range(5)]
    batch, deferred = aa.select_batch(dirs, 2)
    assert batch == dirs[:2]
    assert deferred == dirs[2:]


def test_select_batch_no_cap(tmp_path):
    dirs = [tmp_path / str(i) for i in range(3)]
    assert aa.select_batch(dirs, 0) == (dirs, [])
    assert aa.select_batch(dirs, -1) == (dirs, [])


def test_build_overwrite_cmd(tmp_path):
    assert aa.build_overwrite_cmd(tmp_path, 1000, "folder.jpg") == [
        "sacad_r",
        "-i",
        str(tmp_path),
        "1000",
        "folder.jpg",
    ]


def test_summarize_overwrite_plan_counts(tmp_path):
    out = aa.summarize_overwrite_plan(
        counts={"total": 10, "done": 4, "overwrite": 3, "gap": 3, "cooling": 0},
        n_batch=5,
        n_deferred=1,
        sample=[aa.Candidate(tmp_path / "x", "gap")],
        cover_filename="folder.jpg",
    )
    assert "10" in out and "4" in out  # total + settled
    assert "overwrite" in out.lower()
    assert "defer" in out.lower()
    assert str(tmp_path / "x") in out


def test_summarize_plan_reports_why_a_run_is_quiet(tmp_path):
    """A run that does nothing must name the bucket that swallowed the work."""
    out = aa.summarize_overwrite_plan(
        counts={"total": 900, "done": 0, "overwrite": 0, "gap": 0, "cooling": 900},
        n_batch=0,
        n_deferred=0,
        sample=[],
        cover_filename="folder.jpg",
    )
    assert "900" in out
    assert "cooldown" in out.lower()


# --- overwrite-once main() behaviour ---


def _make_album(tmp_path, name, *, cover=False, marker=False):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "track.flac").touch()
    if cover:
        (d / "folder.jpg").write_bytes(b"old")
    if marker:
        (d / ".album_art_done").touch()
    return d


def test_overwrite_once_skips_marked(tmp_path):
    done = _make_album(tmp_path, "Marked", cover=True, marker=True)
    fresh = _make_album(tmp_path, "Fresh", cover=True)

    def fake_run(cmd, check=False):
        target = Path(cmd[2])
        (target / "folder.jpg").write_bytes(b"new")
        return mock.Mock(returncode=0)

    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", side_effect=fake_run) as run,
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 0
    called_dirs = {Path(c.args[0][2]) for c in run.call_args_list}
    assert called_dirs == {fresh}
    assert (done / ".album_art_done").exists()
    assert (fresh / ".album_art_done").exists()


def test_overwrite_once_no_source_keeps_art_and_marks(tmp_path):
    album = _make_album(tmp_path, "BadArt", cover=True)  # sacad finds nothing

    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)),
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 0
    assert (album / "folder.jpg").read_bytes() == b"old"  # never blanked
    assert (album / ".album_art_done").exists()  # attempt spent -> marked


def test_overwrite_once_unfilled_gap_not_marked(tmp_path):
    gap = _make_album(tmp_path, "Obscure")  # no cover, sacad finds nothing

    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)),
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 0
    assert not (gap / "folder.jpg").exists()
    assert not (gap / ".album_art_done").exists()  # stays unmarked -> retried


def test_overwrite_once_limit_bounds_calls(tmp_path):
    for i in range(4):
        _make_album(tmp_path, f"A{i}", cover=True)

    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
    ):
        rc = aa.main(
            ["--music-dir", str(tmp_path), "--apply", "--overwrite-once", "--limit", "2"]
        )

    assert rc == 0
    assert run.call_count == 2


def test_overwrite_once_partial_exit_on_failure(tmp_path):
    _make_album(tmp_path, "A", cover=True)

    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=3)),
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 1  # per-folder sacad_r non-zero -> partial


def test_overwrite_once_dry_run_no_calls(tmp_path):
    _make_album(tmp_path, "A", cover=True)
    with mock.patch.object(aa.subprocess, "run") as run:
        rc = aa.main(["--music-dir", str(tmp_path), "--overwrite-once"])
    assert rc == 0
    run.assert_not_called()


# --- sidecar state: the starvation fix (2026-09-16) ---


def test_read_sidecar_absent_is_empty(tmp_path):
    assert aa.read_sidecar(tmp_path / "nope") == {}


def test_read_sidecar_legacy_text_is_v1(tmp_path):
    """Pre-2026-09-16 markers hold one line of prose, not JSON."""
    p = tmp_path / ".album_art_done"
    p.write_text("album_art.py overwrite-once size=1000 cover=folder.jpg\n")
    assert aa.read_sidecar(p) == {"v": 1}


def test_read_sidecar_truncated_json_is_v1_not_empty(tmp_path):
    """A half-written sidecar must not read as 'nothing ever happened here'."""
    p = tmp_path / ".album_art_none"
    p.write_text('{"v": 2, "attempts": 3, "ts": "2026-0')
    assert aa.read_sidecar(p) == {"v": 1}


def test_write_sidecar_is_atomic_and_leaves_no_temp(tmp_path):
    p = tmp_path / ".album_art_none"
    aa.write_sidecar(p, {"v": 2, "attempts": 1})
    assert aa.read_sidecar(p) == {"v": 2, "attempts": 1}
    assert list(tmp_path.iterdir()) == [p]


def test_cooldown_escalates_then_plateaus():
    assert aa.cooldown_days(0) == 0
    assert aa.cooldown_days(1) == 7
    assert aa.cooldown_days(2) == 30
    assert aa.cooldown_days(3) == 90
    assert aa.cooldown_days(99) == 90


def test_miss_is_cooling_inside_window():
    now = datetime(2026, 9, 16, tzinfo=UTC)
    payload = {"attempts": 1, "ts": datetime(2026, 9, 14, tzinfo=UTC).isoformat()}
    assert aa.miss_is_cooling(payload, now) is True


def test_miss_is_not_cooling_after_window():
    now = datetime(2026, 9, 16, tzinfo=UTC)
    payload = {"attempts": 1, "ts": datetime(2026, 9, 1, tzinfo=UTC).isoformat()}
    assert aa.miss_is_cooling(payload, now) is False


def test_miss_with_unreadable_timestamp_retries_now():
    """The guard fails OPEN: a corrupt sidecar must not retire an album forever."""
    now = datetime(2026, 9, 16, tzinfo=UTC)
    assert aa.miss_is_cooling({"attempts": 1, "ts": "not-a-date"}, now) is False
    assert aa.miss_is_cooling({"attempts": 1}, now) is False


# --- needs_upgrade: the anti-churn condition ---


def test_needs_upgrade_true_for_small_cover_never_asked_bigger():
    assert aa.needs_upgrade({"v": 2, "target": 400}, 300, 600) is True


def test_needs_upgrade_false_when_source_ceiling_already_probed():
    """400px because no source has better -> never re-fetch it again."""
    assert aa.needs_upgrade({"v": 2, "target": 1000}, 400, 600) is False


def test_needs_upgrade_true_for_legacy_marker_with_unknown_target():
    assert aa.needs_upgrade({"v": 1}, 400, 600) is True


def test_needs_upgrade_false_when_cover_is_big_enough():
    assert aa.needs_upgrade({"v": 1}, 900, 600) is False


def test_needs_upgrade_disabled_at_zero():
    assert aa.needs_upgrade({"v": 1}, 100, 0) is False


# --- classify_dirs ---


def _classify(dirs, **kw):
    kw.setdefault("cover_filename", "folder.jpg")
    kw.setdefault("marker_filename", ".album_art_done")
    kw.setdefault("miss_filename", ".album_art_none")
    kw.setdefault("upgrade_below", 0)
    kw.setdefault("now", datetime(2026, 9, 16, tzinfo=UTC))
    kw.setdefault("width_of", lambda p: 400)
    return aa.classify_dirs(dirs, **kw)


def test_classify_gap_and_overwrite_and_done(tmp_path):
    gap = _make_album(tmp_path, "Gap")
    over = _make_album(tmp_path, "Over", cover=True)
    done = _make_album(tmp_path, "Done", cover=True, marker=True)
    cands, counts = _classify([gap, over, done])
    assert [(c.path, c.kind) for c in cands] == [(gap, "gap"), (over, "overwrite")]
    assert counts["done"] == 1
    assert counts["gap"] == 1 and counts["overwrite"] == 1


def test_classify_skips_a_cooling_miss(tmp_path):
    """The whole point: an unfindable album stops occupying a batch slot."""
    gap = _make_album(tmp_path, "Unfindable")
    aa.write_sidecar(
        gap / ".album_art_none",
        {"v": 2, "attempts": 1, "ts": datetime(2026, 9, 15, tzinfo=UTC).isoformat()},
    )
    cands, counts = _classify([gap])
    assert cands == []
    assert counts["cooling"] == 1


def test_classify_retries_a_miss_once_cooled(tmp_path):
    gap = _make_album(tmp_path, "Unfindable")
    aa.write_sidecar(
        gap / ".album_art_none",
        {"v": 2, "attempts": 1, "ts": datetime(2026, 1, 1, tzinfo=UTC).isoformat()},
    )
    cands, counts = _classify([gap])
    assert [c.kind for c in cands] == ["gap"]
    assert counts["cooling"] == 0


def test_classify_upgrade_uses_cached_width_not_the_image(tmp_path):
    """Width is read from the marker so a weekly run isn't 17k image opens."""
    d = _make_album(tmp_path, "Small", cover=True)
    aa.write_sidecar(d / ".album_art_done", {"v": 2, "target": 500, "w": 300})
    boom = mock.Mock(side_effect=AssertionError("must not open the image"))
    cands, counts = _classify([d], upgrade_below=600, width_of=boom)
    assert [c.kind for c in cands] == ["upgrade"]
    assert counts["upgrade"] == 1
    boom.assert_not_called()


def test_classify_upgrade_ignored_when_flag_off(tmp_path):
    d = _make_album(tmp_path, "Small", cover=True)
    aa.write_sidecar(d / ".album_art_done", {"v": 2, "target": 500, "w": 300})
    cands, counts = _classify([d], upgrade_below=0)
    assert cands == []
    assert counts["done"] == 1


# --- two-stage fetch + never-downgrade guard ---


def _cfg(tmp_path, **kw):
    base = dict(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        size=1000,
        cover_filename="folder.jpg",
        ignore_existing=False,
        overwrite_once=True,
        limit=aa.DEFAULT_LIMIT,
        marker_filename=aa.DEFAULT_MARKER_FILENAME,
    )
    base.update(kw)
    return aa.RunConfig(**base)


def test_build_overwrite_cmd_carries_tolerance(tmp_path):
    cmd = aa.build_overwrite_cmd(tmp_path, 500, "folder.jpg", tolerance=90)
    assert cmd == ["sacad_r", "-i", "-t", "90", str(tmp_path), "500", "folder.jpg"]


def test_fetch_skips_fallback_when_primary_lands_art(tmp_path):
    d = _make_album(tmp_path, "Found")

    def fake_run(cmd, **kw):
        (d / "folder.jpg").write_bytes(b"new")
        return mock.Mock(returncode=0)

    with (
        mock.patch.object(aa.subprocess, "run", side_effect=fake_run) as run,
        mock.patch.object(aa, "cover_width", return_value=1000),
    ):
        failed, asked, width = aa._fetch_cover(_cfg(tmp_path), d)
    assert (failed, asked, width) == (False, 1000, 1000)
    assert run.call_count == 1


def test_fetch_runs_relaxed_fallback_when_primary_finds_nothing(tmp_path):
    """The 750px floor is what made 80% of the 'unfindable' gap unfindable."""
    d = _make_album(tmp_path, "Small")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 2:  # the relaxed pass succeeds
            (d / "folder.jpg").write_bytes(b"new")
        return mock.Mock(returncode=0)

    with (
        mock.patch.object(aa.subprocess, "run", side_effect=fake_run),
        mock.patch.object(aa, "cover_width", return_value=440),
    ):
        failed, asked, width = aa._fetch_cover(_cfg(tmp_path), d)
    assert len(calls) == 2
    assert calls[1][:4] == ["sacad_r", "-i", "-t", "90"]
    assert calls[1][-2] == "500"
    assert (failed, asked, width) == (False, 1000, 440)


def test_process_records_a_miss_with_attempt_count(tmp_path):
    d = _make_album(tmp_path, "Nowhere")
    with mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)):
        failed, has_cover = aa._process_candidate(
            _cfg(tmp_path), aa.Candidate(d, "gap"), "2026-09-16T00:00:00+00:00"
        )
    assert has_cover is False
    assert not (d / ".album_art_done").exists()
    assert aa.read_sidecar(d / ".album_art_none")["attempts"] == 1


def test_process_bumps_attempts_on_a_repeat_miss(tmp_path):
    d = _make_album(tmp_path, "Nowhere")
    aa.write_sidecar(d / ".album_art_none", {"v": 2, "attempts": 2, "ts": "x"})
    with mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)):
        aa._process_candidate(
            _cfg(tmp_path), aa.Candidate(d, "gap"), "2026-09-16T00:00:00+00:00"
        )
    assert aa.read_sidecar(d / ".album_art_none")["attempts"] == 3


def test_process_restores_the_old_cover_when_the_new_one_is_smaller(tmp_path):
    """An upgrade sweep that makes art worse is worse than no sweep."""
    d = _make_album(tmp_path, "Big", cover=True)
    (d / "folder.jpg").write_bytes(b"ORIGINAL-1000px")

    def fake_run(cmd, **kw):
        (d / "folder.jpg").write_bytes(b"tiny")
        return mock.Mock(returncode=0)

    widths = {b"ORIGINAL-1000px": 1000, b"tiny": 200}
    with (
        mock.patch.object(aa.subprocess, "run", side_effect=fake_run),
        mock.patch.object(aa, "cover_width", side_effect=lambda p: widths[p.read_bytes()]),
    ):
        aa._process_candidate(
            _cfg(tmp_path, upgrade_below=1200),
            aa.Candidate(d, "upgrade", 1000),
            "2026-09-16T00:00:00+00:00",
        )
    assert (d / "folder.jpg").read_bytes() == b"ORIGINAL-1000px"
    assert not (d / "folder.jpg.prev").exists()
    assert aa.read_sidecar(d / ".album_art_done")["w"] == 1000


def test_process_keeps_the_new_cover_when_it_is_bigger(tmp_path):
    d = _make_album(tmp_path, "Small", cover=True)
    (d / "folder.jpg").write_bytes(b"old")

    def fake_run(cmd, **kw):
        (d / "folder.jpg").write_bytes(b"NEW")
        return mock.Mock(returncode=0)

    widths = {b"old": 300, b"NEW": 1000}
    with (
        mock.patch.object(aa.subprocess, "run", side_effect=fake_run),
        mock.patch.object(aa, "cover_width", side_effect=lambda p: widths[p.read_bytes()]),
    ):
        aa._process_candidate(
            _cfg(tmp_path, upgrade_below=600),
            aa.Candidate(d, "upgrade", 300),
            "2026-09-16T00:00:00+00:00",
        )
    assert (d / "folder.jpg").read_bytes() == b"NEW"
    marker = aa.read_sidecar(d / ".album_art_done")
    assert marker["w"] == 1000 and marker["target"] == 1000


def test_process_clears_a_stale_miss_sidecar_on_success(tmp_path):
    d = _make_album(tmp_path, "Late")
    aa.write_sidecar(d / ".album_art_none", {"v": 2, "attempts": 3, "ts": "x"})

    def fake_run(cmd, **kw):
        (d / "folder.jpg").write_bytes(b"new")
        return mock.Mock(returncode=0)

    with (
        mock.patch.object(aa.subprocess, "run", side_effect=fake_run),
        mock.patch.object(aa, "cover_width", return_value=900),
    ):
        aa._process_candidate(
            _cfg(tmp_path), aa.Candidate(d, "gap"), "2026-09-16T00:00:00+00:00"
        )
    assert not (d / ".album_art_none").exists()
    assert (d / ".album_art_done").exists()


# --- systemic-outage exit code ---


def test_whole_gap_batch_failing_exits_2(tmp_path):
    """Measured hit rate is ~80%; 0/20 is an outage, not an obscure library."""
    for i in range(25):
        _make_album(tmp_path, f"Gap{i:02d}")
    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)),
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])
    assert rc == 2


def test_a_healthy_gap_batch_exits_0(tmp_path):
    dirs = [_make_album(tmp_path, f"Gap{i:02d}") for i in range(25)]

    def fake_run(cmd, **kw):
        target = Path(cmd[-3])
        (target / "folder.jpg").write_bytes(b"art")
        return mock.Mock(returncode=0)

    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", side_effect=fake_run),
        mock.patch.object(aa, "cover_width", return_value=1000),
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])
    assert rc == 0
    assert all((d / ".album_art_done").exists() for d in dirs)


def test_classify_caches_a_measured_width_into_the_marker(tmp_path):
    """Measure 15k covers once, not once per week."""
    d = _make_album(tmp_path, "Legacy", cover=True)
    (d / ".album_art_done").write_text("album_art.py overwrite-once size=1000\n")
    cands, _ = _classify(
        [d], upgrade_below=600, width_of=lambda p: 420, cache_widths=True
    )
    assert [c.kind for c in cands] == ["upgrade"]
    assert aa.read_sidecar(d / ".album_art_done")["w"] == 420


def test_classify_does_not_write_when_caching_is_off(tmp_path):
    """--dry-run writes nothing, including the width cache."""
    d = _make_album(tmp_path, "Legacy", cover=True)
    (d / ".album_art_done").write_text("legacy\n")
    _classify([d], upgrade_below=600, width_of=lambda p: 420, cache_widths=False)
    assert aa.read_sidecar(d / ".album_art_done") == {"v": 1}
