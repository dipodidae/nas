"""Tests for scripts/stack_watchdog.py — pure-logic unit tests, no docker needed."""

from __future__ import annotations

import importlib.util
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
    alerts, restarts = check = wd.check_containers(["jellyfin"], containers, {}, set())
    assert alerts == []
    assert restarts == {"jellyfin": 0}
    assert check is not None


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
    import os

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
