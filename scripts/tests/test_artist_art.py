"""Tests for scripts/artist_art.py — pure logic, no network.

The gate that matters here is pick_candidate's album cross-check. An artist
folder called "33" or "Beware" matches something on Deezer no matter what, and
a wrong artist image looks correct forever, so "name matched but no shared
album" has to stay a rejection rather than a best guess.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  spec = importlib.util.spec_from_file_location("artist_art", scripts_dir / "artist_art.py")
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


aa = _load_module()


def _artist(tmp_path: Path, name: str, albums: tuple[str, ...] = ()):
  path = tmp_path / name
  path.mkdir(parents=True, exist_ok=True)
  return aa.Artist(path=path, name=name, albums=albums)


# --- normalisation -----------------------------------------------------------


def test_normalise_strips_year_prefix_brackets_and_accents():
  assert aa.normalise("2004 - A Sermon In The Name Of Death") == "a sermon in the name of death"
  assert aa.normalise("Hvis lyset tar oss (Remastered)") == "hvis lyset tar oss"
  assert aa.normalise("Abwärts") == "abwarts"
  assert aa.normalise("De Mysteriis Dom. Sathanas") == "de mysteriis dom sathanas"


def test_normalise_makes_the_two_spellings_of_one_album_equal():
  assert aa.normalise("1994 - Transilvanian Hunger") == aa.normalise("Transilvanian Hunger")


def test_folder_to_name_undoes_the_slash_substitution():
  assert aa.folder_to_name("AC+DC") == "AC/DC"
  # A plus that is really a plus, and a name with a real slash, are left alone.
  assert aa.folder_to_name("Godspeed You! Black Emperor") == "Godspeed You! Black Emperor"
  assert aa.folder_to_name("AC/DC") == "AC/DC"


# --- placeholder detection ---------------------------------------------------


def test_placeholder_pictures_are_rejected():
  assert aa.is_placeholder("")
  assert aa.is_placeholder("https://e-cdns-images.dzcdn.net/images/artist//1000x1000-000000-80-0-0.jpg")
  assert not aa.is_placeholder("https://e-cdns-images.dzcdn.net/images/artist/abc123/1000x1000.jpg")


# --- candidate selection -----------------------------------------------------

GOOD_PIC = "https://cdn/images/artist/abc/1000x1000.jpg"


def test_exact_name_plus_shared_album_is_accepted(tmp_path: Path):
  artist = _artist(tmp_path, "Abigor", ("1994 - Verwüstung", "1995 - Nachthymnen"))
  cands = [{"id": 1, "name": "Abigor", "picture_xl": GOOD_PIC}]
  cand, reason = aa.pick_candidate(
    artist, cands, {1: ["Verwüstung", "Opus IV"]}, verify=True
  )
  assert cand is cands[0]
  assert "1 shared album" in reason


def test_name_match_without_a_shared_album_is_rejected(tmp_path: Path):
  """The '33' case: something always matches, and it is usually the wrong band."""
  artist = _artist(tmp_path, "33", ("2011 - A Bright Cold Day",))
  cands = [{"id": 9, "name": "33", "picture_xl": GOOD_PIC}]
  cand, reason = aa.pick_candidate(artist, cands, {9: ["Greatest Hits"]}, verify=True)
  assert cand is None
  assert reason == "name matched but no shared album"


def test_the_candidate_with_the_most_overlap_wins(tmp_path: Path):
  artist = _artist(tmp_path, "Acid", ("1983 - Acid", "1984 - Maniac"))
  cands = [
    {"id": 1, "name": "Acid", "picture_xl": GOOD_PIC},
    {"id": 2, "name": "Acid", "picture_xl": GOOD_PIC},
  ]
  cand, _ = aa.pick_candidate(artist, cands, {1: ["Acid"], 2: ["Acid", "Maniac"]}, verify=True)
  assert cand["id"] == 2


def test_a_near_miss_on_the_name_is_not_a_match(tmp_path: Path):
  artist = _artist(tmp_path, "Abbath", ("2016 - Abbath",))
  cands = [{"id": 3, "name": "Abbath Doom Occulta", "picture_xl": GOOD_PIC}]
  cand, reason = aa.pick_candidate(artist, cands, {3: ["Abbath"]}, verify=True)
  assert cand is None
  assert reason == "no exact name match"


def test_exact_name_but_no_photo_is_reported_separately(tmp_path: Path):
  artist = _artist(tmp_path, "Abhomine", ("2016 - Larvae Offal Swine",))
  cands = [{"id": 4, "name": "Abhomine", "picture_xl": "https://cdn/images/artist//x.jpg"}]
  cand, reason = aa.pick_candidate(artist, cands, {4: ["Larvae Offal Swine"]}, verify=True)
  assert cand is None
  assert reason == "matched, but Deezer has no photo"


def test_verification_off_accepts_the_first_exact_name(tmp_path: Path):
  artist = _artist(tmp_path, "33", ("2011 - A Bright Cold Day",))
  cands = [{"id": 9, "name": "33", "picture_xl": GOOD_PIC}]
  cand, reason = aa.pick_candidate(artist, cands, {}, verify=False)
  assert cand is cands[0]
  assert "verification off" in reason


def test_an_artist_with_no_album_folders_falls_back_to_the_name(tmp_path: Path):
  artist = _artist(tmp_path, "Sunn O)))", ())
  cands = [{"id": 5, "name": "Sunn O)))", "picture_xl": GOOD_PIC}]
  cand, reason = aa.pick_candidate(artist, cands, {}, verify=True)
  assert cand is cands[0]
  assert "no album folders" in reason


# --- target selection --------------------------------------------------------


def test_targets_skip_artists_that_already_have_a_cover(tmp_path: Path):
  have = _artist(tmp_path, "Has")
  (have.path / "folder.jpg").write_bytes(b"x")
  want = _artist(tmp_path, "Wants")
  assert aa.select_targets([have, want], {}, 1000.0, 45.0, 10) == [want]


def test_targets_respect_the_cooldown(tmp_path: Path):
  recent = _artist(tmp_path, "Recent")
  old = _artist(tmp_path, "Old")
  now = 100 * 86400.0
  attempts = {str(recent.path): now - 86400.0, str(old.path): now - 90 * 86400.0}
  assert aa.select_targets([recent, old], attempts, now, 45.0, 10) == [old]


def test_never_tried_sorts_before_long_ago(tmp_path: Path):
  fresh = _artist(tmp_path, "NeverTried")
  stale = _artist(tmp_path, "TriedLongAgo")
  now = 100 * 86400.0
  targets = aa.select_targets([stale, fresh], {str(stale.path): 1.0}, now, 45.0, 10)
  assert targets == [fresh, stale]


def test_limit_bounds_the_run(tmp_path: Path):
  artists = [_artist(tmp_path, f"A{i}") for i in range(10)]
  assert len(aa.select_targets(artists, {}, 1000.0, 45.0, 3)) == 3


# --- discovery ---------------------------------------------------------------


def test_discover_reads_album_folders_and_ignores_empty_dirs(tmp_path: Path):
  (tmp_path / "Darkthrone" / "1994 - Transilvanian Hunger").mkdir(parents=True)
  (tmp_path / "Darkthrone" / "1994 - Transilvanian Hunger" / "01.flac").write_bytes(b"x")
  (tmp_path / "Darkthrone" / "scans").mkdir()
  (tmp_path / "NotAnArtist").mkdir()
  found = {a.name: a for a in aa.discover_artists(tmp_path)}
  assert set(found) == {"Darkthrone"}
  assert found["Darkthrone"].albums == ("1994 - Transilvanian Hunger",)


def test_discover_treats_loose_audio_as_a_single_album_artist(tmp_path: Path):
  (tmp_path / "Loose").mkdir()
  (tmp_path / "Loose" / "track.mp3").write_bytes(b"x")
  assert [a.name for a in aa.discover_artists(tmp_path)] == ["Loose"]
