"""Tests for scripts/notify_digest.py — the one message that replaces chatter.

The digest is the counterweight to the six-lane split: the split works by *not*
sending things, and this is what stops "not sent" and "not happening" looking
identical. So the properties that matter are the ones where it would lie
quietly:

* a section that cannot collect must produce a visible gap and exit 1, not an
  empty digest that reads as good news;
* a failing container, a full disk, an OOM kill and a stale backup must each be
  detectable in the rendered text, or the digest is decoration;
* no collector may raise, because a digest that crashes is a digest that stops
  arriving — and nothing reports the absence of a report.
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


dg = _load("notify_digest")


# --- rendering -----------------------------------------------------------


def test_render_emits_one_block_per_section():
  text = dg.render([
    dg.Section("Containers", ["all running"]),
    dg.Section("Disk", ["56% used", "clean"]),
  ])
  assert "**Containers**" in text
  assert "- all running" in text
  assert "**Disk**" in text
  assert text.count("- ") == 3


def test_an_empty_section_says_so_rather_than_rendering_blank():
  """A silent gap reads as good news. It must read as a gap."""
  text = dg.render([dg.Section("Imports", [])])
  assert "(nothing collected)" in text


def test_render_is_pure():
  sections = [dg.Section("A", ["x"])]
  before = json.dumps([[s.heading, s.lines, s.failed] for s in sections])
  dg.render(sections)
  after = json.dumps([[s.heading, s.lines, s.failed] for s in sections])
  assert before == after


# --- containers ----------------------------------------------------------


def _fake_run(monkeypatch, mapping):
  """Stub _run by matching on a substring of the joined command."""
  def _run(cmd, timeout=30.0):  # noqa: ARG001
    joined = " ".join(cmd)
    for needle, (code, out) in mapping.items():
      if needle in joined:
        return code, out
    return 1, ""
  monkeypatch.setattr(dg, "_run", _run)


def test_a_service_with_no_container_is_reported_and_fails_the_section(monkeypatch):
  """ADR-0006's failure mode is the one line this section exists for."""
  _fake_run(monkeypatch, {
    "compose ps": (0, "jellyfin\tUp 3 hours (healthy)\n"),
    "config --services": (0, "jellyfin\nautoheal\n"),
  })
  section = dg.containers_section()
  text = "\n".join(section.lines)
  assert "no container at all: autoheal" in text
  assert section.failed


def test_an_unhealthy_container_is_reported_but_does_not_fail_the_section(monkeypatch):
  """Unhealthy is the watchdog's job with its own ladder; here it is a line."""
  _fake_run(monkeypatch, {
    "compose ps": (0, "jellyfin\tUp 3 hours (unhealthy)\n"),
    "config --services": (0, "jellyfin\n"),
  })
  section = dg.containers_section()
  assert "unhealthy: jellyfin" in "\n".join(section.lines)
  assert not section.failed


def test_an_exited_container_fails_the_section(monkeypatch):
  _fake_run(monkeypatch, {
    "compose ps": (0, "qbittorrent\tExited (137) 2 hours ago\n"),
    "config --services": (0, "qbittorrent\n"),
  })
  section = dg.containers_section()
  assert "not running: qbittorrent" in "\n".join(section.lines)
  assert section.failed


def test_unreadable_docker_is_a_visible_gap(monkeypatch):
  _fake_run(monkeypatch, {})
  section = dg.containers_section()
  assert section.failed
  assert section.lines


# --- disk ----------------------------------------------------------------


def test_a_full_disk_is_flagged(monkeypatch):
  import collections  # noqa: PLC0415

  usage = collections.namedtuple("u", "total used free")
  monkeypatch.setattr(dg.shutil, "disk_usage", lambda _p: usage(1000, 950, 50))
  _fake_run(monkeypatch, {})
  assert "⚠️" in "\n".join(dg.disk_section("/mnt/drive").lines)


def test_a_healthy_disk_is_not_flagged(monkeypatch):
  import collections  # noqa: PLC0415

  usage = collections.namedtuple("u", "total used free")
  monkeypatch.setattr(dg.shutil, "disk_usage", lambda _p: usage(1000, 500, 500))
  _fake_run(monkeypatch, {})
  section = dg.disk_section("/mnt/drive")
  assert "⚠️" not in "\n".join(section.lines)
  assert not section.failed


def test_a_missing_mount_is_a_gap_not_a_crash(monkeypatch):
  def _boom(_p):
    raise OSError("no such file")

  monkeypatch.setattr(dg.shutil, "disk_usage", _boom)
  section = dg.disk_section("/mnt/gone")
  assert section.failed
  assert "unreadable" in "\n".join(section.lines)


def test_clean_with_errors_is_not_treated_as_clean(monkeypatch):
  """`clean with errors` CONTAINS `clean`. Equality, not substring — the trap
  ADR-0023 already pinned once, re-pinned here because this is a second reader
  of the same tune2fs output."""
  import collections  # noqa: PLC0415

  usage = collections.namedtuple("u", "total used free")
  monkeypatch.setattr(dg.shutil, "disk_usage", lambda _p: usage(1000, 100, 900))
  _fake_run(monkeypatch, {
    "findmnt": (0, "/dev/sda1\n"),
    "tune2fs": (0, "Filesystem state:         clean with errors\nFS Error count:           7\n"),
  })
  section = dg.disk_section("/mnt/drive")
  assert section.failed
  assert "clean with errors" in "\n".join(section.lines)


def test_a_zero_error_count_is_absent_from_tune2fs_and_that_is_healthy(monkeypatch):
  """tune2fs omits `FS Error count` entirely when it is zero."""
  import collections  # noqa: PLC0415

  usage = collections.namedtuple("u", "total used free")
  monkeypatch.setattr(dg.shutil, "disk_usage", lambda _p: usage(1000, 100, 900))
  _fake_run(monkeypatch, {
    "findmnt": (0, "/dev/sda1\n"),
    "tune2fs": (0, "Filesystem state:         clean\n"),
  })
  section = dg.disk_section("/mnt/drive")
  assert not section.failed
  assert "error count 0" in "\n".join(section.lines)


# --- OOM -----------------------------------------------------------------


def test_an_oom_kill_is_counted_and_fails_the_section(monkeypatch):
  _fake_run(monkeypatch, {
    "journalctl": (0, "2026-09-03T04:00:00 host kernel: Out of memory: Killed process 1 (jellyfin)\n"),
  })
  section = dg.oom_section()
  assert section.failed
  assert "**1**" in "\n".join(section.lines)


def test_no_oom_kills_is_the_quiet_case(monkeypatch):
  _fake_run(monkeypatch, {"journalctl": (0, "nothing interesting\n")})
  section = dg.oom_section()
  assert not section.failed


def test_an_unreadable_kernel_log_is_a_gap(monkeypatch):
  _fake_run(monkeypatch, {})
  assert dg.oom_section().failed


# --- cron ----------------------------------------------------------------


def _cron_state(tmp_path, name, **fields):
  d = tmp_path / "cron-state"
  d.mkdir(exist_ok=True)
  (d / f"{name}.json").write_text(json.dumps(fields))
  return d


def test_a_failing_job_is_named_with_its_streak(tmp_path):
  d = _cron_state(tmp_path, "album-art", failing_since=1.0, last_exit=2,
                  consecutive_failures=3, max_age_min=10380)
  section = dg.cron_section(d)
  assert section.failed
  assert "album-art (exit 2, ×3)" in "\n".join(section.lines)


def test_an_overdue_job_is_reported_as_a_warning_not_a_failure(tmp_path):
  d = _cron_state(tmp_path, "media-ops-status", max_age_min=30,
                  last_success=time.time() - 4 * 3600)
  section = dg.cron_section(d)
  assert "overdue" in "\n".join(section.lines)
  assert not section.failed


def test_a_healthy_job_produces_no_finding(tmp_path):
  d = _cron_state(tmp_path, "heartbeat", max_age_min=40, last_success=time.time())
  section = dg.cron_section(d)
  assert not section.failed
  assert "no failures or overdue jobs" in "\n".join(section.lines)


def test_a_corrupt_cron_state_file_is_reported_not_skipped(tmp_path):
  d = tmp_path / "cron-state"
  d.mkdir()
  (d / "broken.json").write_text("{not json")
  assert "broken (unreadable)" in "\n".join(dg.cron_section(d).lines)


def test_a_missing_cron_state_dir_is_a_gap(tmp_path):
  assert dg.cron_section(tmp_path / "nope").failed


# --- backup --------------------------------------------------------------


def test_a_recent_backup_reports_its_age(tmp_path, monkeypatch):
  d = _cron_state(tmp_path, "config-backup", last_success=time.time() - 3 * 3600)
  monkeypatch.setattr(dg, "CRON_STATE_DIR", d)
  section = dg.backup_section()
  assert not section.failed
  assert "3h ago" in "\n".join(section.lines)


def test_a_backup_older_than_30h_fails_the_section(tmp_path, monkeypatch):
  d = _cron_state(tmp_path, "config-backup", last_success=time.time() - 40 * 3600)
  monkeypatch.setattr(dg, "CRON_STATE_DIR", d)
  assert dg.backup_section().failed


def test_a_backup_that_never_succeeded_fails_the_section(tmp_path, monkeypatch):
  d = _cron_state(tmp_path, "config-backup", registered=1.0)
  monkeypatch.setattr(dg, "CRON_STATE_DIR", d)
  section = dg.backup_section()
  assert section.failed
  assert "never succeeded" in "\n".join(section.lines)


# --- imports -------------------------------------------------------------


def test_import_counts_come_from_the_event_names_the_apis_actually_use():
  """`episodeFileImported` does not exist in Sonarr and counted zero forever."""
  assert dg.ARR_APPS["sonarr"][3] == "downloadFolderImported"
  assert dg.ARR_APPS["radarr"][3] == "downloadFolderImported"
  assert dg.ARR_APPS["lidarr"][3] == "trackFileImported"


def test_count_events_is_pure_and_exact():
  records = [
    {"eventType": "grabbed"},
    {"eventType": "downloadFolderImported"},
    {"eventType": "downloadFolderImported"},
    {"eventType": "downloadFailed"},
  ]
  assert dg.count_events(records, "downloadFolderImported") == 2
  assert dg.count_events(records, "downloadFailed") == 1
  assert dg.count_events(records, "nothingLikeThis") == 0


def test_imports_uses_the_since_endpoint_not_a_single_page(monkeypatch):
  """A 200-row page of Sonarr history reached back only a few hours here, so a
  page-based count reported 0 imports for a day that had some."""
  seen: list[str] = []

  def _get(url, _headers, timeout=10.0):  # noqa: ARG001
    seen.append(url)
    return [{"eventType": "downloadFolderImported"}, {"eventType": "downloadFailed"}]

  monkeypatch.setattr(dg, "_get_json", _get)
  for env in ("API_KEY_SONARR", "API_KEY_RADARR", "API_KEY_LIDARR"):
    monkeypatch.setenv(env, "k")
  section = dg.imports_section()
  assert all("/history/since?date=" in u for u in seen), seen
  assert "1** imported, 1 failed" in "\n".join(section.lines)


def test_a_missing_api_key_is_a_visible_gap(monkeypatch):
  for env in ("API_KEY_SONARR", "API_KEY_RADARR", "API_KEY_LIDARR"):
    monkeypatch.delenv(env, raising=False)
  section = dg.imports_section()
  assert section.failed
  assert "API_KEY_SONARR` unset" in "\n".join(section.lines)


def test_an_unreadable_history_is_a_gap_not_a_zero(monkeypatch):
  """Zero imports and "could not ask" must not render the same."""
  monkeypatch.setattr(dg, "_get_json", lambda *_a, **_k: None)
  for env in ("API_KEY_SONARR", "API_KEY_RADARR", "API_KEY_LIDARR"):
    monkeypatch.setenv(env, "k")
  section = dg.imports_section()
  assert section.failed
  assert "history unreadable" in "\n".join(section.lines)


# --- slskd ---------------------------------------------------------------


def test_a_logged_out_slskd_is_flagged(monkeypatch):
  monkeypatch.setenv("API_KEY_SLSKD", "k")
  monkeypatch.setattr(dg, "_get_json", lambda *_a, **_k: {"isLoggedIn": False, "state": "Connected"})
  assert "LOGGED OUT" in "\n".join(dg.slskd_section().lines)


def test_slskd_with_no_http_listener_is_not_a_digest_failure(monkeypatch):
  """For ~2h after a cold start slskd has no listener at all (ADR-0026)."""
  monkeypatch.setenv("API_KEY_SLSKD", "k")
  monkeypatch.setattr(dg, "_get_json", lambda *_a, **_k: None)
  section = dg.slskd_section()
  assert not section.failed
  assert "share rescan" in "\n".join(section.lines)


# --- suppressed ----------------------------------------------------------


def test_the_suppressed_count_is_reported_per_lane(monkeypatch, tmp_path):
  state = dg.notifier.State(
    cooldowns={
      "a": {"suppressed": 3, "lane": "attention"},
      "b": {"suppressed": 2, "lane": "infra"},
    },
    suppressed_total=5,
  )
  monkeypatch.setattr(dg.notifier, "load_state", lambda *_a, **_k: state)
  text = "\n".join(dg.suppressed_section().lines)
  assert "**5** messages held back" in text
  assert "nas-attention ×3" in text
  assert "nas-infra ×2" in text


def test_zero_suppressed_says_the_windows_may_be_pointless(monkeypatch):
  monkeypatch.setattr(dg.notifier, "load_state", lambda *_a, **_k: dg.notifier.State())
  assert "too short to matter" in "\n".join(dg.suppressed_section().lines)


# --- main ----------------------------------------------------------------


def test_no_docker_is_fatal(monkeypatch):
  _fake_run(monkeypatch, {})
  assert dg.main(["--dry-run"]) == 2


def test_a_clean_collection_publishes_once_and_exits_zero(monkeypatch, capsys):
  _fake_run(monkeypatch, {"docker version": (0, "27.0\n")})
  monkeypatch.setattr(dg, "collect", lambda: [dg.Section("All", ["fine"])])
  published: list[tuple] = []

  class _R:
    def __bool__(self):
      return True

  monkeypatch.setattr(
    dg.notifier, "notify",
    lambda lane, title, body, **kw: (published.append((lane, title, body, kw)), _R())[1],
  )
  assert dg.main([]) == 0
  assert len(published) == 1
  lane, title, body, kw = published[0]
  assert lane is dg.notifier.Lane.INFRA
  assert "NAS digest" in title
  assert kw["markdown"] is True
  assert "**All**" in body
  # The digest is one message a day by construction; a cooldown on it could
  # only ever suppress the whole thing.
  assert "dedup_key" not in kw
  capsys.readouterr()


def test_a_collection_gap_still_publishes_but_exits_one(monkeypatch):
  _fake_run(monkeypatch, {"docker version": (0, "27.0\n")})
  monkeypatch.setattr(dg, "collect", lambda: [dg.Section("Imports", ["unreadable"], failed=True)])
  published: list = []

  class _R:
    def __bool__(self):
      return True

  monkeypatch.setattr(
    dg.notifier, "notify",
    lambda *a, **k: (published.append(a), _R())[1],
  )
  assert dg.main([]) == 1, "a gap must be reported as partial, not success"
  assert len(published) == 1, "and it must still send — a gap is not a reason to go silent"


def test_dry_run_publishes_nothing(monkeypatch):
  _fake_run(monkeypatch, {"docker version": (0, "27.0\n")})
  monkeypatch.setattr(dg, "collect", lambda: [dg.Section("All", ["fine"])])
  monkeypatch.setattr(
    dg.notifier, "notify",
    lambda *a, **k: (_ for _ in ()).throw(AssertionError("--dry-run must not publish")),
  )
  assert dg.main(["--dry-run"]) == 0


def test_no_collector_raises_on_a_completely_empty_host(monkeypatch, tmp_path):
  """A digest that crashes is a digest that stops arriving, and nothing reports
  the absence of a report."""
  _fake_run(monkeypatch, {})
  monkeypatch.setattr(dg, "CRON_STATE_DIR", tmp_path / "nope")
  monkeypatch.setattr(dg, "_get_json", lambda *_a, **_k: None)
  for env in ("API_KEY_SONARR", "API_KEY_RADARR", "API_KEY_LIDARR",
              "API_KEY_SLSKD", "CONFIG_DIRECTORY"):
    monkeypatch.delenv(env, raising=False)
  sections = dg.collect()
  assert dg.render(sections)
  assert any(s.failed for s in sections)
