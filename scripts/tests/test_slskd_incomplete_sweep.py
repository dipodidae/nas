import datetime as _dt
import importlib.util
import os
import sys
import time
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


# ---- collect_candidates (qBittorrent-zone exclusion) ---------------------


def test_collect_candidates_never_enters_qbittorrent_zone(tmp_path):
  inc = tmp_path / "incomplete"
  (inc / "qbittorrent" / "torrent-temp").mkdir(parents=True)  # qBit-owned: untouchable
  (inc / "slskd" / "Soulseek Album").mkdir(parents=True)       # Zone B orphan
  (inc / "Legacy Flat Orphan").mkdir(parents=True)             # Zone A orphan
  (inc / "loose.txt").write_text("x")                          # non-dir, ignored
  names = {p.name for p, _ in sweep.collect_candidates(inc)}
  # qBittorrent's temp dir and its children are structurally unreachable
  assert "qbittorrent" not in names
  assert "torrent-temp" not in names
  # the managed subdir roots themselves are never candidates
  assert "slskd" not in names
  # the real orphans (flat root + inside incomplete/slskd) ARE candidates
  assert names == {"Legacy Flat Orphan", "Soulseek Album"}


# ---- main() containment guard --------------------------------------------


def test_main_containment_rejects_symlink_escape(tmp_path, monkeypatch):
  inc = tmp_path / "incomplete"
  inc.mkdir()
  outside = tmp_path / "outside"
  outside.mkdir()
  # age the escape target well past the 24h default so the age gate would select it
  old = time.time() - 100 * 3600
  os.utime(outside, (old, old))
  (inc / "escape").symlink_to(outside, target_is_directory=True)
  # empty protection sets so only containment can stop the (aged, unreferenced) target
  monkeypatch.setattr(sweep, "fetch_slskd_refs", lambda *a, **k: set())
  monkeypatch.setattr(sweep, "fetch_qbt_refs", lambda *a, **k: set())
  monkeypatch.setenv("API_KEY_SLSKD", "x")
  monkeypatch.setenv("QBITTORRENT_USER", "x")
  monkeypatch.setenv("QBITTORRENT_PASS", "x")
  monkeypatch.setenv("INCOMPLETE_DIR", str(inc))
  # --dry-run still runs the containment guard (it precedes the dry-run return)
  assert sweep.main(["--dry-run"]) == 2
  assert outside.exists()  # nothing deleted
