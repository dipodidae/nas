"""Tests for scripts/log_pruner.py.

The one that matters: until 2026-09-13 this globbed ``*.log`` only, and every
*arr writes ``sonarr.debug.21.txt``. The weekly job therefore reported
"processed 0 file(s)" for its entire life while Prowlarr held 99 MB, Lidarr
105 MB and Sonarr 80 MB of logs.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  spec = importlib.util.spec_from_file_location("log_pruner", scripts_dir / "log_pruner.py")
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


lp = _load_module()


def test_a_dot_log_file_is_a_log_anywhere():
  assert lp.is_log_file(Path("/config/swag/log/nginx/access.log"))
  assert lp.is_log_file(Path("/home/tom/nas/logs/process_soulseek_imports.log"))
  assert lp.is_log_file(Path("/anywhere/at/all/thing.log"))


def test_arr_txt_logs_are_caught_inside_a_logs_directory():
  assert lp.is_log_file(Path("/config/sonarr/logs/sonarr.debug.21.txt"))
  assert lp.is_log_file(Path("/config/prowlarr/logs/prowlarr.2.txt"))
  assert lp.is_log_file(Path("/config/lidarr/log/lidarr.15.txt"))


def test_a_txt_outside_a_logs_directory_is_left_alone():
  """Otherwise this would eat subtitles, NFO dumps and config exports."""
  assert not lp.is_log_file(Path("/config/bazarr/subtitles/movie.en.txt"))
  assert not lp.is_log_file(Path("/config/notes.txt"))


def test_other_extensions_are_ignored():
  assert not lp.is_log_file(Path("/config/sonarr/logs/sonarr.db"))
  assert not lp.is_log_file(Path("/config/sonarr/logs/archive.zip"))


def test_jsonl_counts_only_inside_a_logs_directory():
  assert lp.is_log_file(Path("/config/lidarr-bulk/logs/jobs.jsonl"))
  assert not lp.is_log_file(Path("/config/lidarr-bulk/jobs.jsonl"))


def _args(roots: list[str], extra: list[str]) -> argparse.Namespace:
  return argparse.Namespace(roots=roots, extra_roots=extra)


def test_extra_roots_add_to_the_default(tmp_path: Path, monkeypatch):
  cfg = tmp_path / "config"
  logs = tmp_path / "logs"
  cfg.mkdir()
  logs.mkdir()
  monkeypatch.setenv("CONFIG_DIRECTORY", str(cfg))
  roots = lp.gather_roots(_args([], [str(logs)]))
  assert set(roots) == {cfg, logs}


def test_a_root_named_twice_is_scanned_once(tmp_path: Path, monkeypatch):
  cfg = tmp_path / "config"
  cfg.mkdir()
  monkeypatch.setenv("CONFIG_DIRECTORY", str(cfg))
  assert lp.gather_roots(_args([], [str(cfg)])) == [cfg]


def test_a_nonexistent_root_is_dropped(tmp_path: Path, monkeypatch):
  cfg = tmp_path / "config"
  cfg.mkdir()
  monkeypatch.setenv("CONFIG_DIRECTORY", str(cfg))
  assert lp.gather_roots(_args([], [str(tmp_path / "nope")])) == [cfg]
