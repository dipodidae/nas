"""Tests for scripts/playlist_sync_stage.py — pure logic, no docker needed."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def _load():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "playlist_sync_stage", scripts_dir / "playlist_sync_stage.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


ps = _load()


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _sse(*events):
    return "".join(f"data: {e}\n\n" for e in events)


# --- URL construction ---


def test_bounded_stage_gets_its_limit_applied():
    url = ps.build_url(ps.STAGES["lastfm-tracks"], 4000)
    assert url.endswith("/enrich/lastfm-tracks/stream?max_tracks=4000")


def test_album_stages_use_max_albums_not_max_tracks():
    """The param name differs per stage; guessing it silently runs unbounded."""
    assert ps.build_url(ps.STAGES["release-dates"], 50).endswith("?max_albums=50")
    assert ps.build_url(ps.STAGES["lastfm-album-tags"], 50).endswith("?max_albums=50")


def test_unbounded_stage_ignores_a_limit_rather_than_sending_a_bad_param():
    url = ps.build_url(ps.STAGES["clusters"], 500)
    assert url.endswith("/enrich/clusters/stream")
    assert "?" not in url


def test_no_limit_means_no_query_string():
    assert ps.build_url(ps.STAGES["lastfm-tracks"], None).endswith("/stream")


def test_every_stage_targets_a_stream_endpoint_except_the_one_that_has_none():
    """The plain endpoints are fire-and-forget, so a non-stream path is a bug."""
    non_streaming = [n for n, s in ps.STAGES.items() if not s.streaming]
    assert non_streaming == ["search-vectors"]
    for name, stage in ps.STAGES.items():
        if stage.streaming:
            assert stage.path.endswith("/stream"), name


# --- SSE parsing: a stream that merely stops is not a success ---


def test_a_done_event_is_a_completion():
    done, err, stats = ps.parse_stream(_sse('{"done": true, "stats": {"albums_tagged": 2}}'))
    assert done and not err and stats == {"albums_tagged": 2}


def test_a_stream_that_just_stops_is_not_a_completion():
    """Exactly the old failure: progress for hours, then silence, called success."""
    done, err, _ = ps.parse_stream(_sse('{"progress": 16, "message": "Albums: 2340/15507"}'))
    assert not done and not err


def test_an_error_event_is_surfaced():
    done, err, _ = ps.parse_stream(_sse('{"done": true, "error": "lastfm rate limited"}'))
    assert done and err == "lastfm rate limited"


def test_non_data_lines_and_garbage_are_skipped():
    body = "event: ping\n\ndata: not json\n\n" + _sse('{"done": true}')
    done, err, _ = ps.parse_stream(body)
    assert done and not err


def test_empty_stream_is_not_a_completion():
    assert ps.parse_stream("") == (False, "", {})


def test_a_non_dict_event_does_not_crash():
    done, _, _ = ps.parse_stream(_sse("[1, 2, 3]", '{"done": true}'))
    assert done


# --- exit codes ---


def test_completion_exits_zero():
    runner = lambda *a, **k: _proc(stdout=_sse('{"done": true, "stats": {"x": 1}}'))  # noqa: E731
    assert ps.run_stage("lastfm-tracks", 10, runner=runner) == 0


def test_stream_without_done_is_fatal_not_success():
    """It must not refresh the freshness clock; that is what hid this for weeks."""
    runner = lambda *a, **k: _proc(stdout=_sse('{"progress": 40}'))  # noqa: E731
    assert ps.run_stage("lastfm-tracks", 10, runner=runner) == 2


def test_stage_reported_error_is_fatal():
    runner = lambda *a, **k: _proc(stdout=_sse('{"done": true, "error": "boom"}'))  # noqa: E731
    assert ps.run_stage("embeddings", None, runner=runner) == 2


def test_http_409_is_come_back_later_not_a_fault():
    """A scan already running is normal for a drip and must not page."""
    runner = lambda *a, **k: _proc(returncode=22)  # noqa: E731
    assert ps.run_stage("scan", None, runner=runner) == 1


def test_other_curl_failures_are_fatal():
    runner = lambda *a, **k: _proc(returncode=7)  # noqa: E731
    assert ps.run_stage("scan", None, runner=runner) == 2


def test_docker_exec_blowing_up_is_fatal():
    def runner(*a, **k):
        raise OSError("no docker")
    assert ps.run_stage("scan", None, runner=runner) == 2


def test_non_streaming_stage_succeeds_on_a_plain_body():
    runner = lambda *a, **k: _proc(stdout='{"status": "ok"}')  # noqa: E731
    assert ps.run_stage("search-vectors", None, runner=runner) == 0


# --- availability gate ---


def test_container_absent_is_reported_not_probed():
    runner = lambda *a, **k: _proc(stdout="sonarr\nradarr\n")  # noqa: E731
    assert ps.container_available(runner=runner) is False


def test_container_up_but_backend_unhealthy_is_unavailable():
    """Up-but-starting must not look like a broken stage (ADR-0026)."""
    def runner(cmd, *a, **k):
        if cmd[1] == "ps":
            return _proc(stdout="playlist-generator\n")
        return _proc(returncode=7)
    assert ps.container_available(runner=runner) is False


def test_container_up_and_healthy_is_available():
    def runner(cmd, *a, **k):
        if cmd[1] == "ps":
            return _proc(stdout="playlist-generator\n")
        return _proc(stdout='{"status": "ok"}')
    assert ps.container_available(runner=runner) is True


def test_docker_itself_failing_is_unavailable_not_a_crash():
    def runner(*a, **k):
        raise subprocess.TimeoutExpired("docker", 30)
    assert ps.container_available(runner=runner) is False


# --- CLI ---


def test_list_exits_zero_and_names_every_stage(capsys):
    assert ps.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name in ps.STAGES:
        assert name in out


def test_missing_stage_is_a_usage_error():
    assert ps.main([]) == 2


# --- ordered multi-stage runs ---


def test_stages_run_in_the_order_given(monkeypatch):
    """Derived stages are computed FROM earlier ones, so order is not cosmetic."""
    ran = []
    monkeypatch.setattr(ps, "container_available", lambda *a, **k: True)
    monkeypatch.setattr(ps, "run_stage", lambda n, limit: ran.append(n) or 0)
    assert ps.main(["--stage", "embeddings", "profiles", "clusters"]) == 0
    assert ran == ["embeddings", "profiles", "clusters"]


def test_a_fatal_stage_stops_the_chain(monkeypatch):
    """Continuing would build a later stage on data the failed one never wrote."""
    ran = []
    monkeypatch.setattr(ps, "container_available", lambda *a, **k: True)
    monkeypatch.setattr(ps, "run_stage",
                        lambda n, limit: (ran.append(n), 2 if n == "profiles" else 0)[1])
    assert ps.main(["--stage", "embeddings", "profiles", "clusters"]) == 2
    assert ran == ["embeddings", "profiles"], "clusters must be skipped"


def test_a_partial_stage_does_not_stop_the_chain(monkeypatch):
    """exit 1 is 'nothing to do right now', which the next stage can live with."""
    ran = []
    monkeypatch.setattr(ps, "container_available", lambda *a, **k: True)
    monkeypatch.setattr(ps, "run_stage",
                        lambda n, limit: (ran.append(n), 1 if n == "embeddings" else 0)[1])
    assert ps.main(["--stage", "embeddings", "profiles"]) == 1
    assert ran == ["embeddings", "profiles"]


def test_unavailable_backend_short_circuits_every_stage(monkeypatch):
    ran = []
    monkeypatch.setattr(ps, "container_available", lambda *a, **k: False)
    monkeypatch.setattr(ps, "run_stage", lambda n, limit: ran.append(n) or 0)
    assert ps.main(["--stage", "embeddings", "profiles"]) == 1
    assert ran == []
