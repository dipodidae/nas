"""Tests for scripts/music_library_sweep.py — pure logic, no network, no disk."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "music_library_sweep", scripts_dir / "music_library_sweep.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sweep = _load()


# --- norm(): the multi-disc trap that manufactures ~1500 false positives ---


def test_norm_collapses_disc_subfolders():
    for suffix in ("Disc 01", "CD 2", "Disk_3", "disc  4", "DISC 10"):
        assert sweep.norm(f"/m/A/Album/{suffix}") == "/m/A/Album", suffix


def test_norm_leaves_a_plain_album_alone():
    assert sweep.norm("/m/A/1988 - Blood Fire Death") == "/m/A/1988 - Blood Fire Death"


def test_norm_does_not_eat_an_album_that_merely_mentions_a_disc():
    """'Disc' has to be the whole trailing segment, not a word inside the name."""
    assert sweep.norm("/m/A/The Disc 2 Sessions/Disc 1") == "/m/A/The Disc 2 Sessions"
    assert sweep.norm("/m/A/Compact Disc 1 Anthology") == "/m/A/Compact Disc 1 Anthology"


def test_norm_only_strips_one_level():
    assert sweep.norm("/m/A/Album/Disc 1/Disc 2") == "/m/A/Album/Disc 1"


def test_a_multidisc_album_normalises_to_one_entry():
    raw = {"/m/A/Alb/Disc 01", "/m/A/Alb/Disc 02", "/m/A/Alb/Disc 03"}
    assert {sweep.norm(p) for p in raw} == {"/m/A/Alb"}


# --- the identity that makes a self-contradicting report impossible ---


def test_arithmetic_holds_for_equal_sets():
    s = {"a", "b", "c"}
    assert sweep.arithmetic_holds(s, set(s))


def test_arithmetic_holds_when_both_sides_differ():
    assert sweep.arithmetic_holds({"a", "b"}, {"b", "c"})


def test_arithmetic_holds_for_empty_sets():
    assert sweep.arithmetic_holds(set(), set())


def test_the_audits_impossible_numbers_are_rejected():
    """15,268 disk vs 15,273 jellyfin with 0 missing and 0 ghosts cannot be true.

    Equal-looking sets with unequal sizes is exactly what the identity catches,
    and it is what the 2026-09-04 audit reported.
    """

    class Impossible(set):
        """Reports a size that its contents do not support."""

        def __len__(self):
            return 15268

    disk = Impossible({f"p{i}" for i in range(15273)})
    jf = {f"p{i}" for i in range(15273)}
    assert not disk - jf and not jf - disk  # "0 missing, 0 ghosts"
    assert not sweep.arithmetic_holds(disk, jf)  # ...and still refused


# --- the extension list is load-bearing ---


def test_aiff_is_in_the_extension_list():
    """Five albums here are aiff-only and become fake ghosts without it."""
    assert "aif" in sweep.AUDIO_EXTENSIONS
    assert "aiff" in sweep.AUDIO_EXTENSIONS
