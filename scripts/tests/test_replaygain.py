"""Tests for scripts/replaygain.py — pure-logic unit tests only (no I/O, no shelling out)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "replaygain.py"
    spec = importlib.util.spec_from_file_location("replaygain", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


rg = _load_module()

AUDIO_EXTS = rg.AUDIO_EXTENSIONS


# ---------------------------------------------------------------------------
# discover_album_dirs
# ---------------------------------------------------------------------------


def test_discover_finds_single_album(tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "track01.mp3").touch()
    (album / "track02.mp3").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert result == [album]


def test_discover_finds_multiple_albums(tmp_path):
    for name in ("AlbumA", "AlbumB", "AlbumC"):
        d = tmp_path / "Artist" / name
        d.mkdir(parents=True)
        (d / "track.flac").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert len(result) == 3
    assert all(isinstance(p, Path) for p in result)


def test_discover_ignores_non_audio_files(tmp_path):
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    (d / "cover.jpg").touch()
    (d / "info.txt").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert result == []


def test_discover_handles_mixed_extensions(tmp_path):
    d = tmp_path / "Mixed"
    d.mkdir()
    (d / "a.mp3").touch()
    (d / "b.flac").touch()
    (d / "c.opus").touch()
    (d / "d.jpg").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert result == [d]


def test_discover_includes_root_dir_with_audio(tmp_path):
    (tmp_path / "single.mp3").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert tmp_path in result


def test_discover_returns_sorted_list(tmp_path):
    for name in ("ZAlbum", "AAlbum", "MAlbum"):
        d = tmp_path / name
        d.mkdir()
        (d / "t.mp3").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert result == sorted(result)


def test_discover_empty_directory(tmp_path):
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert result == []


def test_discover_deeply_nested(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "track.ogg").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert result == [deep]


def test_discover_each_leaf_counted_once(tmp_path):
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    for i in range(10):
        (d / f"track{i:02d}.mp3").touch()
    result = rg.discover_album_dirs(tmp_path, AUDIO_EXTS)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# count_audio_files
# ---------------------------------------------------------------------------


def test_count_audio_files_basic(tmp_path):
    d = tmp_path / "Album"
    d.mkdir()
    (d / "t1.mp3").touch()
    (d / "t2.flac").touch()
    (d / "cover.jpg").touch()
    assert rg.count_audio_files([d], AUDIO_EXTS) == 2


def test_count_audio_files_multiple_dirs(tmp_path):
    dirs = []
    for name in ("A", "B"):
        d = tmp_path / name
        d.mkdir()
        (d / "x.mp3").touch()
        (d / "y.mp3").touch()
        dirs.append(d)
    assert rg.count_audio_files(dirs, AUDIO_EXTS) == 4


def test_count_audio_files_empty_dirs(tmp_path):
    d = tmp_path / "Empty"
    d.mkdir()
    assert rg.count_audio_files([d], AUDIO_EXTS) == 0


# ---------------------------------------------------------------------------
# build_rsgain_cmd
# ---------------------------------------------------------------------------


def test_build_rsgain_cmd_defaults(tmp_path):
    config = rg.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        jobs=1,
        skip_existing=False,
    )
    cmd = rg.build_rsgain_cmd(config)
    assert cmd == ["rsgain", "easy", str(tmp_path)]


def test_build_rsgain_cmd_with_jobs(tmp_path):
    config = rg.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        jobs=4,
        skip_existing=False,
    )
    cmd = rg.build_rsgain_cmd(config)
    assert "-m" in cmd
    assert "4" in cmd
    assert str(tmp_path) == cmd[-1]


def test_build_rsgain_cmd_skip_existing(tmp_path):
    config = rg.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        jobs=1,
        skip_existing=True,
    )
    cmd = rg.build_rsgain_cmd(config)
    assert "-s" in cmd
    assert "i" in cmd


def test_build_rsgain_cmd_all_options(tmp_path):
    config = rg.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        jobs=8,
        skip_existing=True,
    )
    cmd = rg.build_rsgain_cmd(config)
    assert cmd[0] == "rsgain"
    assert cmd[1] == "easy"
    assert "-m" in cmd
    assert "8" in cmd
    assert "-s" in cmd
    assert "i" in cmd
    assert cmd[-1] == str(tmp_path)


def test_build_rsgain_cmd_music_dir_is_last_arg(tmp_path):
    config = rg.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        jobs=2,
        skip_existing=True,
    )
    cmd = rg.build_rsgain_cmd(config)
    assert cmd[-1] == str(tmp_path)


# ---------------------------------------------------------------------------
# summarize_plan
# ---------------------------------------------------------------------------


def test_summarize_plan_empty():
    result = rg.summarize_plan([], 0)
    assert "No album" in result


def test_summarize_plan_single_dir(tmp_path):
    dirs = [tmp_path / "Album"]
    result = rg.summarize_plan(dirs, 12)
    assert "1 album" in result.lower() or "1 album director" in result.lower()
    assert "12" in result


def test_summarize_plan_multiple_dirs(tmp_path):
    dirs = [tmp_path / f"Album{i}" for i in range(3)]
    result = rg.summarize_plan(dirs, 30)
    assert "3" in result
    assert "30" in result


def test_summarize_plan_shows_sample_paths(tmp_path):
    dirs = [tmp_path / f"Album{i}" for i in range(3)]
    result = rg.summarize_plan(dirs, 9)
    for d in dirs:
        assert str(d) in result


def test_summarize_plan_truncates_long_list(tmp_path):
    dirs = [tmp_path / f"Album{i:02d}" for i in range(20)]
    result = rg.summarize_plan(dirs, 200, sample_n=5)
    assert "more" in result
    assert str(dirs[0]) in result
    assert str(dirs[10]) not in result


def test_summarize_plan_no_truncation_when_few(tmp_path):
    dirs = [tmp_path / f"Album{i}" for i in range(3)]
    result = rg.summarize_plan(dirs, 9, sample_n=5)
    assert "more" not in result


# ---------------------------------------------------------------------------
# parse_args / _resolve_config
# ---------------------------------------------------------------------------


def test_parse_args_dry_run_is_default(tmp_path):
    args = rg.parse_args(["--music-dir", str(tmp_path)])
    config = rg._resolve_config(args)
    assert config.dry_run is True
    assert config.apply is False


def test_parse_args_apply_disables_dry_run(tmp_path):
    args = rg.parse_args(["--music-dir", str(tmp_path), "--apply"])
    config = rg._resolve_config(args)
    assert config.apply is True
    assert config.dry_run is False


def test_parse_args_jobs(tmp_path):
    args = rg.parse_args(["--music-dir", str(tmp_path), "--jobs", "4"])
    config = rg._resolve_config(args)
    assert config.jobs == 4


def test_parse_args_skip_existing(tmp_path):
    args = rg.parse_args(["--music-dir", str(tmp_path), "--skip-existing"])
    config = rg._resolve_config(args)
    assert config.skip_existing is True


def test_resolve_config_min_jobs(tmp_path):
    args = rg.parse_args(["--music-dir", str(tmp_path), "--jobs", "0"])
    config = rg._resolve_config(args)
    assert config.jobs >= 1
