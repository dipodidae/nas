import importlib.util
import sys
from pathlib import Path


def _load_qb_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "qbittorrent_stalled_kickstart.py"
  spec = importlib.util.spec_from_file_location("qbittorrent_stalled_kickstart", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  # Ensure the module name is registered before exec for dataclass decorator introspection
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


qbk = _load_qb_module()


def test_classify_state():
  assert qbk.classify_state({"state": "stalledDL"}) == "stalled"
  assert qbk.classify_state({"state": "pausedUP"}) == "paused"
  assert qbk.classify_state({"state": "downloading"}) == "downloading"
  assert qbk.classify_state({"state": "uploading"}) == "uploading"
  assert qbk.classify_state({"state": "foo"}) == "foo"


def test_plan_actions_basic():
  torrents = [
    {"hash": "a1", "state": "stalledDL"},
    {"hash": "b2", "state": "pausedDL"},
    {"hash": "c3", "state": "downloading"},
  ]
  plan = qbk.plan_actions(torrents, do_recheck=True)
  # All hashes subject to reannounce
  assert set(plan["reannounce"]) == {"a1", "b2", "c3"}
  # Resume only paused hash
  assert plan["resume"] == ["b2"]
  # Recheck only stalled
  assert plan["recheck"] == ["a1"]
