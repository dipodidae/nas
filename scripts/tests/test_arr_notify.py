"""Tests for scripts/arr_notify.sh — the *arr custom-script media publisher.

Run against the real script with `sh`, so what is tested is what the containers
execute. A fake ntfy is stood up on loopback and the script is pointed at it,
which is the only way to assert the headers it actually sends: a wrong header
name produces HTTP 200 and no effect, and this repo has been bitten by that
exact shape three times (AGENTS.md).

The property that matters most is the last one: **the script must exit 0 no
matter what**. It runs inside the *arr import pipeline, so a non-zero exit is
reported as a failed notification and invites someone to go "fix" a perfectly
good import. A notifier must never be able to affect the thing it observes.
"""

from __future__ import annotations

import http.server
import json
import subprocess
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "arr_notify.sh"


class _Collector(http.server.BaseHTTPRequestHandler):
  received: list[dict] = []

  def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
    length = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(length).decode("utf-8", "replace")
    # http.server decodes header values as latin-1 (the RFC default). ntfy
    # reads X-Title as UTF-8, which is why the emoji arrive correctly in
    # production; re-decode here so the comparison is against what ntfy sees.
    def _utf8(value: str) -> str:
      try:
        return value.encode("latin-1").decode("utf-8")
      except (UnicodeDecodeError, UnicodeEncodeError):
        return value

    type(self).received.append({
      "path": self.path,
      "headers": {k: _utf8(v) for k, v in self.headers.items()},
      "body": body,
    })
    self.send_response(200)
    self.send_header("Content-Length", "0")
    self.end_headers()

  def log_message(self, *_args):
    pass


@pytest.fixture
def fake_ntfy():
  """A loopback ntfy that records what the script sent."""
  _Collector.received = []
  server = http.server.HTTPServer(("127.0.0.1", 0), _Collector)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  yield server, _Collector.received
  server.shutdown()
  server.server_close()


@pytest.fixture
def run_script(fake_ntfy, tmp_path):
  """Run the real script with a token file and a fake ntfy. Returns a callable."""
  server, received = fake_ntfy
  token = tmp_path / "arr-token"
  token.write_text("tk_fake\n")
  logs = tmp_path / "logs"
  logs.mkdir()

  def _run(env: dict[str, str], *, with_token: bool = True, url: str | None = None):
    base = url or f"http://127.0.0.1:{server.server_port}"
    full = {
      "PATH": "/usr/bin:/bin",
      "NTFY_URL": base,
      "NTFY_TOPIC_MEDIA": "nas-media",
      **env,
    }
    # The script's paths are absolute constants, so redirect them with sed into
    # a tmp copy rather than mounting anything. Testing the real file's logic is
    # the point; only the two paths differ.
    text = SCRIPT.read_text()
    text = text.replace("TOKEN_FILE=/run/ntfy-arr-token",
                        f"TOKEN_FILE={token if with_token else tmp_path / 'absent'}")
    text = text.replace("LOG=/config/logs/arr_notify.log", f"LOG={logs / 'arr_notify.log'}")
    text = text.replace("FIRST_EVENT_MARKER=/config/logs/.arr_notify_first_event",
                        f"FIRST_EVENT_MARKER={logs / '.first'}")
    copy = tmp_path / "arr_notify.sh"
    copy.write_text(text)
    copy.chmod(0o755)
    proc = subprocess.run(["sh", str(copy)], env=full, capture_output=True,
                          text=True, timeout=30, check=False)
    return proc, received, logs / "arr_notify.log"

  return _run


# --- the script itself ---------------------------------------------------


def test_the_script_is_posix_sh_clean():
  """LSIO s6 scripts have no bash guarantee: `sh -n` must accept it."""
  proc = subprocess.run(["sh", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
  assert proc.returncode == 0, proc.stderr


def test_the_script_is_executable():
  assert SCRIPT.stat().st_mode & 0o111, "the host copy must be chmod 755 to mount usefully"


def _code_lines() -> list[str]:
  """The script's executable lines only.

  Comments are excluded deliberately: the file DOCUMENTS the bashisms it avoids
  and the lane it publishes to, and a scanner that cannot tell prose from code
  fails on its own documentation.
  """
  return [
    ln for ln in SCRIPT.read_text().splitlines()
    if ln.strip() and not ln.lstrip().startswith("#")
  ]


def test_the_script_contains_no_bashisms():
  code = "\n".join(_code_lines())
  for bashism in ("[[", "declare ", "local ", "${!", "=~", "<<<"):
    assert bashism not in code, f"bashism {bashism!r} in a POSIX sh script"


def test_the_script_holds_no_credential_and_no_topic_literal():
  """The token comes from a 0600 file (ADR-0011); the topic from the env."""
  code = "\n".join(_code_lines())
  assert "tk_" not in code
  assert "/run/ntfy-arr-token" in code
  # `nas-media` appears exactly once in code, as the documented default of the
  # NTFY_TOPIC_MEDIA override — never at the publish site.
  assert code.count("nas-media") == 1, "a topic literal escaped into the code"


# --- message shapes -----------------------------------------------------


def test_a_single_episode_import_renders_the_tv_shape(run_script):
  proc, received, _log = run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "The Expanse",
    "sonarr_episodefile_seasonnumber": "2",
    "sonarr_episodefile_episodenumbers": "7",
    "sonarr_episodefile_episodetitles": "The Seventh Man",
    "sonarr_episodefile_quality": "WEBDL-1080p",
    "sonarr_episodefile_releasegroup": "NTb",
  })
  assert proc.returncode == 0
  assert len(received) == 1
  msg = received[0]
  assert msg["path"] == "/nas-media"
  assert msg["headers"]["X-Title"] == "📺 The Expanse S02E07"
  assert msg["headers"]["X-Priority"] == "3"
  assert msg["headers"]["X-Tags"] == "tv"
  assert msg["headers"]["X-Markdown"] == "true"
  assert msg["headers"]["Authorization"] == "Bearer tk_fake"
  assert "The Seventh Man" in msg["body"]
  assert "WEBDL-1080p" in msg["body"]
  assert "NTb" in msg["body"]


def test_a_multi_episode_import_is_ONE_message_with_a_range(run_script):
  """A season pack must not become ten pushes — that is the noise being fixed."""
  proc, received, _log = run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "Fargo",
    "sonarr_episodefile_seasonnumber": "4",
    "sonarr_episodefile_episodenumbers": "1,2,3,4,5,6",
    "sonarr_episodefile_quality": "Bluray-2160p",
  })
  assert proc.returncode == 0
  assert len(received) == 1, "one import event is one message"
  assert received[0]["headers"]["X-Title"] == "📺 Fargo S04E01-E06"
  assert "6 episodes" in received[0]["body"]


def test_an_upgrade_says_so_and_carries_the_old_quality(run_script):
  _proc, received, _log = run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "Fargo",
    "sonarr_episodefile_seasonnumber": "4",
    "sonarr_episodefile_episodenumbers": "1",
    "sonarr_episodefile_quality": "Bluray-2160p",
    "sonarr_isupgrade": "True",
    "sonarr_deletedfilequalities": "WEBDL-1080p",
  })
  assert "upgrade WEBDL-1080p → Bluray-2160p" in received[0]["body"]


def test_an_upgrade_with_no_old_quality_degrades_rather_than_inventing_an_arrow(run_script):
  _proc, received, _log = run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "Fargo",
    "sonarr_episodefile_seasonnumber": "4",
    "sonarr_episodefile_episodenumbers": "1",
    "sonarr_episodefile_quality": "Bluray-2160p",
    "sonarr_isupgrade": "True",
  })
  body = received[0]["body"]
  assert "upgrade" in body
  assert "→" not in body


def test_a_movie_import_renders_the_movie_shape(run_script):
  _proc, received, _log = run_script({
    "radarr_eventtype": "Download",
    "radarr_movie_title": "Dune: Part Two",
    "radarr_movie_year": "2024",
    "radarr_moviefile_quality": "Bluray-2160p",
    "radarr_moviefile_releasegroup": "FLUX",
  })
  assert received[0]["headers"]["X-Title"] == "🎬 Dune: Part Two (2024)"
  assert received[0]["headers"]["X-Tags"] == "film_projector"


def test_a_movie_with_no_year_still_renders(run_script):
  _proc, received, _log = run_script({
    "radarr_eventtype": "Download",
    "radarr_movie_title": "Untitled",
    "radarr_moviefile_quality": "WEBDL-1080p",
  })
  assert received[0]["headers"]["X-Title"] == "🎬 Untitled"


def test_an_album_import_renders_the_music_shape_and_counts_pipe_separated_tracks(run_script):
  """Lidarr separates added track paths with `|`, which no shell splits on."""
  _proc, received, _log = run_script({
    "lidarr_eventtype": "ReleaseImport",
    "lidarr_artist_name": "Boards of Canada",
    "lidarr_album_title": "Geogaddi",
    "lidarr_release_quality": "FLAC",
    "lidarr_addedtrackpaths": "/a/1.flac|/a/2.flac|/a/3.flac|/a/4.flac",
  })
  assert received[0]["headers"]["X-Title"] == "🎵 Boards of Canada — Geogaddi"
  assert received[0]["headers"]["X-Tags"] == "musical_note"
  assert "4 tracks" in received[0]["body"]


def test_a_missing_field_never_renders_a_dangling_separator(run_script):
  """No release group, no quality, no size: the body must not be ' ·  · '."""
  _proc, received, _log = run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "Sparse",
    "sonarr_episodefile_seasonnumber": "1",
    "sonarr_episodefile_episodenumbers": "1",
  })
  body = received[0]["body"]
  assert not body.startswith("·")
  assert not body.endswith("·")
  assert " ·  · " not in body


def test_the_click_url_is_sent_when_configured_and_omitted_when_not(run_script):
  _proc, received, _log = run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "X",
    "sonarr_episodefile_seasonnumber": "1",
    "sonarr_episodefile_episodenumbers": "1",
    "NTFY_MEDIA_CLICK": "https://jellyfin.example.invalid",
  })
  assert received[0]["headers"]["X-Click"] == "https://jellyfin.example.invalid"
  received.clear()
  run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "X",
    "sonarr_episodefile_seasonnumber": "1",
    "sonarr_episodefile_episodenumbers": "1",
  })
  assert "X-Click" not in received[0]["headers"]


# --- the exclusions ------------------------------------------------------


def test_a_test_event_publishes_nothing_and_exits_zero(run_script):
  """The *arr Test button must report success without producing a message."""
  proc, received, log = run_script({"sonarr_eventtype": "Test"})
  assert proc.returncode == 0
  assert received == []
  assert "TEST event" in log.read_text()


@pytest.mark.parametrize("event", ["Grab", "Rename", "TrackRetag",
                                   "ApplicationUpdate", "HealthIssue",
                                   "HealthRestored", "SeriesAdd", "MovieAdded",
                                   "SeriesDelete", "SomethingNew"])
def test_every_excluded_event_type_is_a_silent_no_op(run_script, event):
  """A connector toggled on in the UI must not start producing messages."""
  proc, received, _log = run_script({
    "sonarr_eventtype": event,
    "sonarr_series_title": "Should Not Send",
  })
  assert proc.returncode == 0
  assert received == [], f"{event} produced a message"


def test_no_eventtype_at_all_is_a_silent_no_op(run_script):
  proc, received, log = run_script({})
  assert proc.returncode == 0
  assert received == []
  assert "no *_eventtype" in log.read_text()


# --- it must never be able to break an import ---------------------------


def test_a_dead_ntfy_still_exits_zero(run_script):
  """This runs in the import pipeline. A non-zero exit invites someone to
  'fix' an import that was fine."""
  proc, received, log = run_script(
    {
      "sonarr_eventtype": "ImportComplete",
      "sonarr_series_title": "Unreachable",
      "sonarr_episodefile_seasonnumber": "1",
      "sonarr_episodefile_episodenumbers": "1",
    },
    url="http://127.0.0.1:9",
  )
  assert proc.returncode == 0
  assert received == []
  assert "failed" in log.read_text().lower()


def test_a_missing_token_file_exits_zero_and_says_why(run_script):
  proc, received, log = run_script(
    {
      "sonarr_eventtype": "ImportComplete",
      "sonarr_series_title": "No Token",
      "sonarr_episodefile_seasonnumber": "1",
      "sonarr_episodefile_episodenumbers": "1",
    },
    with_token=False,
  )
  assert proc.returncode == 0
  assert received == []
  assert "no token" in log.read_text()


def test_an_unwritable_log_does_not_break_the_publish(run_script, tmp_path):
  """A notifier that dies because it cannot log is worse than one that logs
  nothing."""
  text = SCRIPT.read_text().replace(
    "LOG=/config/logs/arr_notify.log", "LOG=/proc/definitely/not/writable")
  copy = tmp_path / "nolog.sh"
  copy.write_text(text)
  copy.chmod(0o755)
  proc = subprocess.run(
    ["sh", str(copy)],
    env={"PATH": "/usr/bin:/bin", "sonarr_eventtype": "Grab"},
    capture_output=True, text=True, timeout=30, check=False,
  )
  assert proc.returncode == 0


def test_the_first_real_event_dumps_the_environment_exactly_once(run_script):
  """The *arr Test button carries only `<app>_eventtype`, so this dump is the
  only thing that can confirm the payload variable names."""
  env = {
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "Dump Once",
    "sonarr_episodefile_seasonnumber": "1",
    "sonarr_episodefile_episodenumbers": "1",
  }
  _proc, _received, log = run_script(env)
  first = log.read_text()
  assert "FIRST real event" in first
  assert "sonarr_series_title=Dump Once" in first
  _proc, _received, log = run_script(env)
  assert log.read_text().count("FIRST real event") == 1, "the dump must not repeat"


def test_a_title_with_shell_metacharacters_is_not_interpreted(run_script):
  """A release name is untrusted text; it must reach ntfy as data."""
  _proc, received, _log = run_script({
    "sonarr_eventtype": "ImportComplete",
    "sonarr_series_title": "Rick & Morty $(id) `id` ;id",
    "sonarr_episodefile_seasonnumber": "1",
    "sonarr_episodefile_episodenumbers": "1",
  })
  title = received[0]["headers"]["X-Title"]
  assert "$(id)" in title
  assert "uid=" not in title, "command substitution was evaluated"
  assert json.dumps(title)  # header survived as text
