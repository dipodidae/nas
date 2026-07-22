"""Tests for scripts/album_art.py — pure-logic unit tests + mocked subprocess."""

from __future__ import annotations

import importlib.util
import sys
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
        total=10,
        n_marked=4,
        n_overwrite=3,
        n_gap=3,
        n_batch=5,
        n_deferred=1,
        sample=[tmp_path / "x"],
        cover_filename="folder.jpg",
    )
    assert "10" in out and "4" in out  # total + marked
    assert "overwrite" in out.lower()
    assert "defer" in out.lower()
    assert str(tmp_path / "x") in out


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
