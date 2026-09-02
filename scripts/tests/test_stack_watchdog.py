"""Tests for scripts/stack_watchdog.py — pure-logic unit tests, no docker needed."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path


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


REPO = Path("/home/tom/nas")


def test_relative_path_without_cd_is_flagged():
    """The exact line that silently did nothing for three months."""
    line = "*/5 * * * * .venv/bin/python scripts/media_ops_status.py --json-out /x >/dev/null 2>&1"
    alerts = wd.lint_crontab(line, REPO)
    assert [a.key.split(":")[1] for a in alerts] == ["no-cd"]
    assert alerts[0].severity == "critical"


def test_line_with_cd_is_accepted():
    line = "0 1 * * * cd /home/tom/nas && . .venv/bin/activate && python scripts/album_art.py"
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
    assert wd.lint_crontab("@daily cd /home/tom/nas && python scripts/album_art.py", REPO) == []


# --- WAN shaper ---


def test_missing_cake_shaper_is_critical(monkeypatch):
    """tc state is lost on link-down, so its absence must be noisy."""
    def fake_run(cmd, *a, **k):
        if cmd[0] == "tc":
            return 0, "qdisc mq 0: root\nqdisc pfifo_fast 0: parent :1"
        return 0, "wan_shaper-bulk"

    monkeypatch.setattr(wd, "_run", fake_run)
    alerts = wd.check_wan_shaper()
    assert [a.key for a in alerts] == ["wan:shaper:missing"]
    assert alerts[0].severity == "critical"


def test_present_cake_shaper_is_quiet(monkeypatch):
    def fake_run(cmd, *a, **k):
        if cmd[0] == "tc":
            return 0, "qdisc cake 20: parent 1:20 bandwidth 28Mbit"
        return 0, "-A POSTROUTING -s 172.30.0.4/32 --comment wan_shaper-bulk -j DSCP"

    monkeypatch.setattr(wd, "_run", fake_run)
    assert wd.check_wan_shaper() == []


def test_shaper_present_but_bulk_marks_gone_alerts(monkeypatch):
    """CAKE without the CS1 marks bounds latency but stops torrents yielding."""
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        if cmd[0] == "tc":
            return 0, "qdisc cake 20: parent 1:20 bandwidth 28Mbit"
        return 0, "=== DSCP bulk marks ===\n  (none)"

    monkeypatch.setattr(wd, "_run", fake_run)
    assert [a.key for a in wd.check_wan_shaper()] == ["wan:bulk-marks:missing"]
