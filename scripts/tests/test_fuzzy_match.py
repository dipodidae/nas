import importlib.util
import sys
from pathlib import Path


def _load_checker_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "prowlarr_priority_checker.py"
  spec = importlib.util.spec_from_file_location("prowlarr_priority_checker", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


checker = _load_checker_module()


def test_find_best_match_exact():
  match, ratio = checker.find_best_match("Solid Torrents", ["Solid Torrents", "Other"], 0.8)
  assert match == "Solid Torrents"
  assert ratio == 1


def test_find_best_match_fuzzy():
  match, ratio = checker.find_best_match("solid torrents", ["Solid Torrents"], 0.8)
  assert match == "Solid Torrents"
  assert ratio > 0.9


def test_find_best_match_no_match():
  match, ratio = checker.find_best_match("Unknown", ["Solid Torrents"], 0.95)
  assert match is None
  assert ratio == 0
