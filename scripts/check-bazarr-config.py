#!/usr/bin/env python3
"""Assert Bazarr's live config is still the accuracy-first setup of 2026-09-25.

Why this exists
---------------
Every one of these settings lives in ``.docker-config/bazarr/`` (``config.yaml`` and
``bazarr.db``), which is gitignored, so ``make check`` cannot see them, and a config
restore or a UI click can undo them silently. Each one was measured wrong on
Agatha Christie's Poirot before it was changed:

* **providers** -- the two with real release/hash matching (OpenSubtitles.com, and
  Addic7ed via Gestdown) were off. OpenSubtitles had been disabled on 2026-09-02 for
  ``Login failed``; Jellyfin's plugin held a working login the whole time. What was
  left, mostly subf2m (old Subscene uploads without release info), handed 49 of 70
  Poirot episodes subtitles timed for the 25 fps PAL DVDs;
* **whisperai** was an auto-download provider, and its transcripts score 61%, so a
  minimum of 60 let 69 of them in. It is left OFF: generated subtitles are not wanted;
* **sync** had ``no_fix_framerate: true``, so ffsubsync could shift a subtitle but
  never stretch it. A 25-vs-24 fps subtitle needs a 0.960 / 1.036 stretch, which is
  exactly what it measured and was not allowed to apply;
* **minimum scores** follow TRaSH (90 series / 80 movies), sync thresholds 96 / 86;
* **profile** -- every series and movie is on "NL + EN", which is also the default
  for new ones. The retired 9-language profile had produced one Dutch subtitle.

The content check -- does the subtitle match the audio at all -- is not a setting:
see ``scripts/subtitle_audit.py``.

Exit: 0 ok / 1 drift / 2 Bazarr unreachable.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

BAZARR_URL = os.environ.get("BAZARR_URL", "http://localhost:6767")
BAZARR_CONFIG = Path(os.environ.get("BAZARR_CONFIG", ".docker-config/bazarr/config/config.yaml"))

WANT_PROVIDERS = {"opensubtitlescom", "subsource", "gestdown", "subdl", "embeddedsubtitles"}
NEVER_PROVIDERS = {"whisperai", "subf2m", "yifysubtitles", "tvsubtitles"}
WANT_MODS = {"common", "OCR_fixes", "remove_HI", "fix_uppercase"}
PROFILE_NAME = "NL + EN"
PROFILE_LANGS = ["nl", "en"]

OK, DRIFT, UNREACHABLE = 0, 1, 2


def problems(
  settings: dict, profiles: list[dict], series: list[dict], movies: list[dict]
) -> list[str]:
  """Every way the live config differs from the pinned one. Pure; see tests."""
  out = []
  g, s = settings["general"], settings["subsync"]
  enabled = set(g.get("enabled_providers") or [])
  if missing := WANT_PROVIDERS - enabled:
    out.append(f"providers missing: {sorted(missing)}")
  if bad := NEVER_PROVIDERS & enabled:
    out.append(f"providers that must stay off are on: {sorted(bad)}")
  if g.get("minimum_score") != 90 or g.get("minimum_score_movie") != 80:
    out.append(
      f"minimum scores {g.get('minimum_score')}/{g.get('minimum_score_movie')}, want 90/80"
    )
  if not s.get("use_subsync") or s.get("no_fix_framerate") is not False:
    out.append("sync must be on WITH framerate correction (no_fix_framerate false)")
  if (s.get("subsync_threshold"), s.get("subsync_movie_threshold")) != (96, 86):
    out.append(
      f"sync thresholds {s.get('subsync_threshold')}/{s.get('subsync_movie_threshold')}, want 96/86"
    )
  if missing := WANT_MODS - set(g.get("subzero_mods") or []):
    out.append(f"subtitle mods off: {sorted(missing)}")
  if not g.get("parse_embedded_audio_track"):
    out.append("deep audio-track analysis is off")
  if (settings.get("opensubtitlescom") or {}).get("use_hash") is not True:
    out.append("OpenSubtitles hash matching is off")
  prof = next((p for p in profiles if p["name"] == PROFILE_NAME), None)
  if not prof:
    return [*out, f"language profile {PROFILE_NAME!r} is gone"]
  if [i["language"] for i in prof["items"]] != PROFILE_LANGS or prof.get("cutoff") is not None:
    out.append(f"{PROFILE_NAME!r} should be {PROFILE_LANGS} with no cutoff")
  pid = prof["profileId"]
  if (g.get("serie_default_profile"), g.get("movie_default_profile")) != (pid, pid):
    out.append(f"default profile for new series/movies is not {PROFILE_NAME!r}")
  for kind, items, key in (("series", series, "title"), ("movies", movies, "title")):
    off = [i[key] for i in items if i.get("profileId") != pid]
    if off:
      out.append(f"{len(off)} {kind} not on {PROFILE_NAME!r}: {off[:3]}")
  return out


def _get(path: str, key: str) -> object:
  req = urllib.request.Request(BAZARR_URL + path, headers={"X-API-KEY": key})
  with urllib.request.urlopen(req, timeout=60) as resp:
    return json.load(resp)


def main() -> int:
  try:
    import yaml  # noqa: PLC0415

    key = yaml.safe_load(BAZARR_CONFIG.read_text())["auth"]["apikey"]
    settings = _get("/api/system/settings", key)
    profiles = _get("/api/system/languages/profiles", key)
    series = _get("/api/series", key)["data"]
    movies = _get("/api/movies", key)["data"]
  except (OSError, KeyError, ValueError, urllib.error.URLError) as exc:
    print(f"bazarr unreachable: {exc}", file=sys.stderr)
    return UNREACHABLE
  found = problems(settings, profiles, series, movies)
  for p in found:
    print(f"DRIFT: {p}")
  if not found:
    print(f"bazarr config ok: {len(series)} series + {len(movies)} movies on {PROFILE_NAME!r}")
  return DRIFT if found else OK


if __name__ == "__main__":
  sys.exit(main())
