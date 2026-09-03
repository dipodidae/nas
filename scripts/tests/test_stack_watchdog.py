"""Tests for scripts/stack_watchdog.py — pure-logic unit tests, no docker needed."""

from __future__ import annotations

import datetime as _dt
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
    # Must be neutralised or the unit suite reaches the live Prowlarr.
    monkeypatch.setattr(wd_mod, "fetch_indexer_failures", lambda *a, **k: None)
    monkeypatch.setattr(wd_mod, "check_indexer_failures", lambda *a, **k: [])
    monkeypatch.setattr(wd_mod, "fetch_arr_health", lambda *a, **k: None)
    monkeypatch.setattr(wd_mod, "check_arr_health", lambda *a, **k: [])
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
    monkeypatch.setenv("NTFY_TOKEN_SCRIPTS", "tk_test")
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
    monkeypatch.setenv("NTFY_TOKEN_SCRIPTS", "tk_test")
    sent: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        wd, "notify",
        lambda alert, resolved=False, active_min=0.0: (  # noqa: ARG005
            sent.append((alert.key, resolved)), True)[1],
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


# --- Prowlarr indexer flap damping ---


def _idx(name, down_hours=None, till="2026-09-04T07:44:26Z"):
    initial = None
    if down_hours is not None:
        stamp = _dt.datetime.fromtimestamp(time.time() - down_hours * 3600, tz=_dt.UTC)
        initial = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"name": name, "initial_failure": initial, "disabled_till": till}


def test_a_flapping_indexer_is_silent():
    """Knaben/Uindex/TorrentDownload: ~6 fail/restore cycles in a day, all normal."""
    rows = [_idx("Knaben", 0.2), _idx("Uindex", 1.0), _idx("TorrentDownload", 5.9)]
    assert wd.check_indexer_failures(rows) == []


def test_an_indexer_down_past_the_threshold_alerts_once():
    """1337x: failing since 2026-08-27, i.e. genuinely down rather than flapping."""
    alerts = wd.check_indexer_failures([_idx("1337x", 24 * 7)])
    assert [a.key for a in alerts] == ["prowlarr:indexer:1337x:down"]
    assert "168.0h" in alerts[0].message


def test_the_alert_key_is_stable_so_a_flap_cannot_re_page():
    """An unstable key defeats dedupe and every cycle becomes a new alert."""
    first = [a.key for a in wd.check_indexer_failures([_idx("1337x", 10)])]
    second = [a.key for a in wd.check_indexer_failures([_idx("1337x", 11)])]
    assert first == second == ["prowlarr:indexer:1337x:down"]


def test_a_long_outage_backs_off_rather_than_nagging_hourly():
    (alert,) = wd.check_indexer_failures([_idx("Torrent[CORE]", 24 * 38)])
    assert alert.repeat_min == wd.BACKOFF_CAP_MIN


def test_disabled_till_alone_does_not_trigger():
    """Prowlarr sets disabledTill on the FIRST failure, so it is true of a flap."""
    rows = [{"name": "Knaben", "initial_failure": None, "disabled_till": "2026-09-04T07:44:26Z"}]
    assert wd.check_indexer_failures(rows) == []


def test_no_failing_indexers_is_quiet():
    """An empty /indexerstatus genuinely means every indexer is fine."""
    assert wd.check_indexer_failures([]) == []


def test_unreachable_prowlarr_is_not_an_indexer_alert():
    """Prowlarr being down is check_containers' job, not this one's."""
    assert wd.check_indexer_failures(None) == []


def test_threshold_is_configurable_and_respected():
    rows = [_idx("Knaben", 2)]
    assert wd.check_indexer_failures(rows, min_down_min=360) == []
    assert wd.check_indexer_failures(rows, min_down_min=60)


def test_alerts_are_ordered_deterministically():
    """Unstable ordering makes the ntfy feed hard to diff between runs."""
    rows = [_idx("Uindex", 10), _idx("1337x", 20), _idx("Knaben", 30)]
    assert [a.key for a in wd.check_indexer_failures(rows)] == [
        "prowlarr:indexer:1337x:down",
        "prowlarr:indexer:Knaben:down",
        "prowlarr:indexer:Uindex:down",
    ]


def test_malformed_timestamps_do_not_crash_the_run():
    """A watchdog that dies on bad input stops watching everything else."""
    rows = [{"name": "x", "initial_failure": "not-a-date", "disabled_till": None},
            {"name": "y", "initial_failure": "", "disabled_till": None},
            {"name": "z", "initial_failure": 12345, "disabled_till": None}]
    assert wd.check_indexer_failures(rows) == []


# --- *arr health, owned here so onHealthIssue can be switched off ---


def _hrow(app, source, message="something", type_="warning"):
    return {"app": app, "source": source, "type": type_, "message": message}


def test_short_term_indexer_check_is_dropped():
    """IndexerStatusCheck flaps within minutes and is the whole source of churn."""
    rows = [_hrow("prowlarr", "IndexerStatusCheck", "Indexers unavailable: Knaben")]
    assert wd.check_arr_health(rows) == []


def test_long_term_indexer_check_is_dropped_as_redundant():
    """Accurate, but check_indexer_failures says it better and says it once.

    All three apps raise this for the same indexers, so keeping it turned 2 dead
    indexers into 6 alerts. The per-indexer check carries the real duration and
    backs off; this one carries neither.
    """
    rows = [_hrow(app, "IndexerLongTermStatusCheck",
                  "Indexers unavailable due to failures for more than 6 hours: 1337x")
            for app in ("prowlarr", "sonarr", "radarr")]
    assert wd.check_arr_health(rows) == []


def test_non_indexer_warnings_survive_so_nothing_is_lost():
    """Turning off onHealthIssue must not lose root-folder/client warnings."""
    rows = [_hrow("sonarr", "RootFolderCheck", "Missing root folder: /data/series"),
            _hrow("radarr", "DownloadClientCheck", "No download client is available",
                  type_="error")]
    alerts = wd.check_arr_health(rows)
    assert [a.key for a in alerts] == ["arr:radarr:DownloadClientCheck",
                                       "arr:sonarr:RootFolderCheck"]
    # `error` must outrank `warning`, or a dead download client reads as cosmetic
    assert [a.severity for a in alerts] == ["critical", "warning"]


def test_the_same_warning_in_three_apps_is_three_keys_not_one_message():
    """Each app is separately actionable, but each is deduped within itself."""
    rows = [_hrow(app, "RootFolderCheck") for app in ("prowlarr", "sonarr", "radarr")]
    rows += [_hrow("sonarr", "RootFolderCheck")]  # duplicate
    assert len(wd.check_arr_health(rows)) == 3


def test_arr_health_repeats_daily_not_hourly():
    """These are standing conditions; hourly repetition is what killed the topic."""
    (alert,) = wd.check_arr_health([_hrow("sonarr", "RootFolderCheck")])
    assert alert.repeat_min == wd.CONFIG_GAP_REPEAT_MIN


def test_no_reachable_app_is_not_a_health_alert():
    """Containers being down is check_containers' job; do not double-report."""
    assert wd.check_arr_health(None) == []


def test_clean_apps_are_quiet():
    assert wd.check_arr_health([]) == []


def test_malformed_health_payload_does_not_crash():
    assert wd.check_arr_health([{"app": "sonarr"}, "not a dict", {}]) == []


# --- lane routing and escalation (ADR-0033) ---


def _alert(key, severity="warning"):
    return wd.Alert(key, severity, "msg")


def test_no_container_at_all_is_critical_from_the_first_tick():
    """ADR-0006's failure mode. It cost qBittorrent 13h; it does not wait."""
    assert wd.lane_for(_alert("container:qbittorrent:missing"), active_min=0.0) == "critical"
    assert wd.lane_for(_alert("container:recyclarr:missing"), active_min=0.0) == "critical"


def test_an_unhealthy_container_starts_quiet_and_escalates_with_age():
    key = "container:recyclarr:unhealthy"
    assert wd.lane_for(_alert(key), active_min=0.0) == "infra"
    assert wd.lane_for(_alert(key), active_min=14.9) == "infra"
    assert wd.lane_for(_alert(key), active_min=15.0) == "attention"
    # A service nobody is looking at never reaches critical on age alone.
    assert wd.lane_for(_alert(key), active_min=600.0) == "attention"


def test_a_user_visible_service_reaches_critical_in_five_minutes():
    for svc in ("jellyfin", "nextcloud", "swag", "qbittorrent"):
        key = f"container:{svc}:unhealthy"
        assert wd.lane_for(_alert(key), active_min=1.0) == "infra", svc
        assert wd.lane_for(_alert(key), active_min=5.0) == "critical", svc


def test_the_user_visible_set_is_exactly_the_four_documented_services():
    assert set(wd.USER_VISIBLE_SERVICES) == {"jellyfin", "nextcloud", "swag", "qbittorrent"}


def test_the_box_itself_is_always_critical():
    for key in ("kernel:oom:2026-09-03T04:00", "media:unmounted", "media:readonly",
                "media:ext4-errors", "media:ext4-state", "media:kernel-errors"):
        assert wd.lane_for(_alert(key, "critical")) == "critical", key


def test_a_blind_detector_is_attention_not_critical():
    """The OOM check being unreadable is not the box failing — but it is today's job."""
    assert wd.lane_for(_alert("kernel:oom:unreadable")) == "attention"
    assert wd.lane_for(_alert("jellyfin:sampler:stale")) == "attention"


def test_the_routine_and_standing_gaps_land_in_infra():
    for key in ("heartbeat:unconfigured", "autoheal:supervising-nothing",
                "media:ext4-unreadable", "jellyfin:sampler:unparsed",
                "watchdog:self-test", "container:slskd:stuck-starting"):
        assert wd.lane_for(_alert(key)) == "infra", key


def test_longest_prefix_wins_so_a_specific_key_can_differ_from_its_namespace():
    """`media:low-space` must not inherit `media:`'s critical routing."""
    assert wd.lane_for(_alert("media:low-space")) == "attention"
    assert wd.lane_for(_alert("media:unmounted")) == "critical"
    assert wd.lane_for(_alert("autoheal:down")) == "attention"
    assert wd.lane_for(_alert("autoheal:supervising-nothing")) == "infra"


def test_an_unrouted_key_defaults_to_attention_not_silence():
    """A new check with no table entry must be visible, not swallowed."""
    assert wd.lane_for(_alert("something:brand:new")) == "attention"


def test_an_explicit_lane_on_the_alert_wins():
    assert wd.lane_for(wd.Alert("media:low-space", "warning", "m", lane="critical")) == "critical"


def test_every_lane_the_table_names_exists_in_the_router():
    lanes = {lane for _prefix, lane in wd.LANE_BY_KEY_PREFIX}
    assert lanes <= {lane.value for lane in wd.notifier.Lane}


def test_an_escalation_is_pushed_immediately_rather_than_waiting_for_repeat_min(tmp_path, monkeypatch):
    """The bug this guards: the escalation is recorded and never actually sent.

    `due` is False for another 45 minutes after the first push, so without the
    `escalated` clause a jellyfin outage would spend its whole life in nas-infra
    while the state file happily said `lane: critical`.
    """
    state = tmp_path / "watchdog.json"
    now = time.time()
    state.write_text(json.dumps({
        "active": {"container:jellyfin:unhealthy": {
            "first_seen": now - 600, "last_notified": now - 60, "lane": "infra"}},
        "restart_counts": {}, "oom_cursor": 0,
    }))

    _quiet_main(monkeypatch, wd)
    monkeypatch.setenv("NTFY_TOKEN_SCRIPTS", "tk_test")
    monkeypatch.setattr(wd, "check_containers", lambda *a, **k: (  # noqa: ARG005
        [wd.Alert("container:jellyfin:unhealthy", "warning", "healthcheck failing")], {}))
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        wd, "notify",
        lambda alert, resolved=False, active_min=0.0: (  # noqa: ARG005
            sent.append((alert.key, wd.lane_for(alert, active_min))), True)[1],
    )

    wd.main(["--state", str(state), "--repeat-min", "60"])
    assert sent == [("container:jellyfin:unhealthy", "critical")], (
        "a 10-minute jellyfin outage must escalate now, not in 45 minutes"
    )
    assert json.loads(state.read_text())["active"][
        "container:jellyfin:unhealthy"]["lane"] == "critical"


def test_a_stable_lane_still_honours_repeat_min(tmp_path, monkeypatch):
    """The escape hatch must not become "push on every tick"."""
    state = tmp_path / "watchdog.json"
    now = time.time()
    state.write_text(json.dumps({
        "active": {"container:recyclarr:unhealthy": {
            "first_seen": now - 60, "last_notified": now - 60, "lane": "infra"}},
        "restart_counts": {}, "oom_cursor": 0,
    }))
    _quiet_main(monkeypatch, wd)
    monkeypatch.setenv("NTFY_TOKEN_SCRIPTS", "tk_test")
    monkeypatch.setattr(wd, "check_containers", lambda *a, **k: (  # noqa: ARG005
        [wd.Alert("container:recyclarr:unhealthy", "warning", "healthcheck failing")], {}))
    sent: list[str] = []
    monkeypatch.setattr(
        wd, "notify",
        lambda alert, resolved=False, active_min=0.0: (  # noqa: ARG005
            sent.append(alert.key), True)[1],
    )
    wd.main(["--state", str(state), "--repeat-min", "60"])
    assert sent == [], "still infra, still inside repeat-min: nothing to send"


def test_severity_only_raises_a_lanes_priority_never_lowers_it(monkeypatch):
    """A `notice` routed into nas-critical must still arrive at priority 5."""
    seen: list[int | None] = []

    class _R:
        sent = True

    monkeypatch.setattr(
        wd.notifier, "notify",
        lambda _lane, _t, _m, priority=None: (seen.append(priority), _R())[1],
    )
    wd.notify(wd.Alert("media:unmounted", "notice", "m"))
    assert seen == [None], "no override: the lane's own priority 5 stands"
    seen.clear()
    wd.notify(wd.Alert("heartbeat:unconfigured", "critical", "m"))
    assert seen == [5], "a critical severity in nas-infra is worth raising"


def test_a_known_slow_service_never_escalates_out_of_infra():
    """playlist-generator's CPU-bound stages block its event loop for hours.

    Measured CPU 101.63%, unhealthy streak 23. The container is busy, not
    broken, so age must not turn legitimate work into a page — the same
    reasoning as slskd's 4h start_period (ADR-0026).
    """
    key = "container:playlist-generator:unhealthy"
    for age in (0.0, 15.0, 120.0, 60 * 24.0):
        assert wd.lane_for(_alert(key), active_min=age) == "infra", age


def test_the_slow_exemption_does_not_cover_absent_or_exited():
    """Busy is an excuse for unhealthy. It is not an excuse for not existing."""
    assert wd.lane_for(_alert("container:playlist-generator:missing")) == "critical"
    assert wd.lane_for(_alert("container:playlist-generator:down"), active_min=99.0) == "attention"
