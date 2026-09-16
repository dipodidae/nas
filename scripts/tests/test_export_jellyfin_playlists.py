"""Tests for scripts/export_jellyfin_playlists.py — the pure path/name logic."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "export_jellyfin_playlists.py"
    spec = importlib.util.spec_from_file_location("export_jellyfin_playlists", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


ejp = _load_module()

JELLYFIN_ROOTS = ["/data/movies/music"]


# --- to_relative: the three-namespace problem ---


def test_strips_the_jellyfin_music_prefix():
    got = ejp.to_relative("/data/movies/music/Kreator/1986 - Flag of Hate/02 - x.mp3", JELLYFIN_ROOTS)
    assert got == "Kreator/1986 - Flag of Hate/02 - x.mp3"


def test_path_outside_the_music_library_is_none_not_a_guess():
    # A video dropped into an audio playlist, or a path left by a repath.
    assert ejp.to_relative("/data/movies/movies/Dune/Dune.mkv", JELLYFIN_ROOTS) is None
    assert ejp.to_relative("/music/Kreator/x.mp3", JELLYFIN_ROOTS) is None


def test_longest_root_wins_so_a_broad_root_cannot_swallow_a_specific_one():
    roots = sorted(["/data", "/data/movies/music"], key=len, reverse=True)
    assert ejp.to_relative("/data/movies/music/A/B/c.flac", roots) == "A/B/c.flac"


def test_trailing_slash_on_the_root_is_tolerated():
    assert ejp.to_relative("/data/movies/music/A/c.flac", ["/data/movies/music/"]) == "A/c.flac"


def test_the_root_itself_is_not_a_track():
    assert ejp.to_relative("/data/movies/music", JELLYFIN_ROOTS) is None


# --- sanitize_filename: lossy on purpose, the real name rides in #PLAYLIST: ---


def test_slashes_would_make_a_directory_so_they_go():
    got = ejp.sanitize_filename("Слово пацана. Пыяла/Музыка / Аигел", fallback="x")
    assert "/" not in got
    assert got.startswith("Слово пацана")


def test_unicode_ornaments_are_kept():
    assert ejp.sanitize_filename("✦ Evil Death Metal", fallback="x") == "✦ Evil Death Metal"


def test_control_characters_and_runs_of_space_collapse():
    assert ejp.sanitize_filename("a\tb\n\nc   d", fallback="x") == "a b c d"


def test_a_name_that_sanitises_to_nothing_falls_back():
    assert ejp.sanitize_filename("///", fallback="fallback-id") == "fallback-id"


def test_leading_and_trailing_dots_go_so_the_file_is_not_hidden():
    assert not ejp.sanitize_filename(".hidden.", fallback="x").startswith(".")


# --- unique_filenames: a collision must not silently lose a playlist ---


def _pl(pid, name):
    return ejp.Playlist(jellyfin_id=pid, name=name)


def test_two_names_that_sanitise_alike_get_distinct_files():
    names = ejp.unique_filenames([_pl("id1", "Rock/Metal"), _pl("id2", "Rock:Metal")])
    assert len(set(names.values())) == 2
    assert names["id1"] != names["id2"]


def test_collision_resolution_is_deterministic_across_runs():
    playlists = [_pl("id2", "Rock:Metal"), _pl("id1", "Rock/Metal")]
    first = ejp.unique_filenames(playlists)
    second = ejp.unique_filenames(list(reversed(playlists)))
    assert first == second


def test_case_only_differences_still_collide_because_the_fs_may_not_care():
    names = ejp.unique_filenames([_pl("a", "Bangers"), _pl("b", "BANGERS")])
    assert len(set(n.casefold() for n in names.values())) == 2


def test_every_file_ends_in_m3u8():
    names = ejp.unique_filenames([_pl("a", "x"), _pl("b", "y")])
    assert all(n.endswith(".m3u8") for n in names.values())


# --- render_m3u ---


def _track(rel="A/B/c.mp3", title="c", artist="A", seconds=61):
    return ejp.Track(relative_path=rel, title=title, artist=artist, seconds=seconds)


def test_body_carries_the_real_name_and_the_marker():
    body = ejp.render_m3u(ejp.Playlist("pid", "✦ Real / Name", [_track()]))
    assert body.startswith("#EXTM3U\n")
    assert "#PLAYLIST:✦ Real / Name\n" in body
    assert f"{ejp.MARKER_PREFIX}pid\n" in body


def test_depth_one_prefixes_dotdot_so_it_resolves_from_the_playlists_subdir():
    body = ejp.render_m3u(ejp.Playlist("p", "n", [_track()]), depth=1)
    assert "../A/B/c.mp3" in body


def test_depth_zero_writes_a_bare_relative_path():
    body = ejp.render_m3u(ejp.Playlist("p", "n", [_track()]), depth=0)
    assert "\nA/B/c.mp3\n" in body


def test_extinf_carries_seconds_and_artist_title():
    body = ejp.render_m3u(ejp.Playlist("p", "n", [_track(seconds=61)]))
    assert "#EXTINF:61,A - c" in body


def test_a_track_with_no_artist_degrades_to_the_title_alone():
    body = ejp.render_m3u(ejp.Playlist("p", "n", [_track(artist="")]))
    assert "#EXTINF:61,c" in body


def test_track_order_is_preserved():
    tracks = [_track(rel=f"A/{i}.mp3") for i in range(5)]
    body = ejp.render_m3u(ejp.Playlist("p", "n", tracks))
    positions = [body.index(f"../A/{i}.mp3") for i in range(5)]
    assert positions == sorted(positions)


def test_an_empty_playlist_still_renders_a_valid_header():
    body = ejp.render_m3u(ejp.Playlist("p", "n", []))
    assert body.endswith("\n")
    assert body.count("#EXTINF") == 0


# --- exported_files: --prune must never touch a hand-made playlist ---


def test_only_files_carrying_the_marker_are_claimed(tmp_path):
    (tmp_path / "ours.m3u8").write_text(
        ejp.render_m3u(ejp.Playlist("jf-id", "ours", [_track()])), encoding="utf-8"
    )
    (tmp_path / "handmade.m3u8").write_text("#EXTM3U\n../A/B/c.mp3\n", encoding="utf-8")
    found = ejp.exported_files(tmp_path)
    assert found == {"jf-id": tmp_path / "ours.m3u8"}


def test_a_missing_directory_is_empty_not_an_error(tmp_path):
    assert ejp.exported_files(tmp_path / "nope") == {}
