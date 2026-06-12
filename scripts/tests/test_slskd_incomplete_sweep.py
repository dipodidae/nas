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


# ---- plan_incomplete_sweep -----------------------------------------------


NOW = _dt.datetime(2026, 6, 12, 12, 0, 0)


def _cand(name, hours_old):
  mtime = (NOW - _dt.timedelta(hours=hours_old)).timestamp()
  return (Path(f"/mnt/drive/downloads/incomplete/{name}"), mtime)


def test_plan_skips_protected_and_recent_selects_orphans():
  candidates = [
    _cand("Old Orphan Album", 100),       # eligible
    _cand("Active Slskd Album", 100),      # protected by slskd ref
    _cand("Seeding Torrent Dir", 100),     # protected by qbt ref
    _cand("Fresh Download", 2),            # too recent (age gate)
  ]
  slskd_refs = {"Active Slskd Album"}
  qbt_refs = {"Seeding Torrent Dir"}
  out = sweep.plan_incomplete_sweep(
    candidates, slskd_refs, qbt_refs, now=NOW, min_age_hours=24
  )
  assert [p.name for p in out] == ["Old Orphan Album"]


def test_plan_empty_candidates():
  assert sweep.plan_incomplete_sweep([], set(), set(), now=NOW, min_age_hours=24) == []
