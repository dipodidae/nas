import importlib.util
import sqlite3
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "check-cleanuparr-excludes-lidarr.py"
  spec = importlib.util.spec_from_file_location("check_cleanuparr", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module  # type: ignore[attr-defined]
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


check = _load_module()

HEALTHY = [("Sonarr", 1), ("Radarr", 1), ("Lidarr", 0)]


def test_healthy_state_passes():
  ok, problems = check.evaluate(HEALTHY, [0])
  assert ok
  assert problems == []


def test_lidarr_absent_entirely_passes():
  ok, _ = check.evaluate([("Sonarr", 1), ("Radarr", 1)], [0])
  assert ok


def test_lidarr_enabled_is_caught():
  ok, problems = check.evaluate([("Lidarr", 1)], [0])
  assert not ok
  assert any("ENABLED" in p for p in problems)


def test_lidarr_name_match_ignores_case_and_padding():
  ok, _ = check.evaluate([("  lidarr ", 1)], [0])
  assert not ok


def test_seeker_on_is_caught_even_with_lidarr_off():
  ok, problems = check.evaluate(HEALTHY, [1])
  assert not ok
  assert any("Seeker" in p for p in problems)


def test_both_drifted_reports_both():
  ok, problems = check.evaluate([("Lidarr", 1)], [1])
  assert not ok
  assert len(problems) == 2


def test_the_exact_2026_09_17_drift_state_fails():
  # What was actually live when the re-grab loop was found.
  ok, problems = check.evaluate([("Sonarr", 1), ("Radarr", 1), ("Lidarr", 1)], [1])
  assert not ok
  assert len(problems) == 2


def test_read_state_reads_a_real_sqlite_db(tmp_path):
  db = tmp_path / "cleanuparr.db"
  conn = sqlite3.connect(db)
  conn.execute("create table arr_instances (name text, enabled int)")
  conn.execute("create table seeker_configs (search_enabled int)")
  conn.execute("insert into arr_instances values ('Lidarr', 1)")
  conn.execute("insert into seeker_configs values (1)")
  conn.commit()
  conn.close()
  instances, seeker = check.read_state(db)
  assert instances == [("Lidarr", 1)]
  assert seeker == [1]
  assert not check.evaluate(instances, seeker)[0]


def test_main_returns_1_on_drift(tmp_path, monkeypatch, capsys):
  cfg = tmp_path / "cleanuparr"
  cfg.mkdir()
  db = cfg / "cleanuparr.db"
  conn = sqlite3.connect(db)
  conn.execute("create table arr_instances (name text, enabled int)")
  conn.execute("create table seeker_configs (search_enabled int)")
  conn.execute("insert into arr_instances values ('Lidarr', 1)")
  conn.execute("insert into seeker_configs values (0)")
  conn.commit()
  conn.close()
  monkeypatch.setenv("CONFIG_DIRECTORY", str(tmp_path))
  assert check.main([]) == 1
  assert "ENABLED" in capsys.readouterr().err


def test_main_returns_0_when_clean(tmp_path, monkeypatch, capsys):
  cfg = tmp_path / "cleanuparr"
  cfg.mkdir()
  db = cfg / "cleanuparr.db"
  conn = sqlite3.connect(db)
  conn.execute("create table arr_instances (name text, enabled int)")
  conn.execute("create table seeker_configs (search_enabled int)")
  conn.execute("insert into arr_instances values ('Lidarr', 0)")
  conn.execute("insert into seeker_configs values (0)")
  conn.commit()
  conn.close()
  monkeypatch.setenv("CONFIG_DIRECTORY", str(tmp_path))
  assert check.main([]) == 0
  assert "ok:" in capsys.readouterr().out


def test_main_returns_2_when_db_missing(tmp_path, monkeypatch):
  monkeypatch.setenv("CONFIG_DIRECTORY", str(tmp_path))
  assert check.main([]) == 2
