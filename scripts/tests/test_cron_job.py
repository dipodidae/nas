"""Tests for scripts/cj.py — the in-flight heartbeat half of the contract.

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


# --- which lane a failure goes to (ADR-0033) ---


def test_a_single_failure_is_quiet_and_a_repeat_is_not():
    """Most single failures self-heal on the next tick; two in a row do not."""
    assert cj.failure_lane(None, 1) == "infra"
    assert cj.failure_lane(None, 2) == "attention"
    assert cj.failure_lane(None, 17) == "attention"


def test_fail_lane_pins_the_lane_and_skips_the_ladder():
    """config-backup's FIRST failure is the incident: a backup failure is only
    ever discovered when you need the backup."""
    for n in (1, 2, 99):
        assert cj.failure_lane("critical", n) == "critical"


def test_the_ladder_never_produces_a_lane_the_router_does_not_have():
    lanes = {lane.value for lane in cj.notifier.Lane}
    assert {cj.FIRST_FAILURE_LANE, cj.REPEAT_FAILURE_LANE} <= lanes


def _run_failing(monkeypatch, name, extra=()):
    """Run the wrapper over a command that exits 2, capturing the publishes.

    The caller redirects STATE_DIR: two runs of the same job must share one
    state file, which is the whole point of the ladder.
    """
    sent: list[tuple[str, str]] = []

    class _R:
        sent = True

    monkeypatch.setattr(
        cj.notifier, "notify",
        lambda lane, title, message, **kw: (  # noqa: ARG005
            sent.append((lane, title)), _R())[1],
    )
    monkeypatch.setattr(cj.notifier, "resolved", lambda *a, **k: _R())  # noqa: ARG005
    argv = ["--name", name, "--max-age-min", "30", "--alert-repeat-min", "0",
            *extra, "--", "python3", "-c", "import sys; sys.exit(2)"]
    code = cj.main(argv)
    return code, sent


def test_the_first_failure_lands_in_infra_and_the_second_in_attention(monkeypatch, tmp_path):
    _redirect_state(monkeypatch, tmp_path)
    code, sent = _run_failing(monkeypatch, "laddertest")
    assert code == 2
    assert [lane for lane, _t in sent] == ["infra"]
    _code2, sent2 = _run_failing(monkeypatch, "laddertest")
    assert [lane for lane, _t in sent2] == ["attention"]


def test_fail_lane_critical_reaches_critical_on_the_very_first_run(monkeypatch, tmp_path):
    _redirect_state(monkeypatch, tmp_path)
    _code, sent = _run_failing(monkeypatch, "backuptest", extra=("--fail-lane", "critical"))
    assert [lane for lane, _t in sent] == ["critical"]


def test_an_escalation_is_not_held_back_by_the_repeat_window(monkeypatch, tmp_path):
    """A first failure in nas-infra must be able to become nas-attention even
    inside --alert-repeat-min, or the escalation is recorded and never sent."""
    _redirect_state(monkeypatch, tmp_path)
    sent: list[str] = []

    class _R:
        sent = True

    monkeypatch.setattr(
        cj.notifier, "notify",
        lambda lane, title, message, **kw: (sent.append(lane), _R())[1],  # noqa: ARG005
    )
    argv = ["--name", "esc", "--max-age-min", "30", "--alert-repeat-min", "600",
            "--", "python3", "-c", "import sys; sys.exit(2)"]
    cj.main(argv)
    cj.main(argv)
    assert sent == ["infra", "attention"], (
        "the second failure must escalate now, not in 10 hours"
    )
    cj.main(argv)
    assert sent == ["infra", "attention"], "and then it must go quiet again"


def test_a_success_clears_the_consecutive_failure_count(monkeypatch, tmp_path):
    _redirect_state(monkeypatch, tmp_path)

    class _R:
        sent = True

    monkeypatch.setattr(cj.notifier, "notify", lambda *a, **k: _R())  # noqa: ARG005
    monkeypatch.setattr(cj.notifier, "resolved", lambda *a, **k: _R())  # noqa: ARG005
    fail = ["--name", "clr", "--max-age-min", "30", "--alert-repeat-min", "0",
            "--", "python3", "-c", "import sys; sys.exit(2)"]
    cj.main(fail)
    cj.main(fail)
    assert cj.load_state("clr")["consecutive_failures"] == 2
    cj.main(["--name", "clr", "--max-age-min", "30", "--", "python3", "-c", ""])
    state = cj.load_state("clr")
    assert "consecutive_failures" not in state
    assert "last_failure_lane" not in state


# --- lock skips must leave a trace (an external `flock -n` leaves none) ---


def test_lock_is_acquired_when_free(tmp_path):
    lock = tmp_path / "l.lock"
    handle = cj.acquire_lock(str(lock), 0.0)
    assert handle is not None
    handle.close()


def test_lock_is_refused_when_held(tmp_path):
    lock = tmp_path / "l.lock"
    held = cj.acquire_lock(str(lock), 0.0)
    assert held is not None
    assert cj.acquire_lock(str(lock), 0.0) is None
    held.close()


def test_lock_wait_gives_up_after_the_deadline(tmp_path):
    import time as _t

    lock = tmp_path / "l.lock"
    held = cj.acquire_lock(str(lock), 0.0)
    start = _t.monotonic()
    assert cj.acquire_lock(str(lock), 0.5) is None
    assert _t.monotonic() - start >= 0.4
    held.close()


def test_a_skipped_run_exits_0_and_records_the_streak(tmp_path, monkeypatch):
    """A skip is not this job's failure -- but it must not look like a clean run."""
    monkeypatch.setattr(cj, "save_state", lambda name, st: None)
    sent = []
    monkeypatch.setattr(cj.notifier, "notify", lambda *a, **k: sent.append(a))
    state = {}
    assert cj.record_skip("j", state, "/tmp/x.lock", max_skips=3) == 0
    assert state["consecutive_lock_skips"] == 1
    assert sent == []


def test_consecutive_skips_alert_once_past_the_threshold(tmp_path, monkeypatch):
    monkeypatch.setattr(cj, "save_state", lambda name, st: None)
    sent = []
    monkeypatch.setattr(cj.notifier, "notify", lambda *a, **k: sent.append(a))
    state = {"consecutive_lock_skips": 2}
    cj.record_skip("j", state, "/tmp/x.lock", max_skips=3)
    assert state["consecutive_lock_skips"] == 3
    assert len(sent) == 1
    assert "lock-starved" in sent[0][1]


def test_running_the_job_clears_the_skip_streak(tmp_path, monkeypatch):
    monkeypatch.setattr(cj, "save_state", lambda name, st: None)
    monkeypatch.setattr(cj, "load_state", lambda name: {"consecutive_lock_skips": 4})
    monkeypatch.setattr(cj, "run_job", lambda *a, **k: (0, ""))
    monkeypatch.setattr(cj.notifier, "notify", lambda *a, **k: None)
    lock = tmp_path / "l.lock"
    rc = cj.main(
        ["--name", "j", "--max-age-min", "60", "--lock", str(lock), "--", "true"]
    )
    assert rc == 0
