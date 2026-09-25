import copy
import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  script_path = root / "scripts" / "check-bazarr-config.py"
  spec = importlib.util.spec_from_file_location("check_bazarr_config", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


cb = _load_module()

GOOD = {
  "general": {
    "enabled_providers": [
      "opensubtitlescom",
      "subsource",
      "gestdown",
      "subdl",
      "embeddedsubtitles",
    ],
    "minimum_score": 90,
    "minimum_score_movie": 80,
    "subzero_mods": ["common", "OCR_fixes", "remove_HI", "fix_uppercase"],
    "parse_embedded_audio_track": True,
    "serie_default_profile": 3,
    "movie_default_profile": 3,
  },
  "subsync": {
    "use_subsync": True,
    "no_fix_framerate": False,
    "subsync_threshold": 96,
    "subsync_movie_threshold": 86,
  },
  "opensubtitlescom": {"use_hash": True},
}
PROFILES = [
  {
    "profileId": 3,
    "name": "NL + EN",
    "cutoff": None,
    "items": [{"language": "nl"}, {"language": "en"}],
  }
]
SERIES = [{"title": "Poirot", "profileId": 3}]
MOVIES = [{"title": "12 Angry Men", "profileId": 3}]


def _with(path, value):
  s = copy.deepcopy(GOOD)
  s[path[0]][path[1]] = value
  return s


def test_pinned_config_is_clean():
  assert cb.problems(GOOD, PROFILES, SERIES, MOVIES) == []


def test_each_measured_regression_is_caught():
  cases = [
    (("general", "enabled_providers"), ["subf2m", "whisperai", "subdl"]),
    (("general", "minimum_score"), 60),
    (("subsync", "no_fix_framerate"), True),
    (("subsync", "subsync_movie_threshold"), 96),
    (("general", "subzero_mods"), []),
    (("general", "parse_embedded_audio_track"), False),
    (("general", "serie_default_profile"), 2),
    (("opensubtitlescom", "use_hash"), False),
  ]
  for path, value in cases:
    assert cb.problems(_with(path, value), PROFILES, SERIES, MOVIES), path


def test_profile_drift_is_caught():
  assert cb.problems(GOOD, [], SERIES, MOVIES) == ["language profile 'NL + EN' is gone"]
  cutoff = [dict(PROFILES[0], cutoff=1)]
  assert cb.problems(GOOD, cutoff, SERIES, MOVIES)
  assert cb.problems(GOOD, PROFILES, [{"title": "Friends", "profileId": 1}], MOVIES)
