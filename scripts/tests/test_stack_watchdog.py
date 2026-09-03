"""Tests for scripts/stack_watchdog.py — pure-logic unit tests, no docker needed."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "stack_watchdog.py"
    spec = importlib.util.spec_from_file_location("stack_watchdog", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


wd = _load_module()


def _container(service, status="running", health=None, restarts=0, exit_code=0, oom=False):
    state = {"Status": status, "RestartCount": restarts, "ExitCode": exit_code, "OOMKilled": oom}
    if health is not None:
        state["Health"] = {"Status": health, "FailingStreak": 3}
    return {
        "Name": f"/{service}",
        "State": state,
        "Config": {"Labels": {"com.docker.compose.service": service}},
    }


# --- check_containers ---


def test_healthy_stack_raises_nothing():
    containers = {"jellyfin": _container("jellyfin", health="healthy")}
    alerts, restarts = wd.check_containers(["jellyfin"], containers, {}, set())
    assert alerts == []
    assert restarts == {"jellyfin": 0}


def test_service_defined_but_no_container_alerts():
    """The autoheal gap: a compose service with no container at all."""
    alerts, _ = wd.check_containers(["jellyfin", "autoheal"], {"jellyfin": _container("jellyfin")}, {}, set())
    keys = [a.key for a in alerts]
    assert "container:autoheal:missing" in keys
    assert all(a.severity == "critical" for a in alerts)


def test_stopped_container_reports_exit_code_and_oom():
    containers = {"qbittorrent": _container("qbittorrent", status="exited", exit_code=137, oom=True)}
    alerts, _ = wd.check_containers(["qbittorrent"], containers, {}, set())
    assert len(alerts) == 1
    assert alerts[0].key == "container:qbittorrent:down"
    assert "137" in alerts[0].message
    assert "OOM-killed" in alerts[0].message


def test_unhealthy_running_container_alerts():
    containers = {"slskd": _container("slskd", health="unhealthy")}
    alerts, _ = wd.check_containers(["slskd"], containers, {}, set())
    assert [a.key for a in alerts] == ["container:slskd:unhealthy"]
    assert alerts[0].severity == "warning"


def test_restart_churn_is_caught_even_while_up():
    containers = {"qbittorrent": _container("qbittorrent", health="healthy", restarts=5)}
    alerts, restarts = wd.check_containers(["qbittorrent"], containers, {"qbittorrent": 2}, set())
    assert [a.key for a in alerts] == ["container:qbittorrent:restarting"]
    assert restarts["qbittorrent"] == 5


def test_first_run_does_not_alert_on_existing_restart_count():
    containers = {"qbittorrent": _container("qbittorrent", health="healthy", restarts=9)}
    alerts, _ = wd.check_containers(["qbittorrent"], containers, {}, set())
    assert alerts == []


def test_ignored_service_is_skipped():
    containers: dict[str, dict] = {}
    alerts, _ = wd.check_containers(["recyclarr"], containers, {}, {"recyclarr"})
    assert alerts == []


# --- jellyfin memory ---


SAMPLE = (
    "2026-09-01T16:37:01+00:00\tanon={anon}MB\tfile=556.8MB\tslab=36.1MB\tkstack=0.5MB"
    "\tmem_current=1058.6MB\tmem_peak=1276.8MB\tvmrss=576.3MB\tarena_regions=1\tdoublemapper=0"
)


def _mem_log(tmp_path, line):
    path = tmp_path / "jellyfin-mem.log"
    path.write_text("# a v1 header comment that must be ignored\n" + line + "\n")
    return path


def test_memory_below_threshold_is_quiet(tmp_path):
    path = _mem_log(tmp_path, SAMPLE.format(anon="455.6"))
    assert wd.check_jellyfin_memory(path, 4096.0, 15.0) == []


def test_memory_above_threshold_alerts(tmp_path):
    path = _mem_log(tmp_path, SAMPLE.format(anon="5120.0"))
    alerts = wd.check_jellyfin_memory(path, 4096.0, 15.0)
    assert [a.key for a in alerts] == ["jellyfin:memory:high"]
    assert alerts[0].severity == "critical"


def test_stale_sampler_alerts(tmp_path):
    path = _mem_log(tmp_path, SAMPLE.format(anon="455.6"))
    old = time.time() - 3600
    os.utime(path, (old, old))
    alerts = wd.check_jellyfin_memory(path, 4096.0, 15.0)
    assert [a.key for a in alerts] == ["jellyfin:sampler:stale"]


def test_failed_sample_line_alerts(tmp_path):
    path = _mem_log(tmp_path, "2026-09-01T16:37:01+00:00\tSAMPLE_FAILED\treason=no_such_container")
    alerts = wd.check_jellyfin_memory(path, 4096.0, 15.0)
    assert [a.key for a in alerts] == ["jellyfin:sampler:failing"]


def test_missing_sampler_log_alerts(tmp_path):
    alerts = wd.check_jellyfin_memory(tmp_path / "nope.log", 4096.0, 15.0)
    assert [a.key for a in alerts] == ["jellyfin:sampler:missing"]


# --- kernel OOM pattern ---


def test_oom_pattern_matches_real_kernel_lines():
    killed = (
        "2026-09-01T05:34:58+02:00 kartoffelschen kernel: Out of memory: Killed process 2552509 "
        "(jellyfin) total-vm:278141732kB, anon-rss:23483348kB, file-rss:8kB, shmem-rss:40088kB"
    )
    constraint = (
        "2026-09-01T05:34:58+02:00 kartoffelschen kernel: oom-kill:constraint=CONSTRAINT_NONE,"
        "nodemask=(null),cpuset=docker-07f7.scope,mems_allowed=0,global_oom"
    )
    benign = "2026-09-01T05:34:58+02:00 kartoffelschen kernel: eth0: renamed from veth123"
    assert wd.OOM_PATTERN.search(killed)
    assert wd.OOM_PATTERN.search(constraint)
    assert not wd.OOM_PATTERN.search(benign)


# --- autoheal (the supervisor itself) ---


def _labeled(service, **labels):
    c = _container(service)
    c["Config"]["Labels"].update(labels)
    return c


def test_autoheal_down_is_critical():
    containers = {"autoheal": _container("autoheal", status="exited")}
    alerts = wd.check_autoheal(containers, "")
    assert [a.key for a in alerts] == ["autoheal:down"]
    assert alerts[0].severity == "critical"


def test_autoheal_absent_entirely_is_critical():
    alerts = wd.check_autoheal({}, "")
    assert [a.key for a in alerts] == ["autoheal:down"]


def test_autoheal_running_with_supervised_containers_is_quiet():
    containers = {
        "autoheal": _container("autoheal"),
        "qbittorrent": _labeled("qbittorrent", autoheal="true"),
    }
    assert wd.check_autoheal(containers, "Monitoring containers for unhealthy status") == []


def test_autoheal_supervising_nothing_alerts():
    """Running, healthy, and completely pointless — no container wears the label."""
    containers = {"autoheal": _container("autoheal"), "qbittorrent": _container("qbittorrent")}
    alerts = wd.check_autoheal(containers, "")
    assert [a.key for a in alerts] == ["autoheal:supervising-nothing"]


def test_autoheal_restart_failures_alert():
    """CURL_TIMEOUT < AUTOHEAL_DEFAULT_STOP_TIMEOUT produces exactly this line."""
    containers = {
        "autoheal": _container("autoheal"),
        "qbittorrent": _labeled("qbittorrent", autoheal="true"),
    }
    logs = (
        "01-09-2026 17:30:17 Container /qbittorrent (fb2a) found to be unhealthy - Restarting\n"
        "01-09-2026 17:30:47 Restarting container fb2a failed\n"
    )
    alerts = wd.check_autoheal(containers, logs)
    assert [a.key for a in alerts] == ["autoheal:restart-failing"]
    assert "failed" in alerts[0].message


# --- wrapped cron job freshness ---


def _cron_state(tmp_path, name, **fields):
    d = tmp_path / "cron-state"
    d.mkdir(exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(fields))
    return d


def test_fresh_cron_job_is_quiet(tmp_path):
    d = _cron_state(tmp_path, "media-ops", max_age_min=30, last_success=time.time() - 60, last_exit=0)
    assert wd.check_cron_jobs(d) == []


def test_cron_job_that_stopped_succeeding_alerts(tmp_path):
    d = _cron_state(tmp_path, "media-ops", max_age_min=30, last_success=time.time() - 3 * 3600, last_exit=0)
    alerts = wd.check_cron_jobs(d)
    assert [a.key for a in alerts] == ["cron:media-ops:stale"]
    assert alerts[0].severity == "critical"
    assert "last succeeded" in alerts[0].message


def test_cron_job_that_never_ran_alerts_from_registration(tmp_path):
    """The media_ops_status case: broken cron line, so no output at all to notice."""
    d = _cron_state(tmp_path, "media-ops", max_age_min=30, registered=time.time() - 9 * 3600)
    alerts = wd.check_cron_jobs(d)
    assert [a.key for a in alerts] == ["cron:media-ops:stale"]
    assert "never succeeded" in alerts[0].message
    assert "has not run at all" in alerts[0].message


def test_a_failing_job_still_shows_its_exit_code(tmp_path):
    d = _cron_state(
        tmp_path, "album-art", max_age_min=30, registered=time.time() - 5 * 3600,
        last_run=time.time() - 60, last_exit=2,
    )
    alerts = wd.check_cron_jobs(d)
    assert "last exit 2" in alerts[0].message


def test_missing_state_dir_is_not_an_error(tmp_path):
    assert wd.check_cron_jobs(tmp_path / "nope") == []


def test_unreadable_state_file_alerts(tmp_path):
    d = tmp_path / "cron-state"
    d.mkdir()
    (d / "broken.json").write_text("{not json")
    assert [a.key for a in wd.check_cron_jobs(d)] == ["cron:broken:unreadable"]


def test_state_without_max_age_is_skipped(tmp_path):
    d = _cron_state(tmp_path, "legacy", last_success=time.time() - 99 * 3600)
    assert wd.check_cron_jobs(d) == []


# --- crontab lint (the media_ops_status class of bug) ---


# Derived, not hardcoded: lint_crontab checks each `scripts/<x>.py` for
# existence under this root, so a literal "/home/tom/nas" makes every script
# look missing anywhere but this one machine. These tests passed locally and
# failed in CI for exactly that reason.
REPO = Path(__file__).resolve().parents[2]


def test_relative_path_without_cd_is_flagged():
    """The exact line that silently did nothing for three months."""
    line = "*/5 * * * * .venv/bin/python scripts/media_ops_status.py --json-out /x >/dev/null 2>&1"
    alerts = wd.lint_crontab(line, REPO)
    assert [a.key.split(":")[1] for a in alerts] == ["no-cd"]
    assert alerts[0].severity == "critical"


def test_line_with_cd_is_accepted():
    line = f"0 1 * * * cd {REPO} && . .venv/bin/activate && python scripts/album_art.py"
    assert wd.lint_crontab(line, REPO) == []


def test_missing_script_is_flagged(tmp_path):
    line = "0 1 * * * cd " + str(tmp_path) + " && python scripts/nope.py"
    alerts = wd.lint_crontab(line, tmp_path)
    assert [a.key for a in alerts] == ["crontab:missing-script:scripts/nope.py"]


def test_comments_and_blank_lines_are_ignored():
    assert wd.lint_crontab("# scripts/whatever.py is mentioned here\n\n   \n", REPO) == []


def test_alert_key_is_stable_across_processes():
    """Keys must be deterministic or dedupe fails and the same alert re-notifies forever."""
    line = "*/5 * * * * .venv/bin/python scripts/album_art.py"
    first = [a.key for a in wd.lint_crontab(line, REPO)]
    second = [a.key for a in wd.lint_crontab(line, REPO)]
    assert first == second
    assert first[0] == "crontab:no-cd:c16ebc0d"


def test_absolute_paths_need_no_cd():
    line = "0 3 * * 0 /usr/bin/docker image prune -f"
    assert wd.lint_crontab(line, REPO) == []


def test_at_shorthand_lines_are_linted_too():
    """@daily/@reboot are valid cron and must not be a blind spot in the lint."""
    assert wd.lint_crontab("@daily .venv/bin/python scripts/album_art.py", REPO)
    assert wd.lint_crontab(f"@daily cd {REPO} && python scripts/album_art.py", REPO) == []


# --- WAN shaper ---


def test_shaper_check_failure_is_critical(monkeypatch):
    """Any of qdisc / rate / marks missing must degrade, not just a missing qdisc."""
    for detail in ("wan_shaper: FAIL no CAKE qdisc on enp88s0",
                   "wan_shaper: FAIL CAKE bandwidth is not 28Mbit (line re-provisioned? re-measure)",
                   "wan_shaper: FAIL 0 of 2 DSCP bulk marks present — torrents are not yielding"):
        monkeypatch.setattr(wd, "_run", lambda *a, d=detail, **k: (1, d))
        alerts = wd.check_wan_shaper()
        assert [x.key for x in alerts] == ["wan:shaper:degraded"], detail
        assert alerts[0].severity == "critical"
        assert "FAIL" in alerts[0].message


def test_healthy_shaper_is_quiet(monkeypatch):
    monkeypatch.setattr(wd, "_run", lambda *a, **k: (0, "wan_shaper: OK shaping 28Mbit, 2 bulk marks"))
    assert wd.check_wan_shaper() == []


# --- media drive (no SMART available through the USB bridge) ---


def test_unmounted_media_drive_is_critical(monkeypatch):
    monkeypatch.setattr(wd, "_run", lambda *a, **k: (1, ""))
    assert [a.key for a in wd.check_media_storage()] == ["media:unmounted"]


def test_readonly_remount_is_critical(monkeypatch):
    """ext4's default on error is remount-ro; every *arr import then fails silently."""
    def fake_run(cmd, *a, **k):
        if cmd[0] == "findmnt":
            return 0, "ro,relatime"
        if cmd[0] == "df":
            return 0, "Avail\n5000000000000"
        return 0, ""

    monkeypatch.setattr(wd, "_run", fake_run)
    assert "media:readonly" in [a.key for a in wd.check_media_storage()]


def test_kernel_io_errors_are_critical(monkeypatch):
    """The earliest warning available on a disk with no SMART."""
    def fake_run(cmd, *a, **k):
        if cmd[0] == "findmnt":
            return 0, "rw,relatime"
        if cmd[0] == "df":
            return 0, "Avail\n5000000000000"
        return 0, "blk_update_request: I/O error, dev sda, sector 12345"

    monkeypatch.setattr(wd, "_run", fake_run)
    alerts = wd.check_media_storage()
    assert "media:kernel-errors" in [a.key for a in alerts]


def test_healthy_media_drive_is_quiet(monkeypatch):
    def fake_run(cmd, *a, **k):
        if cmd[0] == "findmnt":
            return 0, "rw,relatime"
        if cmd[0] == "df":
            return 0, "Avail\n5000000000000"
        return 0, "eth0: renamed from veth123"

    monkeypatch.setattr(wd, "_run", fake_run)
    assert wd.check_media_storage() == []


def test_low_free_space_warns(monkeypatch):
    def fake_run(cmd, *a, **k):
        if cmd[0] == "findmnt":
            return 0, "rw,relatime"
        if cmd[0] == "df":
            return 0, "Avail\n50000000000"
        return 0, ""

    monkeypatch.setattr(wd, "_run", fake_run)
    assert [a.key for a in wd.check_media_storage()] == ["media:low-space"]


# --- main(): --dry-run must not mutate the state file ---


def _quiet_main(monkeypatch, wd_mod):
    """Neutralise every collector so main() computes an empty alert list."""
    monkeypatch.setattr(wd_mod, "inspect_containers", lambda *a, **k: {})
    monkeypatch.setattr(wd_mod, "defined_services", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_containers", lambda *a, **k: ([], {}))
    monkeypatch.setattr(wd_mod, "autoheal_logs", lambda *a, **k: "")
    monkeypatch.setattr(wd_mod, "check_autoheal", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_cron_jobs", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_heartbeat_configured", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_wan_shaper", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_media_storage", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_stuck_starting", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "read_crontab", lambda *a, **k: "")
    monkeypatch.setattr(wd_mod, "lint_crontab", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_jellyfin_memory", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "check_kernel_oom", lambda *a, **k: ([], 123.0))


def test_dry_run_does_not_consume_a_pending_resolve(tmp_path, monkeypatch):
    """The 2026-09-02 20:15->20:20 defect: --dry-run saved the pruned state.

    An ad-hoc --dry-run loaded `active`, saw the problem was over, printed
    [RESOLVED], skipped the push, and then persisted the pruned state anyway.
    The next cron run had nothing left to announce, so autoheal:down and
    container:slskd:unhealthy never sent a recovery and stayed open on the
    phone for ~19h after they had actually cleared.
    """
    state = tmp_path / "watchdog.json"
    before = json.dumps(
        {"active": {"autoheal:down": {"first_seen": 1.0, "last_notified": 2.0}},
         "restart_counts": {}, "oom_cursor": 0},
        indent=1, sort_keys=True,
    )
    state.write_text(before)

    _quiet_main(monkeypatch, wd)
    monkeypatch.setenv("NAS_ALERT_WEBHOOK", "http://ntfy.invalid/nas-alerts")
    monkeypatch.setattr(
        wd, "notify",
        lambda *a, **k: pytest.fail("--dry-run must not notify"),  # noqa: ARG005
    )

    assert wd.main(["--dry-run", "--state", str(state)]) == 0
    assert state.read_text() == before, "--dry-run rewrote the state file"


def test_a_real_run_does_persist_and_resolve(tmp_path, monkeypatch):
    """The guard must not break the normal path: a real run still clears it."""
    state = tmp_path / "watchdog.json"
    state.write_text(json.dumps(
        {"active": {"autoheal:down": {"first_seen": 1.0, "last_notified": 2.0}},
         "restart_counts": {}, "oom_cursor": 0}))

    _quiet_main(monkeypatch, wd)
    monkeypatch.setenv("NAS_ALERT_WEBHOOK", "http://ntfy.invalid/nas-alerts")
    sent: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        wd, "notify",
        lambda _hook, alert, resolved=False: (sent.append((alert.key, resolved)), True)[1],
    )

    assert wd.main(["--state", str(state)]) == 0
    assert sent == [("autoheal:down", True)], "the recovery must be pushed"
    assert json.loads(state.read_text())["active"] == {}


# --- alert cadence: backoff, and never-succeeded vs went-stale ---


def test_backoff_doubles_then_holds_at_the_cap():
    """20 identical hourly pages is the bug; this is the cadence that replaces it."""
    assert wd.backoff_repeat_min(0) == 60.0
    assert wd.backoff_repeat_min(59) == 60.0
    assert wd.backoff_repeat_min(60) == 120.0
    assert wd.backoff_repeat_min(180) == 240.0
    # and it never grows past the cap, however long the outage runs
    assert wd.backoff_repeat_min(60 * 24 * 30) == wd.BACKOFF_CAP_MIN


def test_backoff_never_returns_zero_or_negative():
    """A zero interval would push on every */5 tick — worse than no backoff."""
    for age in (-10, 0, 1, 7, 12345):
        assert wd.backoff_repeat_min(age) >= 60.0


def test_never_succeeded_job_pages_once_then_daily(tmp_path):
    """A config bug will not fix itself; hourly repetition adds nothing."""
    d = _cron_state(tmp_path, "playlist-sync", max_age_min=1440,
                    registered=time.time() - 43 * 3600, last_exit=1)
    (alert,) = wd.check_cron_jobs(d)
    assert alert.key == "cron:playlist-sync:stale"
    assert "has never succeeded" in alert.message
    assert alert.repeat_min == wd.NEVER_SUCCEEDED_REPEAT_MIN


def test_job_that_went_stale_escalates_instead(tmp_path):
    """Something changed and it may come back, so the age is the news."""
    d = _cron_state(tmp_path, "media-ops", max_age_min=30,
                    last_success=time.time() - 10 * 3600, last_exit=0)
    (alert,) = wd.check_cron_jobs(d)
    assert "last succeeded" in alert.message
    assert alert.repeat_min == wd.BACKOFF_CAP_MIN


def test_the_two_cases_do_not_share_a_cadence(tmp_path):
    """The whole point of the split: one loud-then-quiet, one escalating."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    never = _cron_state(tmp_path / "a", "job", max_age_min=60,
                        registered=time.time() - 30 * 3600)
    went = _cron_state(tmp_path / "b", "job", max_age_min=60,
                       last_success=time.time() - 30 * 3600)
    assert wd.check_cron_jobs(never)[0].repeat_min != wd.check_cron_jobs(went)[0].repeat_min


# --- in-flight jobs are slow, not silent ---


def test_a_job_still_running_is_not_stale(tmp_path):
    """playlist-sync: 21h into a progressing run, paged as stale for all of them."""
    now = time.time()
    d = _cron_state(tmp_path, "playlist-sync", max_age_min=1440,
                    registered=now - 43 * 3600, last_exit=1,
                    in_flight_since=now - 21 * 3600, in_flight_heartbeat=now - 30)
    assert wd.check_cron_jobs(d) == []


def test_an_in_flight_marker_with_a_dead_heartbeat_still_alerts(tmp_path):
    """A -9'd job cannot clear its own marker, so the stamp must expire."""
    now = time.time()
    d = _cron_state(tmp_path, "playlist-sync", max_age_min=1440,
                    registered=now - 43 * 3600, last_exit=1,
                    in_flight_since=now - 21 * 3600,
                    in_flight_heartbeat=now - (wd.IN_FLIGHT_STALE_MIN + 5) * 60)
    (alert,) = wd.check_cron_jobs(d)
    assert "stopped reporting" in alert.message


def test_in_flight_since_alone_cannot_silence_the_check(tmp_path):
    """Without a heartbeat the marker is unfalsifiable; it must be ignored."""
    now = time.time()
    d = _cron_state(tmp_path, "playlist-sync", max_age_min=1440,
                    registered=now - 43 * 3600, in_flight_since=now - 21 * 3600)
    assert [a.key for a in wd.check_cron_jobs(d)] == ["cron:playlist-sync:stale"]
