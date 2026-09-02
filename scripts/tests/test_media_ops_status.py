"""Unit tests for media_ops_status.py — pure functions only.

No real network, subprocess, or filesystem I/O.  All tests feed canned data
and assert on the pure helper functions.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Module loader (mirrors the pattern used in test_lidarr_stuck_download_reaper)
# ---------------------------------------------------------------------------


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "media_ops_status.py"
    spec = importlib.util.spec_from_file_location("media_ops_status", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


m = _load_module()


# ---------------------------------------------------------------------------
# parse_docker_ps
# ---------------------------------------------------------------------------


def test_parse_docker_ps_healthy_container():
    stdout = "sonarr|running|Up 3 days (healthy)\n"
    result = m.parse_docker_ps(stdout)
    assert len(result) == 1
    c = result[0]
    assert c.name == "sonarr"
    assert c.state == "running"
    assert c.healthy == "healthy"


def test_parse_docker_ps_unhealthy_container():
    stdout = "qbittorrent|running|Up 2 hours (unhealthy)\n"
    result = m.parse_docker_ps(stdout)
    assert result[0].healthy == "unhealthy"


def test_parse_docker_ps_no_health_annotation():
    stdout = "watchtower|running|Up 5 minutes\n"
    result = m.parse_docker_ps(stdout)
    assert result[0].healthy == ""


def test_parse_docker_ps_exited_container():
    stdout = "recyclarr|exited|Exited (0) 1 hour ago\n"
    result = m.parse_docker_ps(stdout)
    c = result[0]
    assert c.state == "exited"
    assert c.healthy == ""


def test_parse_docker_ps_multiple_containers():
    stdout = (
        "sonarr|running|Up 3 days (healthy)\n"
        "radarr|running|Up 3 days (healthy)\n"
        "jellyfin|running|Up 1 day\n"
    )
    result = m.parse_docker_ps(stdout)
    assert len(result) == 3
    names = {c.name for c in result}
    assert names == {"sonarr", "radarr", "jellyfin"}


def test_parse_docker_ps_empty_input():
    assert m.parse_docker_ps("") == []
    assert m.parse_docker_ps("\n\n") == []


def test_parse_docker_ps_health_starting():
    stdout = "sonarr|running|Up 30 seconds (health: starting)\n"
    result = m.parse_docker_ps(stdout)
    assert result[0].healthy == "starting"


def test_parse_docker_ps_pipe_in_status():
    # Status field may contain extra characters; split on first 2 pipes only
    stdout = "swag|running|Up 4 days (healthy)\n"
    result = m.parse_docker_ps(stdout)
    assert result[0].name == "swag"
    assert result[0].healthy == "healthy"


# ---------------------------------------------------------------------------
# log_age_status
# ---------------------------------------------------------------------------


def test_log_age_status_fresh():
    # 30-minute-old log, stale threshold = 4h
    mtime = 1000.0
    now = mtime + 1800  # 30 min later
    result = m.log_age_status(mtime, now, 4 * 3600)
    assert "30m" in result
    assert "stale" not in result


def test_log_age_status_stale():
    mtime = 1000.0
    now = mtime + 5 * 3600  # 5 hours later
    result = m.log_age_status(mtime, now, 4 * 3600)
    assert "stale" in result
    assert "5h" in result


def test_log_age_status_exactly_at_threshold_is_fresh():
    mtime = 1000.0
    stale_after = 3600
    now = mtime + stale_after  # exactly at threshold (not over)
    result = m.log_age_status(mtime, now, stale_after)
    assert "stale" not in result


def test_log_age_status_under_one_hour():
    mtime = 5000.0
    now = mtime + 45 * 60  # 45 min
    result = m.log_age_status(mtime, now, 4 * 3600)
    assert "45m" in result


# ---------------------------------------------------------------------------
# derive_overall_status
# ---------------------------------------------------------------------------


def _container(name: str, state: str = "running", healthy: str = "healthy") -> object:
    return m.ContainerState(name=name, state=state, status="", healthy=healthy)


def _arr(name: str, reachable: bool = True, issues: list | None = None) -> object:
    return m.ArrStatus(
        name=name,
        port=1234,
        reachable=reachable,
        health_issues=issues or [],
        queue_total=None,
    )


def _slskd(reachable: bool = True) -> object:
    return m.SlskdStatus(reachable=reachable)


def _log(stale: bool = False) -> object:
    return m.LogStatus(
        path="/nas/logs/test.log",
        mtime_epoch=1000.0,
        age_s=100.0,
        stale=stale,
        last_line=None,
    )


def test_derive_overall_status_all_ok():
    containers = [_container("sonarr"), _container("radarr")]
    arr = [_arr("sonarr"), _arr("radarr"), _arr("lidarr"), _arr("prowlarr")]
    result = m.derive_overall_status(containers, arr, _slskd(), None, [])
    assert result == "ok"


def test_derive_overall_status_no_containers_is_down():
    result = m.derive_overall_status([], [], None, None, [])
    assert result == "down"


def test_derive_overall_status_all_critical_unreachable_is_down():
    containers = [_container("sonarr")]
    arr = [
        _arr("sonarr", reachable=False),
        _arr("radarr", reachable=False),
        _arr("lidarr", reachable=False),
        _arr("prowlarr", reachable=False),
    ]
    result = m.derive_overall_status(containers, arr, None, None, [])
    assert result == "down"


def test_derive_overall_status_one_critical_unreachable_is_degraded():
    containers = [_container("sonarr")]
    arr = [
        _arr("sonarr", reachable=False),
        _arr("radarr"),
        _arr("lidarr"),
        _arr("prowlarr"),
    ]
    result = m.derive_overall_status(containers, arr, None, None, [])
    assert result == "degraded"


def test_derive_overall_status_unhealthy_container_is_degraded():
    containers = [_container("qbittorrent", healthy="unhealthy"), _container("sonarr")]
    arr = [_arr("sonarr"), _arr("radarr"), _arr("lidarr"), _arr("prowlarr")]
    result = m.derive_overall_status(containers, arr, None, None, [])
    assert result == "degraded"


def test_derive_overall_status_arr_error_issue_is_degraded():
    containers = [_container("sonarr")]
    arr_issues = [{"type": "error", "message": "database corrupted"}]
    arr = [
        _arr("sonarr", issues=arr_issues),
        _arr("radarr"), _arr("lidarr"), _arr("prowlarr"),
    ]
    result = m.derive_overall_status(containers, arr, None, None, [])
    assert result == "degraded"


def test_derive_overall_status_arr_warning_issue_is_ok():
    # Warning-level arr issues do NOT push to degraded by the current rules
    # (only error level does; warnings are surfaced in the summary).
    containers = [_container("sonarr")]
    arr_issues = [{"type": "warning", "message": "indexer unreachable"}]
    arr = [
        _arr("sonarr", issues=arr_issues),
        _arr("radarr"), _arr("lidarr"), _arr("prowlarr"),
    ]
    result = m.derive_overall_status(containers, arr, None, None, [])
    assert result == "ok"


def test_derive_overall_status_stale_log_is_degraded():
    containers = [_container("sonarr")]
    arr = [_arr("sonarr"), _arr("radarr"), _arr("lidarr"), _arr("prowlarr")]
    logs = [_log(stale=True)]
    result = m.derive_overall_status(containers, arr, None, None, logs)
    assert result == "degraded"


def test_derive_overall_status_slskd_down_is_degraded():
    containers = [_container("sonarr")]
    arr = [_arr("sonarr"), _arr("radarr"), _arr("lidarr"), _arr("prowlarr")]
    result = m.derive_overall_status(containers, arr, _slskd(reachable=False), None, [])
    assert result == "degraded"


def test_derive_overall_status_non_critical_arr_unreachable_is_degraded():
    # bazarr unreachable: it's not critical for "down" but is degraded
    containers = [_container("sonarr")]
    arr = [
        _arr("sonarr"), _arr("radarr"), _arr("lidarr"), _arr("prowlarr"),
        _arr("bazarr", reachable=False),
    ]
    result = m.derive_overall_status(containers, arr, None, None, [])
    assert result == "degraded"


# ---------------------------------------------------------------------------
# format_summary (structural checks)
# ---------------------------------------------------------------------------


def _make_report(overall: str = "ok") -> object:
    import datetime as _dt
    return m.OpsReport(
        generated_at=_dt.datetime(2026, 6, 10, 12, 0, 0, tzinfo=_dt.UTC).isoformat(),
        overall=overall,
        containers=[
            m.ContainerState("sonarr", "running", "Up 3 days (healthy)", "healthy"),
            m.ContainerState("gluetun", "running", "Up 1 day", ""),
        ],
        arr_services=[
            m.ArrStatus("sonarr", 8989, True, [], 12),
            m.ArrStatus("radarr", 7878, True, [{"type": "warning", "message": "no indexers"}], None),
            m.ArrStatus("lidarr", 8686, False, [], None, error="Connection refused"),
        ],
        slskd=m.SlskdStatus(reachable=True, version="0.21.0", downloads_total=5, uploads_total=2),
        qbittorrent=m.QbitStatus(reachable=True, torrent_count=23),
        logs=[
            m.LogStatus("/nas/logs/lidarr_backlog_drip.log", 1000.0, 1800.0, False, "drip: searched 20"),
            m.LogStatus("/nas/logs/slskd_cleanup.log", 100.0, 14400.0 + 1, True, "cleaned 0"),
        ],
    )


def test_format_summary_contains_service_names():
    report = _make_report()
    summary = m.format_summary(report)
    assert "sonarr" in summary
    assert "radarr" in summary
    assert "lidarr" in summary
    assert "slskd" in summary
    assert "qBittorrent" in summary


def test_format_summary_overall_line():
    for status, icon in [("ok", "✓"), ("degraded", "⚠"), ("down", "✗")]:
        report = _make_report(overall=status)
        summary = m.format_summary(report)
        assert icon in summary
        assert status.upper() in summary


def test_format_summary_unreachable_arr_shows_error():
    report = _make_report()
    summary = m.format_summary(report)
    assert "Connection refused" in summary


def test_format_summary_queue_depth_shown():
    report = _make_report()
    summary = m.format_summary(report)
    assert "queue=12" in summary


def test_format_summary_stale_log_shown():
    report = _make_report()
    summary = m.format_summary(report)
    assert "stale" in summary


def test_format_summary_slskd_version_shown():
    report = _make_report()
    summary = m.format_summary(report)
    assert "v0.21.0" in summary


def test_format_summary_torrent_count_shown():
    report = _make_report()
    summary = m.format_summary(report)
    assert "torrents=23" in summary


def test_format_summary_no_containers():
    import datetime as _dt
    report = m.OpsReport(
        generated_at=_dt.datetime(2026, 6, 10, tzinfo=_dt.UTC).isoformat(),
        overall="down",
        containers=[],
    )
    summary = m.format_summary(report)
    assert "no containers" in summary.lower()


# ---------------------------------------------------------------------------
# _extract_health (internal helper)
# ---------------------------------------------------------------------------


def test_extract_health_variants():
    assert m._extract_health("Up 3 days (healthy)") == "healthy"
    assert m._extract_health("Up 2 hours (unhealthy)") == "unhealthy"
    assert m._extract_health("Up 30 seconds (health: starting)") == "starting"
    assert m._extract_health("Up 5 minutes") == ""
    assert m._extract_health("Exited (0) 2 hours ago") == ""


# ---------------------------------------------------------------------------
# ListenBrainz music-loop tile (Phase 1)
# ---------------------------------------------------------------------------


def test_gather_listenbrainz_parses_latest_listen(monkeypatch):
    payload = {"payload": {"listens": [{"listened_at": 1_700_000_000}]}}

    def fake_get(url, headers):
        assert "user/tom/listens" in url
        return 200, json.dumps(payload)

    monkeypatch.setattr(m, "_http_get", fake_get)
    st = m.gather_listenbrainz(user="tom", token="tok", now_epoch=1_700_000_060.0)
    assert st.reachable is True
    assert st.last_listen_epoch == 1_700_000_000
    assert st.last_listen_age_s == 60.0


def test_gather_listenbrainz_no_user_is_unreachable():
    st = m.gather_listenbrainz(user=None, token=None, now_epoch=1.0)
    assert st.reachable is False
    assert "not set" in (st.error or "")


def test_gather_listenbrainz_http_error_never_raises(monkeypatch):
    def boom(url, headers):
        raise OSError("network down")

    monkeypatch.setattr(m, "_http_get", boom)
    st = m.gather_listenbrainz(user="tom", token="tok", now_epoch=1.0)
    assert st.reachable is False
    assert st.error


def test_report_dict_includes_listenbrainz():
    import datetime as _dt

    report = m.OpsReport(
        generated_at=_dt.datetime(2026, 6, 19, tzinfo=_dt.UTC).isoformat(),
        overall="ok",
        containers=[],
        arr_services=[],
        slskd=None,
        qbittorrent=None,
        logs=[],
        listenbrainz=m.ListenBrainzStatus(
            reachable=True, user="tom", last_listen_age_s=42.0
        ),
    )
    d = m._report_to_dict(report)
    assert d["listenbrainz"]["user"] == "tom"
    assert d["listenbrainz"]["last_listen_age_s"] == 42.0


# ---------------------------------------------------------------------------
# Disk health (ADR-0023)
# ---------------------------------------------------------------------------

# Trimmed from the real `smartctl -a -j /dev/nvme0` on this host, 2026-09-02.
_NVME_JSON = json.dumps(
    {
        "model_name": "SSSTC CL1-4D256",
        "smart_status": {"passed": True, "nvme": {"value": 0}},
        "nvme_smart_health_information_log": {
            "critical_warning": 0,
            "temperature": 50,
            "available_spare": 100,
            "available_spare_threshold": 10,
            "percentage_used": 42,
            "data_units_read": 133413447,
            "data_units_written": 61494562,
            "media_errors": 0,
            "num_err_log_entries": 0,
            "power_on_hours": 9796,
            "power_cycles": 6509,
            "unsafe_shutdowns": 582,
        },
    }
)


def test_parse_nvme_smart_extracts_the_metrics_we_act_on():
    got = m.parse_nvme_smart(_NVME_JSON)
    assert got["model"] == "SSSTC CL1-4D256"
    assert got["percentage_used"] == 42
    assert got["available_spare"] == 100
    assert got["unsafe_shutdowns"] == 582
    assert got["smart_passed"] is True
    # 1 NVMe data unit == 512000 bytes, per spec — not 512.
    assert got["written_tb"] == 31.49


def test_parse_nvme_smart_rejects_non_smart_payloads():
    for payload in ("", "not json", json.dumps({"model_name": "x"})):
        try:
            m.parse_nvme_smart(payload)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {payload!r}")


def test_classify_nvme_current_host_state_is_ok():
    status, notes = m.classify_nvme(m.parse_nvme_smart(_NVME_JSON))
    assert status == "ok"
    assert notes == []


def test_classify_nvme_wear_crosses_warn_then_crit():
    base = m.parse_nvme_smart(_NVME_JSON)
    warn, notes = m.classify_nvme({**base, "percentage_used": m.NVME_WEAR_WARN_PCT})
    assert warn == "warn" and "endurance" in notes[0]
    crit, _ = m.classify_nvme({**base, "percentage_used": m.NVME_WEAR_CRIT_PCT})
    assert crit == "crit"


def test_classify_nvme_media_errors_and_critical_warning_are_critical():
    base = m.parse_nvme_smart(_NVME_JSON)
    assert m.classify_nvme({**base, "media_errors": 1})[0] == "crit"
    assert m.classify_nvme({**base, "critical_warning": 4})[0] == "crit"
    assert m.classify_nvme({**base, "smart_passed": False})[0] == "crit"


def test_classify_nvme_spare_at_device_threshold_is_critical():
    base = m.parse_nvme_smart(_NVME_JSON)
    status, notes = m.classify_nvme({**base, "available_spare": 10})
    assert status == "crit"
    assert "threshold" in notes[0]


# Trimmed from the real `tune2fs -l /dev/sda1` on this host, 2026-09-02.
_TUNE2FS_CLEAN = """tune2fs 1.47.0 (5-Feb-2023)
Filesystem volume name:   <none>
Filesystem state:         clean
Mount count:              12
Maximum mount count:      -1
Last checked:             Fri May 22 17:49:45 2026
Lifetime writes:          15 TB
"""


def test_parse_tune2fs_reads_field_pairs():
    fields = m.parse_tune2fs(_TUNE2FS_CLEAN)
    assert fields["Filesystem state"] == "clean"
    assert fields["Lifetime writes"] == "15 TB"
    assert "FS Error count" not in fields


def test_classify_ext4_absent_error_count_is_healthy_not_unknown():
    # tune2fs omits `FS Error count` entirely when it is zero, so a missing
    # field is the GOOD case. Treating it as unknown would mute the check.
    status, notes = m.classify_ext4(m.parse_tune2fs(_TUNE2FS_CLEAN))
    assert status == "ok"
    assert notes == []


def test_classify_ext4_recorded_errors_are_critical():
    text = _TUNE2FS_CLEAN + "FS Error count:           7\nFirst error time:         Mon Sep  1 03:00:00 2026\n"
    status, notes = m.classify_ext4(m.parse_tune2fs(text))
    assert status == "crit"
    assert "7 ext4 error(s)" in notes[0]


def test_classify_ext4_unclean_state_is_critical():
    text = _TUNE2FS_CLEAN.replace("clean", "not clean with errors")
    status, notes = m.classify_ext4(m.parse_tune2fs(text))
    assert status == "crit"
    assert "not clean" in notes[0]


def _disk(status: str) -> object:
    return m.DiskHealth(device="/dev/x", kind="nvme", status=status)


def test_derive_overall_status_failing_disk_degrades_but_does_not_down():
    containers = [_container("sonarr")]
    arr = [_arr("sonarr"), _arr("radarr"), _arr("lidarr"), _arr("prowlarr")]
    assert m.derive_overall_status(containers, arr, _slskd(), None, [], [_disk("crit")]) == "degraded"
    assert m.derive_overall_status(containers, arr, _slskd(), None, [], [_disk("warn")]) == "degraded"


def test_derive_overall_status_unknown_disk_is_not_a_fault():
    # sudo may simply not be configured on another host; that must not paint
    # the whole stack degraded.
    containers = [_container("sonarr")]
    arr = [_arr("sonarr"), _arr("radarr"), _arr("lidarr"), _arr("prowlarr")]
    assert m.derive_overall_status(containers, arr, _slskd(), None, [], [_disk("unknown")]) == "ok"


def test_classify_ext4_clean_with_errors_is_critical():
    # The trap: "clean with errors" CONTAINS "clean". A substring test would be
    # green during exactly the failure this check exists to catch.
    text = _TUNE2FS_CLEAN.replace("state:         clean", "state:         clean with errors")
    status, notes = m.classify_ext4(m.parse_tune2fs(text))
    assert status == "crit"
    assert "clean with errors" in notes[0]
