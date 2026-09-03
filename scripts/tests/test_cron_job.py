"""Tests for scripts/cron_job.py — the in-flight heartbeat half of the contract.

The read side lives in test_stack_watchdog.py (`check_cron_jobs`); these cover
the writer, plus one end-to-end pass proving the two halves actually agree.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path


def _load(name):
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(name, scripts_dir / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


cj = _load("cron_job")
wd = _load("stack_watchdog")


def _redirect_state(monkeypatch, tmp_path):
    d = tmp_path / "cron-state"
    d.mkdir()
    monkeypatch.setattr(cj, "STATE_DIR", d)
    return d


# --- the marker is written while running and cleared on exit ---


def test_markers_are_cleared_after_a_normal_run(monkeypatch, tmp_path):
    _redirect_state(monkeypatch, tmp_path)
    state: dict = {}
    code, _ = cj.run_job(["true"], "probe", state)
    assert code == 0
    assert "in_flight_since" not in state
    assert "in_flight_heartbeat" not in state


def test_markers_are_cleared_even_when_the_job_fails(monkeypatch, tmp_path):
    """A leftover marker would silence the staleness check for a broken job."""
    _redirect_state(monkeypatch, tmp_path)
    state: dict = {}
    code, _ = cj.run_job(["false"], "probe", state)
    assert code == 1
    assert "in_flight_since" not in state


def test_markers_are_cleared_when_the_command_does_not_exist(monkeypatch, tmp_path):
    """The media_ops_status failure mode: exec fails, so there is no child."""
    _redirect_state(monkeypatch, tmp_path)
    state: dict = {}
    code, err = cj.run_job(["/nonexistent/binary"], "probe", state)
    assert code == 127
    assert "could not execute" in err
    assert "in_flight_since" not in state


def test_heartbeat_is_refreshed_while_the_job_runs(monkeypatch, tmp_path):
    """The state file on disk must show a beat *newer* than the run's start."""
    d = _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(cj, "IN_FLIGHT_HEARTBEAT_SEC", 0.05)
    state: dict = {}
    cj.run_job(["sleep", "0.4"], "probe", state)
    written = json.loads((d / "probe.json").read_text())
    assert written["in_flight_heartbeat"] > written["in_flight_since"]


def test_run_job_without_a_name_writes_no_state(monkeypatch, tmp_path):
    """--register and ad-hoc callers must not gain a state file as a side effect."""
    d = _redirect_state(monkeypatch, tmp_path)
    cj.run_job(["true"])
    assert list(d.glob("*.json")) == []


# --- the two halves agree ---


def test_a_long_running_job_is_not_reported_stale_end_to_end(monkeypatch, tmp_path):
    """playlist-sync's exact shape: overdue clock, live run, must stay quiet.

    Registered 43h ago, never succeeded, a 24h window — every input that made
    the watchdog page hourly for 20 hours. The only difference is that the run
    is in flight and saying so.
    """
    d = _redirect_state(monkeypatch, tmp_path)
    monkeypatch.setattr(cj, "IN_FLIGHT_HEARTBEAT_SEC", 0.05)

    state = {"registered": time.time() - 43 * 3600, "max_age_min": 1440.0}
    cj.save_state("playlist-sync", state)
    assert [a.key for a in wd.check_cron_jobs(d)] == ["cron:playlist-sync:stale"]

    seen: list[list[str]] = []
    original = cj.save_state

    def spy(name, st):
        original(name, st)
        seen.append([a.key for a in wd.check_cron_jobs(d)])

    monkeypatch.setattr(cj, "save_state", spy)
    cj.run_job(["sleep", "0.3"], "playlist-sync", state)

    assert seen, "the heartbeat never wrote"
    assert all(keys == [] for keys in seen), f"paged while running: {seen}"
