"""Tests for scripts/notify.py — the lane router every publisher goes through.

The five properties worth pinning are the ones that fail *silently*:

* a lane mapped to the wrong priority still returns HTTP 200;
* a cooldown that does not suppress produces 288 messages a day and is only
  noticeable on the phone;
* a `transition()` that fires on every poll looks identical to one that fires
  on the edge, until you count;
* a quiet-hours delay applied to `nas-critical` loses the one message that
  matters, and nothing logs it;
* a notifier that raises takes down the job it was bolted onto — the exact
  inversion of what an alerter is for.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


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


nt = _load("notify")


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
  """No real token, no real topic override, no shared state file."""
  for key in list(nt.os.environ):
    if key.startswith(("NTFY_TOPIC_", "NTFY_QUIET_HOURS")):
      monkeypatch.delenv(key, raising=False)
  monkeypatch.setenv("NTFY_URL", "http://ntfy.invalid:8410")
  monkeypatch.setenv("NTFY_TOKEN_SCRIPTS", "tk_test")
  monkeypatch.setattr(nt, "DEFAULT_STATE", tmp_path / ".notify_state.json")
  monkeypatch.setattr(nt, "RETRY_SLEEP_S", 0.0)


@pytest.fixture
def sent(monkeypatch):
  """Capture publishes instead of making them. Returns the list of Messages."""
  captured: list = []

  def _fake(msg):
    captured.append(msg)
    return True, "HTTP 200"

  monkeypatch.setattr(nt, "publish", _fake)
  return captured


# --- lane -> priority / topic / tag mapping ------------------------------


def test_every_lane_has_a_default_priority():
  """A lane with no priority is a lane that silently delivers as `default`."""
  assert set(nt.LANES) == set(nt.Lane)
  for lane, spec in nt.LANES.items():
    assert 1 <= spec.priority <= 5, lane


def test_priority_matches_the_documented_table():
  assert nt.priority_for(nt.Lane.CRITICAL) == 5
  assert nt.priority_for(nt.Lane.ATTENTION) == 4
  assert nt.priority_for(nt.Lane.MEDIA) == 3
  assert nt.priority_for(nt.Lane.REQUESTS) == 4
  assert nt.priority_for(nt.Lane.INFRA) == 2
  assert nt.priority_for(nt.Lane.UPDATES) == 1


def test_priority_override_is_clamped_to_ntfy_range():
  assert nt.priority_for(nt.Lane.INFRA, 9) == 5
  assert nt.priority_for(nt.Lane.INFRA, 0) == 1
  assert nt.priority_for(nt.Lane.INFRA, 4) == 4


def test_topic_defaults_to_nas_prefixed_lane_name():
  for lane in nt.Lane:
    assert nt.topic_for(lane, {}) == f"nas-{lane.value}"


def test_topic_is_overridable_from_the_environment():
  assert nt.topic_for(nt.Lane.MEDIA, {"NTFY_TOPIC_MEDIA": "elsewhere"}) == "elsewhere"
  # An empty override must not produce an empty topic — that would POST to the
  # server root and 404 with no clue why.
  assert nt.topic_for(nt.Lane.MEDIA, {"NTFY_TOPIC_MEDIA": "  "}) == "nas-media"


def test_lane_name_accepts_the_topic_spelling_too():
  """`--lane nas-media` and `--lane media` must mean the same thing."""
  assert nt.lane_of("nas-media") is nt.Lane.MEDIA
  assert nt.lane_of("MEDIA") is nt.Lane.MEDIA
  with pytest.raises(ValueError):
    nt.lane_of("nas-alerts")


def test_tags_fall_back_to_the_lane_tag_but_media_requires_one():
  assert nt.tags_for(nt.Lane.CRITICAL) == nt.TAG_CRITICAL
  assert nt.tags_for(nt.Lane.INFRA) == nt.TAG_INFRA
  # nas-media has no single icon; the caller says which kind it is.
  assert nt.tags_for(nt.Lane.MEDIA) == ""
  assert nt.tags_for(nt.Lane.MEDIA, ("tv",)) == "tv"


def test_headers_carry_the_documented_names():
  msg = nt.build_message(
    nt.Lane.MEDIA, "📺 Show S01E01", "body",
    tags=("tv",), click="https://example.invalid", markdown=True,
  )
  assert msg.headers["X-Title"] == "📺 Show S01E01"
  assert msg.headers["X-Priority"] == "3"
  assert msg.headers["X-Tags"] == "tv"
  assert msg.headers["X-Click"] == "https://example.invalid"
  assert msg.headers["X-Markdown"] == "true"
  assert msg.headers["Authorization"] == "Bearer tk_test"
  assert msg.url.endswith("/nas-media")


def test_no_authorization_header_when_no_token_is_configured(monkeypatch):
  monkeypatch.delenv("NTFY_TOKEN_SCRIPTS", raising=False)
  msg = nt.build_message(nt.Lane.INFRA, "t", "m", token="")
  assert "Authorization" not in msg.headers


# --- cooldown / dedup suppression ---------------------------------------


def test_same_dedup_key_inside_its_cooldown_is_suppressed_and_counted(sent):
  first = nt.notify(nt.Lane.ATTENTION, "t", "m", dedup_key="k", cooldown=60)
  second = nt.notify(nt.Lane.ATTENTION, "t", "m", dedup_key="k", cooldown=60)
  assert first.sent and not first.suppressed
  assert second.suppressed and not second.sent
  assert len(sent) == 1, "the second publish must not reach the wire"
  assert nt.suppressed_since(nt.load_state()) == 1
  assert nt.suppressed_since(nt.load_state(), ["k"]) == 1


def test_a_new_key_is_not_suppressed_by_another_keys_cooldown(sent):
  nt.notify(nt.Lane.ATTENTION, "t", "m", dedup_key="a", cooldown=60)
  assert nt.notify(nt.Lane.ATTENTION, "t", "m", dedup_key="b", cooldown=60).sent
  assert len(sent) == 2


def test_cooldown_expires(sent, monkeypatch):
  nt.notify(nt.Lane.INFRA, "t", "m", dedup_key="k", cooldown=10)
  real = time.time
  monkeypatch.setattr(nt.time, "time", lambda: real() + 11 * 60)
  assert nt.notify(nt.Lane.INFRA, "t", "m", dedup_key="k", cooldown=10).sent
  assert len(sent) == 2


def test_no_dedup_key_means_no_suppression(sent):
  for _ in range(3):
    nt.notify(nt.Lane.INFRA, "t", "m")
  assert len(sent) == 3


def test_lane_defaults_match_the_documented_cooldowns():
  assert nt.cooldown_seconds(nt.Lane.ATTENTION, None) == 6 * 3600
  assert nt.cooldown_seconds(nt.Lane.INFRA, None) == 3600
  assert nt.cooldown_seconds(nt.Lane.MEDIA, None) == 0


def test_critical_is_never_cooldown_suppressed(sent):
  """The one lane where a swallowed message is the failure mode itself."""
  assert nt.cooldown_seconds(nt.Lane.CRITICAL, 600) == 0.0
  for _ in range(4):
    result = nt.notify(nt.Lane.CRITICAL, "t", "m", dedup_key="same", cooldown=600)
    assert result.sent and not result.suppressed
  assert len(sent) == 4


# --- transition-only semantics ------------------------------------------


def test_transition_fires_once_on_the_edge_not_once_per_poll(sent):
  """A */5 job must not be able to send the same message 288 times a day."""
  for _ in range(288):
    nt.transition(
      "container:foo:unhealthy", active=True,
      lane=nt.Lane.INFRA, title="foo unhealthy", message="been up 3m",
    )
  assert len(sent) == 1, f"288 polls produced {len(sent)} messages"


def test_transition_fires_again_when_the_detail_changes(sent):
  nt.transition("idx:down", active=True, lane=nt.Lane.ATTENTION,
                title="indexers", message="3 down", fingerprint="3")
  nt.transition("idx:down", active=True, lane=nt.Lane.ATTENTION,
                title="indexers", message="3 down", fingerprint="3")
  nt.transition("idx:down", active=True, lane=nt.Lane.ATTENTION,
                title="indexers", message="5 down", fingerprint="5")
  assert len(sent) == 2
  assert sent[-1].body == b"5 down"


def test_transition_clear_sends_exactly_one_low_priority_resolved(sent):
  nt.transition("svc:down", active=True, lane=nt.Lane.CRITICAL,
                title="svc gone", message="no container")
  nt.transition("svc:down", active=False, lane=nt.Lane.CRITICAL, title="x", message="y")
  nt.transition("svc:down", active=False, lane=nt.Lane.CRITICAL, title="x", message="y")
  assert len(sent) == 2, "the clear must fire once, not on every subsequent poll"
  clear = sent[-1]
  assert clear.lane is nt.Lane.INFRA, "a recovery belongs in nas-infra, not the alert lane"
  assert clear.headers["X-Priority"] == "2"
  assert clear.headers["X-Tags"] == nt.TAG_RESOLVED


def test_transition_does_not_announce_a_clear_that_never_alerted(sent):
  assert nt.transition("never:seen", active=False, lane=nt.Lane.INFRA,
                       title="x", message="y").suppressed
  assert sent == []


def test_transition_state_survives_a_reload(sent, tmp_path):
  path = tmp_path / "state.json"
  nt.transition("k", active=True, lane=nt.Lane.INFRA, title="t", message="m", state_path=path)
  nt.transition("k", active=True, lane=nt.Lane.INFRA, title="t", message="m", state_path=path)
  assert len(sent) == 1
  data = json.loads(path.read_text())
  assert data["conditions"]["k"]["active"] is True


# --- quiet hours ---------------------------------------------------------


def _amsterdam(hour: int) -> datetime:
  return datetime(2026, 9, 3, hour, 30, tzinfo=ZoneInfo(nt.LOCAL_TZ))


def test_quiet_window_wraps_midnight():
  assert nt.is_quiet_hour(_amsterdam(23)) is True
  assert nt.is_quiet_hour(_amsterdam(2)) is True
  assert nt.is_quiet_hour(_amsterdam(7)) is True
  assert nt.is_quiet_hour(_amsterdam(8)) is False
  assert nt.is_quiet_hour(_amsterdam(22)) is False


def test_chatter_lanes_are_delayed_to_8am_inside_quiet_hours():
  for lane in (nt.Lane.MEDIA, nt.Lane.INFRA, nt.Lane.UPDATES):
    assert nt.delay_for(lane, _amsterdam(1)) == "8am", lane
    assert nt.delay_for(lane, _amsterdam(12)) is None, lane


def test_critical_and_requests_are_never_delayed():
  for lane in (nt.Lane.CRITICAL, nt.Lane.REQUESTS):
    assert nt.delay_for(lane, _amsterdam(1)) is None, lane
    assert nt.delay_for(lane, _amsterdam(3)) is None, lane


def test_quiet_hours_can_be_disabled_and_overridden(monkeypatch):
  monkeypatch.setenv("NTFY_QUIET_HOURS", "")
  assert nt.delay_for(nt.Lane.INFRA, _amsterdam(1)) is None
  monkeypatch.setenv("NTFY_QUIET_HOURS", "20-6")
  assert nt.delay_for(nt.Lane.INFRA, _amsterdam(21)) == "6am"
  assert nt.delay_for(nt.Lane.INFRA, _amsterdam(7)) is None
  # A malformed override must fall back to the documented window, not crash.
  monkeypatch.setenv("NTFY_QUIET_HOURS", "not-a-window")
  assert nt.delay_for(nt.Lane.INFRA, _amsterdam(1)) == "8am"


def test_build_message_never_puts_a_delay_on_critical(monkeypatch):
  monkeypatch.setattr(nt, "delay_for", lambda *_a, **_k: "8am")
  assert "X-Delay" not in nt.build_message(nt.Lane.CRITICAL, "t", "m").headers
  assert nt.build_message(nt.Lane.INFRA, "t", "m").headers["X-Delay"] == "8am"


# --- failure is never fatal ---------------------------------------------


def test_a_dead_server_does_not_raise(monkeypatch):
  def _boom(_req, **_kw):
    raise OSError("connection refused")

  monkeypatch.setattr(nt.urllib.request, "urlopen", _boom)
  result = nt.notify(nt.Lane.CRITICAL, "t", "m")
  assert result.sent is False
  assert bool(result) is False
  assert "refused" in result.reason


def test_a_403_is_not_retried(monkeypatch):
  calls = {"n": 0}

  def _forbidden(_req, **_kw):
    calls["n"] += 1
    raise nt.urllib.error.HTTPError("u", 403, "Forbidden", {}, None)  # type: ignore[arg-type]

  monkeypatch.setattr(nt.urllib.request, "urlopen", _forbidden)
  assert nt.notify(nt.Lane.MEDIA, "t", "m").sent is False
  assert calls["n"] == 1, "an ACL rejection is a config bug; retrying only doubles the noise"


def test_a_transport_error_is_retried_once(monkeypatch):
  calls = {"n": 0}

  def _flaky(_req, **_kw):
    calls["n"] += 1
    raise OSError("timed out")

  monkeypatch.setattr(nt.urllib.request, "urlopen", _flaky)
  nt.notify(nt.Lane.INFRA, "t", "m")
  assert calls["n"] == nt.POST_ATTEMPTS == 2


def test_an_unknown_lane_returns_a_result_rather_than_raising():
  result = nt.notify("nas-alerts", "t", "m")
  assert result.sent is False
  assert "unknown lane" in result.reason


def test_a_failed_publish_does_not_start_a_cooldown(monkeypatch):
  monkeypatch.setattr(nt, "publish", lambda _m: (False, "HTTP 500"))
  nt.notify(nt.Lane.ATTENTION, "t", "m", dedup_key="k", cooldown=60)
  # If a failure recorded last_sent, the retry five minutes later would be
  # suppressed and the alert would be lost entirely.
  assert nt.should_send(nt.load_state(), "k", 3600, time.time()) is True


def test_unwritable_state_file_does_not_raise(sent, monkeypatch, tmp_path):
  target = tmp_path / "nope"
  target.mkdir()
  monkeypatch.setattr(nt, "DEFAULT_STATE", target)  # a directory, not a file
  assert nt.notify(nt.Lane.INFRA, "t", "m", dedup_key="k", cooldown=60).sent


# --- state hygiene -------------------------------------------------------


def test_prune_drops_only_stale_entries():
  state = nt.State(
    cooldowns={"old": {"last_sent": 0.0}, "fresh": {"last_sent": time.time()}},
    conditions={"stale": {"changed_at": 0.0}, "live": {"changed_at": time.time()}},
  )
  assert nt.prune_state(state, time.time()) == 2
  assert set(state.cooldowns) == {"fresh"}
  assert set(state.conditions) == {"live"}


def test_state_roundtrips_through_the_file(tmp_path):
  path = tmp_path / "s.json"
  nt.save_state(nt.State(cooldowns={"k": {"last_sent": 1.0}}, suppressed_total=7), path)
  loaded = nt.load_state(path)
  assert loaded.suppressed_total == 7
  assert loaded.cooldowns["k"]["last_sent"] == 1.0


def test_a_corrupt_state_file_reads_as_empty(tmp_path):
  path = tmp_path / "s.json"
  path.write_text("{not json")
  assert nt.load_state(path).cooldowns == {}


# --- CLI -----------------------------------------------------------------


def test_cli_publishes_through_the_router(sent):
  code = nt.main(["--lane", "infra", "--title", "t", "--message", "m"])
  assert code == 0
  assert len(sent) == 1
  assert sent[0].lane is nt.Lane.INFRA


def test_cli_rejects_an_unknown_lane_with_exit_2():
  assert nt.main(["--lane", "alerts", "--title", "t", "--message", "m"]) == 2


def test_cli_rejects_empty_text_with_exit_2():
  assert nt.main(["--lane", "infra", "--title", " ", "--message", "m"]) == 2


def test_cli_reports_a_delivery_failure_as_exit_1(monkeypatch):
  monkeypatch.setattr(nt, "publish", lambda _m: (False, "HTTP 500"))
  assert nt.main(["--lane", "infra", "--title", "t", "--message", "m"]) == 1


# --- credential freshness ------------------------------------------------


def test_the_token_is_read_at_call_time_not_cached_at_import(monkeypatch):
    """A rotated token must take effect on the next publish, with no restart.

    These tokens have been disclosed once already, so rotation is a routine
    operation and not an emergency. If the router ever caches the token in a
    module-level constant, every publisher keeps presenting the revoked one and
    fails with a 403 that looks like an ACL bug.
    """
    monkeypatch.setenv("NTFY_TOKEN_SCRIPTS", "tk_first")
    assert nt.build_message(nt.Lane.INFRA, "t", "m").headers["Authorization"] == "Bearer tk_first"
    monkeypatch.setenv("NTFY_TOKEN_SCRIPTS", "tk_rotated")
    assert nt.build_message(nt.Lane.INFRA, "t", "m").headers["Authorization"] == "Bearer tk_rotated"


def test_the_topic_is_read_at_call_time_too(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC_INFRA", "one")
    assert nt.build_message(nt.Lane.INFRA, "t", "m").url.endswith("/one")
    monkeypatch.setenv("NTFY_TOPIC_INFRA", "two")
    assert nt.build_message(nt.Lane.INFRA, "t", "m").url.endswith("/two")


# --- non-ASCII titles ----------------------------------------------------


def test_a_non_ascii_title_reaches_the_wire_as_utf8():
  """http.client encodes headers as latin-1, so an em dash or an emoji in
  X-Title raises UnicodeEncodeError — caught as a ValueError and reported as a
  failed publish, with the message simply never sent. Every title in this
  module's own vocabulary is affected: 📺, 🎵, 🗒, and the em dash."""
  for title in ("📺 The Expanse S02E07", "🎵 Boards of Canada — Geogaddi",
                "🗒 NAS digest · Thu 03 Sep", "TEST nas-critical — no container"):
    wire = nt._wire_headers({"X-Title": title})
    # The whole point: latin-1 can now encode it, and the bytes are the
    # original UTF-8 — byte-identical to what curl sends.
    assert wire["X-Title"].encode("latin-1") == title.encode("utf-8")


def test_an_emoji_title_actually_publishes(monkeypatch):
  """The end-to-end version of the above, through the real urllib path."""
  seen: dict = {}

  class _Resp:
    status = 200

    def __enter__(self):
      return self

    def __exit__(self, *_a):
      return False

  def _capture(req, **_kw):
    # This is where it used to raise: http.client encodes the header values.
    for value in req.headers.values():
      value.encode("latin-1")
    seen["title"] = req.get_header("X-title")
    return _Resp()

  monkeypatch.setattr(nt.urllib.request, "urlopen", _capture)
  result = nt.notify(nt.Lane.MEDIA, "🎵 Artist — Album", "body", tags=("musical_note",))
  assert result.sent, result.reason
  assert seen["title"].encode("latin-1").decode("utf-8") == "🎵 Artist — Album"


def test_ascii_headers_are_unchanged():
  assert nt._wire_headers({"X-Priority": "5"}) == {"X-Priority": "5"}
