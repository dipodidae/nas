"""Tests for jellyfin_nfo_dates.py pure functions.

Covers:
  - album_folder: normal, multi-disc, non-prefixed path
  - iso_date: year-only, full, month-only
  - build_album_nfo: create-from-scratch, merge-preserves-non-date, drops-musicbrainz,
                     overwrites-wrong-year, handles-bad-xml
  - nfo_is_current: true/false combinations
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

# ---------------------------------------------------------------------------
# Module loader (avoids import side-effects / dotenv at collection time)
# ---------------------------------------------------------------------------


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "jellyfin_nfo_dates.py"
    spec = importlib.util.spec_from_file_location("jellyfin_nfo_dates", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


nfo = _load_module()

# ---------------------------------------------------------------------------
# album_folder
# ---------------------------------------------------------------------------

_HOST_PFX = "/mnt/drive/music"
_CTR_PFX = "/music"


def test_album_folder_normal():
    """Regular Artist/Album/track layout — returns album dir."""
    result = nfo.album_folder(
        "/music/Burzum/Filosofem/01 - Dunkelheit.flac",
        _CTR_PFX,
        _HOST_PFX,
    )
    assert result == Path("/mnt/drive/music/Burzum/Filosofem")


def test_album_folder_multi_disc_disc_n():
    """Disc sub-folder (Disc 1) collapses to album root."""
    result = nfo.album_folder(
        "/music/Pink Floyd/The Wall/Disc 1/01 - In The Flesh.flac",
        _CTR_PFX,
        _HOST_PFX,
    )
    assert result == Path("/mnt/drive/music/Pink Floyd/The Wall")


def test_album_folder_multi_disc_cd_n():
    """Disc sub-folder (CD2) collapses to album root."""
    result = nfo.album_folder(
        "/music/Artist/Album/CD2/05 - Track.mp3",
        _CTR_PFX,
        _HOST_PFX,
    )
    assert result == Path("/mnt/drive/music/Artist/Album")


def test_album_folder_multi_disc_case_insensitive():
    """disc sub-folder matching is case-insensitive."""
    result = nfo.album_folder(
        "/music/Artist/Album/disc 01/01.flac",
        _CTR_PFX,
        _HOST_PFX,
    )
    assert result == Path("/mnt/drive/music/Artist/Album")


def test_album_folder_non_prefixed_path():
    """Track path outside expected prefix is used as-is (best-effort)."""
    result = nfo.album_folder(
        "/other/Artist/Album/01.flac",
        _CTR_PFX,
        _HOST_PFX,
    )
    assert result == Path("/other/Artist/Album")


def test_album_folder_no_disc_subfolder_named_similarly():
    """A folder named 'Disco Inferno' is NOT collapsed (no trailing digit)."""
    result = nfo.album_folder(
        "/music/Artist/Disco Inferno/01.flac",
        _CTR_PFX,
        _HOST_PFX,
    )
    assert result == Path("/mnt/drive/music/Artist/Disco Inferno")


# ---------------------------------------------------------------------------
# iso_date
# ---------------------------------------------------------------------------


def test_iso_date_year_only():
    assert nfo.iso_date(1990, None, None) == "1990-01-01"


def test_iso_date_full():
    assert nfo.iso_date(1983, 5, 25) == "1983-05-25"


def test_iso_date_month_no_day():
    assert nfo.iso_date(2001, 3, None) == "2001-03-01"


def test_iso_date_zero_padding():
    assert nfo.iso_date(1977, 1, 7) == "1977-01-07"


# ---------------------------------------------------------------------------
# build_album_nfo
# ---------------------------------------------------------------------------


def _parse(xml_str: str) -> ET.Element:
    """Strip the XML declaration and parse the remaining XML."""
    lines = xml_str.split("\n", 1)
    body = lines[1] if len(lines) > 1 else lines[0]
    return ET.fromstring(body)


def test_build_album_nfo_create_from_scratch():
    xml = nfo.build_album_nfo(None, "Filosofem", 1996, 1, 25)
    root = _parse(xml)
    assert root.tag == "album"
    assert root.find("title").text == "Filosofem"
    assert root.find("year").text == "1996"
    assert root.find("premiered").text == "1996-01-25"
    assert root.find("releasedate").text == "1996-01-25"
    assert root.find("lockdata").text == "true"


def test_build_album_nfo_has_xml_declaration():
    xml = nfo.build_album_nfo(None, "Test", 2000, None, None)
    assert xml.startswith('<?xml version="1.0" encoding="utf-8" standalone="yes"?>')


def test_build_album_nfo_merge_preserves_non_date_elements():
    existing = "<album><title>Old Title</title><label>Sub Pop</label><year>1991</year></album>"
    xml = nfo.build_album_nfo(existing, "New Title", 1991, 9, 24)
    root = _parse(xml)
    # <label> survives the merge.
    assert root.find("label").text == "Sub Pop"
    # year is overwritten to the resolved value.
    assert root.find("year").text == "1991"
    assert root.find("premiered").text == "1991-09-24"


def test_build_album_nfo_preserves_existing_title():
    existing = "<album><title>Existing Title</title><label>Factory</label></album>"
    xml = nfo.build_album_nfo(existing, "Ignored Title", 1979, None, None)
    root = _parse(xml)
    # The pre-existing title is preserved (not duplicated or replaced).
    titles = root.findall("title")
    assert len(titles) == 1
    assert titles[0].text == "Existing Title"


def test_build_album_nfo_adds_title_when_missing():
    existing = "<album><label>Factory</label></album>"
    xml = nfo.build_album_nfo(existing, "Joy Division", 1979, None, None)
    root = _parse(xml)
    assert root.find("title").text == "Joy Division"


def test_build_album_nfo_drops_musicbrainz_elements():
    existing = (
        "<album>"
        "<title>OK Computer</title>"
        "<musicbrainzalbumid>abc-123</musicbrainzalbumid>"
        "<MusicBrainzReleaseGroupId>xyz</MusicBrainzReleaseGroupId>"
        "<label>Parlophone</label>"
        "</album>"
    )
    xml = nfo.build_album_nfo(existing, "OK Computer", 1997, 5, 21)
    root = _parse(xml)
    # All musicbrainz* elements should be gone.
    tags = [el.tag.lower() for el in root]
    assert not any(t.startswith("musicbrainz") for t in tags)
    # Unrelated elements survive.
    assert root.find("label").text == "Parlophone"


def test_build_album_nfo_overwrites_wrong_year():
    existing = "<album><title>Nevermind</title><year>2011</year><lockdata>true</lockdata></album>"
    xml = nfo.build_album_nfo(existing, "Nevermind", 1991, 9, 24)
    root = _parse(xml)
    assert root.find("year").text == "1991"
    assert root.find("premiered").text == "1991-09-24"
    # lockdata is still true.
    assert root.find("lockdata").text == "true"


def test_build_album_nfo_handles_bad_xml_gracefully():
    """Corrupt existing XML should not raise — fall back to fresh root."""
    xml = nfo.build_album_nfo("<<< not xml >>>", "My Album", 2005, None, None)
    root = _parse(xml)
    assert root.tag == "album"
    assert root.find("year").text == "2005"


def test_build_album_nfo_no_duplicate_date_elements():
    """Even if existing XML has date fields, the result must have exactly one of each."""
    existing = (
        "<album>"
        "<title>X</title>"
        "<year>1999</year>"
        "<premiered>1999-01-01</premiered>"
        "<releasedate>1999-01-01</releasedate>"
        "<lockdata>false</lockdata>"
        "</album>"
    )
    xml = nfo.build_album_nfo(existing, "X", 1997, 6, 16)
    root = _parse(xml)
    for tag in ("year", "premiered", "releasedate", "lockdata"):
        assert len(root.findall(tag)) == 1, f"Expected exactly one <{tag}>"


# ---------------------------------------------------------------------------
# nfo_is_current
# ---------------------------------------------------------------------------


def test_nfo_is_current_true_when_year_matches_and_locked():
    xml = "<album><year>1996</year><lockdata>true</lockdata></album>"
    assert nfo.nfo_is_current(xml, 1996) is True


def test_nfo_is_current_false_when_year_wrong():
    xml = "<album><year>2011</year><lockdata>true</lockdata></album>"
    assert nfo.nfo_is_current(xml, 1991) is False


def test_nfo_is_current_false_when_lockdata_missing():
    xml = "<album><year>1991</year></album>"
    assert nfo.nfo_is_current(xml, 1991) is False


def test_nfo_is_current_false_when_lockdata_false():
    xml = "<album><year>1991</year><lockdata>false</lockdata></album>"
    assert nfo.nfo_is_current(xml, 1991) is False


def test_nfo_is_current_false_when_no_year():
    xml = "<album><lockdata>true</lockdata></album>"
    assert nfo.nfo_is_current(xml, 1991) is False


def test_nfo_is_current_false_when_bad_xml():
    assert nfo.nfo_is_current("<<< bad xml >>>", 1991) is False


def test_nfo_is_current_lockdata_case_insensitive():
    """lockdata value matching is case-insensitive."""
    xml = "<album><year>2000</year><lockdata>True</lockdata></album>"
    assert nfo.nfo_is_current(xml, 2000) is True


# ---------------------------------------------------------------------------
# write_nfo uses tmp_path (filesystem, still pure in isolation)
# ---------------------------------------------------------------------------


def test_write_nfo_creates_file(tmp_path):
    nfo_path = tmp_path / "Artist" / "Album" / "album.nfo"
    content = '<?xml version="1.0"?>\n<album><year>2000</year></album>'
    nfo.write_nfo(nfo_path, content)
    assert nfo_path.exists()
    assert nfo_path.read_text(encoding="utf-8") == content


def test_write_nfo_overwrites_existing(tmp_path):
    nfo_path = tmp_path / "album.nfo"
    nfo_path.write_text("old content", encoding="utf-8")
    nfo.write_nfo(nfo_path, "new content")
    assert nfo_path.read_text(encoding="utf-8") == "new content"
