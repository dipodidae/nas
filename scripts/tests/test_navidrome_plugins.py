import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name, filename):
  spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


check = _load("check_navidrome_plugins", "check-navidrome-plugins.py")
setup = _load("audiomuse_setup", "audiomuse_setup.py")

PIN = "a" * 64


def _row(**kw):
  return {"sha256": PIN, "enabled": True, "last_error": "", **kw}


def test_lock_parses_the_tracked_file():
  pins = check.read_lock()
  assert set(pins) == {"nd-lyrics", "coverartarchive", "audiomuseai"}
  assert all(len(sha) == 64 for sha in pins.values())


def test_healthy_set_has_no_drift():
  assert check.plugin_drift({"x": PIN}, {"x": _row()}) == []


def test_disabled_plugin_is_drift():
  # What a restored navidrome.db looks like: installed, silently off.
  assert check.plugin_drift({"x": PIN}, {"x": _row(enabled=False)}) == ["x: DISABLED"]


def test_missing_wrong_sha_and_error_are_all_reported():
  drift = check.plugin_drift(
    {"a": PIN, "b": PIN, "c": PIN},
    {"b": _row(sha256="b" * 64), "c": _row(last_error="boom")},
  )
  assert drift[0] == "a: not installed"
  assert drift[1].startswith("b: sha256")
  assert drift[2] == "c: last_error='boom'"


def test_overlap_is_share_of_navidromes_answer():
  assert check.overlap(["1", "2", "3", "4"], ["1", "2", "9"]) == 0.5
  # A Last.fm fallback is a FULL list with nothing in common.
  assert check.overlap(["7", "8"], ["1", "2"]) == 0.0
  assert check.overlap([], ["1"]) == 0.0


ENV = {
  "NAVIDROME_AUDIOMUSE_USER": "audiomuse", "NAVIDROME_AUDIOMUSE_PASSWORD": "pw",
  "AUDIOMUSE_ADMIN_USER": "tom", "AUDIOMUSE_ADMIN_PASSWORD": "apw",
  "AUDIOMUSE_API_TOKEN": "tok",
}


def test_config_sends_admin_only_when_none_exists():
  first = setup.build_config(ENV, has_admin=False)
  assert first["AUDIOMUSE_USER"] == "tom"
  again = setup.build_config(ENV, has_admin=True)
  assert "AUDIOMUSE_USER" not in again and "AUDIOMUSE_PASSWORD" not in again


def test_config_points_at_navidrome_over_nas_network():
  cfg = setup.build_config(ENV, has_admin=True)
  assert cfg["NAVIDROME_URL"] == "http://navidrome:4533"
  assert cfg["MEDIASERVER_TYPE"] == "navidrome"


def test_lrclib_slot_is_a_bare_url():
  # `{artist_param}` in the template raised KeyError on every track (ADR-0053).
  url = setup.TUNING["LYRICS_API_1_URL_TEMPLATE"]
  assert "{" not in url and url.startswith("https://lrclib.net/")
