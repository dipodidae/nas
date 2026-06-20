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
