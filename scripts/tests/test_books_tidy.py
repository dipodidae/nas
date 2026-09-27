"""Tests for the books-library tidy scripts (ADR-0059): sidecars and audiobook merges."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from scripts import audiobook_merge as am, books_sidecars as bs

NS = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}


# --- books_sidecars ------------------------------------------------------------


def _meta(**kw) -> bs.BookMeta:
  base = {"title": "Consider Phlebas", "author": "Iain M. Banks", "author_sort": "Banks, Iain M.",
          "date": "1987-04-23", "series": "Culture", "series_index": "1"}
  return bs.BookMeta(**{**base, **kw})


def test_the_opf_carries_a_full_date_because_the_plugin_tryparses_it():
  """Jellyfin's Bookshelf plugin DateTime.TryParse()s dc:date; a bare '1987'
  fails and every book lost its year. Measured."""
  root = ET.fromstring(bs.build_opf(_meta()))
  assert root.find(".//dc:date", NS).text == "1987-04-23"


def test_the_opf_has_calibre_series_and_an_integer_index():
  root = ET.fromstring(bs.build_opf(_meta()))
  metas = {m.get("name"): m.get("content") for m in root.iter(f"{{{NS['opf']}}}meta")}
  assert metas["calibre:series"] == "Culture"
  assert metas["calibre:series_index"] == "1"
  assert metas["calibre:title_sort"] == "Consider Phlebas"


def test_the_opf_escapes_markup_in_descriptions_and_titles():
  root = ET.fromstring(bs.build_opf(_meta(title="Fish & Chips <3", description="a < b & c")))
  assert root.find(".//dc:title", NS).text == "Fish & Chips <3"
  assert root.find(".//dc:description", NS).text == "a < b & c"


def test_series_parsing_and_title_sort():
  assert bs.parse_series("Culture #1") == ("Culture", "1")
  assert bs.parse_series("Earthsea Cycle #1-3") == ("Earthsea Cycle", "1")
  assert bs.parse_series("") == ("", "")
  assert bs.title_sort("The Crying of Lot 49") == "Crying of Lot 49, The"
  assert bs.title_sort("Het leven gezien van beneden") == "leven gezien van beneden, Het"
  assert bs.title_sort("Choke") == "Choke"


def test_the_series_endpoint_wins_over_a_books_own_series_title():
  mapping = bs.series_map([{"title": "Culture", "links": [{"bookId": 52, "position": "5"}]}])
  meta = bs.build_meta({"title": "Excession", "seriesTitle": ""}, None,
                       {"authorName": "Iain M. Banks"}, mapping.get(52))
  assert (meta.series, meta.series_index) == ("Culture", "5")


def test_a_non_integer_position_is_dropped_rather_than_breaking_the_int_parse():
  mapping = bs.series_map([{"title": "Hainish", "links": [{"bookId": 1, "position": "1.5"}]}])
  assert mapping[1] == ("Hainish", "")


def test_only_a_folder_of_nothing_but_our_sidecars_is_reapable():
  assert bs.sidecar_only({"metadata.opf", "cover.jpg"})
  assert bs.sidecar_only({"cover.jpg"})
  assert not bs.sidecar_only({"metadata.opf", "cover.jpg", "Author - Book.epub"})
  assert not bs.sidecar_only({"notes.txt"})
  assert not bs.sidecar_only(set())


# --- audiobook_merge -------------------------------------------------------------


def _part(n: int, title: str, dur: float = 600.0, kbps: int = 40, ch: int = 2) -> am.Part:
  return am.Part(host_path=f"/x/Book ({n:02}).mp3", duration=dur, kbps=kbps, channels=ch, title=title)


def test_parts_sort_by_their_padded_number_not_lexically():
  paths = ["A - B (10).mp3", "A - B (02).mp3", "A - B (01).mp3"]
  assert sorted(paths, key=am.part_number) == ["A - B (01).mp3", "A - B (02).mp3", "A - B (10).mp3"]


def test_chapter_names_come_from_meaningful_tags_and_fall_back_to_part_n():
  parts = [_part(1, "Chapter 1. The Sacrifice Poles"), _part(2, "002"), _part(3, "Track 3"), _part(4, "")]
  assert am.chapter_titles(parts) == ["Chapter 1. The Sacrifice Poles", "Part 2", "Part 3", "Part 4"]


def test_the_bitrate_follows_the_source_above_the_aac_lc_floor():
  assert am.target_kbps([_part(1, "", kbps=41, ch=2)]) == 48      # stereo floor
  assert am.target_kbps([_part(1, "", kbps=39, ch=1)]) == 39      # mono, above 32
  assert am.target_kbps([_part(1, "", kbps=20, ch=1)]) == 32      # mono floor
  assert am.target_kbps([_part(1, "", kbps=320, ch=2)]) == 128    # cap


def test_ffmetadata_chapters_are_contiguous_and_cover_every_part():
  parts = [_part(1, "One", 60.5), _part(2, "Two", 120.25)]
  meta = am.ffmetadata("Survivor", "Chuck Palahniuk", "1999", parts)
  assert meta.startswith(";FFMETADATA1\n")
  assert "START=0\nEND=60500\ntitle=One" in meta
  assert "START=60500\nEND=180750\ntitle=Two" in meta
  assert meta.count("[CHAPTER]") == 2


def test_ffmetadata_escapes_its_special_characters():
  meta = am.ffmetadata("A=B; C#", "X", "", [_part(1, "Ch=1")])
  assert "title=A\\=B\\; C\\#" in meta
  assert "title=Ch\\=1" in meta


def test_the_concat_list_quotes_apostrophes():
  assert am.concat_list(["/share/a/Ender's Game (01).mp3"]) == "file '/share/a/Ender'\\''s Game (01).mp3'\n"


def test_a_merge_is_only_kept_when_its_duration_matches_the_parts():
  assert am.duration_ok(28547.0, 28540.0)
  assert not am.duration_ok(28547.0, 14000.0)   # half the book: a truncated encode
  assert not am.duration_ok(0.0, 0.0)
