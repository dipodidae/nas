import datetime as _dt
import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "slskd_incomplete_sweep.py"
  spec = importlib.util.spec_from_file_location("slskd_incomplete_sweep", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


sweep = _load_module()


# ---- _trailing_segment ---------------------------------------------------


def test_trailing_segment_handles_separators():
  assert sweep._trailing_segment("music\\Artist\\Album") == "Album"
  assert sweep._trailing_segment("music/Artist/Album/") == "Album"
  assert sweep._trailing_segment("BareName") == "BareName"
  assert sweep._trailing_segment("") == ""
