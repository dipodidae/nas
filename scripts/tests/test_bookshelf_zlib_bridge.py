"""Tests for scripts/bookshelf_zlib_bridge.py -- Bookshelf's Z-Library/LibGen fallback.

The properties that matter:

* the matcher never picks a WRONG book (a summary, a study guide, a Spanish
  edition, a different author) -- an import is a move into the library, so a
  false positive is worse than a miss;
* a downloaded file whose bytes do not hash to the catalogue md5 never
  reaches Bookshelf;
* a flaky LibGen mirror never costs one of the ten daily Z-Library downloads,
  and never counts toward giving up on a book.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import threading
from pathlib import Path

import pytest

from scripts import bookshelf_zlib_bridge as z


def _want(**kw) -> z.Wanted:
  base = {"book_id": 1, "author_id": 1, "title": "Project Hail Mary", "author": "Andy Weir",
          "edition_id": "54493401", "isbns": frozenset({"9780593135204"}), "added": 0.0}
  return z.Wanted(**{**base, **kw})


def _cand(**kw) -> z.Candidate:
  base = {"zlib_id": 17576434, "zlib_hash": "d82cc1", "md5": "a" * 32,
          "title": "Project Hail Mary", "author": "Andy Weir", "extension": "epub",
          "language": "english", "filesize": 1_265_015, "isbns": frozenset()}
  return z.Candidate(**{**base, **kw})


# --- matching ----------------------------------------------------------------


def test_normalize_strips_accents_punctuation_and_a_leading_article():
  assert z.normalize("The Crying of Lot 49") == "crying of lot 49"
  assert z.normalize("José Saramago") == "jose saramago"
  assert z.normalize("De ontdekking van de hemel") == "ontdekking van de hemel"


def test_main_title_drops_subtitles_and_series_suffixes():
  assert z.main_title("Sapiens: A Brief History of Humankind") == "Sapiens"
  assert z.main_title("Dune (Dune #1)") == "Dune "
  assert z.main_title("Consider Phlebas - A Culture Novel") == "Consider Phlebas"


def test_isbns_are_found_in_a_comma_list_and_a_null_is_tolerated():
  assert z.isbns_in("9780593355275,059335527X,B08FHBV4ZX") == {"9780593355275", "059335527X"}
  assert z.isbns_in(None) == frozenset()


def test_an_isbn_hit_scores_highest_and_is_picked():
  exact = _cand(md5="b" * 32, isbns=frozenset({"9780593135204"}))
  plain = _cand(md5="c" * 32)
  assert z.pick(_want(), [plain, exact]) == exact


def test_epub_beats_pdf_for_the_same_book():
  assert z.pick(_want(isbns=frozenset()), [_cand(extension="pdf", md5="p" * 32),
                                           _cand(md5="e" * 32)]).extension == "epub"


@pytest.mark.parametrize("cand", [
  _cand(title="SUMMARY AND ANALYSIS OF PROJECT HAIL MARY BY ANDY WEIR"),
  _cand(title="Study Guide", author="SuperSummary"),
  _cand(title="Project Hail Mary", author="Harry Thomas"),
  _cand(title="Proyecto Hail Mary", language="spanish"),
  _cand(extension="djvu"),
], ids=["summary", "study-guide", "other-author", "spanish", "unknown-format"])
def test_a_wrong_book_is_never_picked_without_an_isbn(cand):
  assert z.pick(_want(isbns=frozenset()), [cand]) is None


def test_a_dutch_edition_is_acceptable():
  assert z.pick(_want(title="Het leven is vurrukkulluk", author="Remco Campert",
                      isbns=frozenset()),
                [_cand(title="Het leven is vurrukkulluk", author="Remco Campert",
                       language="dutch")]) is not None


def test_author_match_survives_last_first_and_initials():
  assert z.author_matches("Andy Weir", "Weir, Andy")
  assert z.author_matches("Ursula K. Le Guin", "Le Guin, Ursula K.")
  assert not z.author_matches("Andy Weir", "Harry Thomas")


# --- parsing the live shapes (measured 2026-09-27) --------------------------------


def test_a_wanted_record_takes_the_edition_id_and_isbns_from_the_edition_list():
  record = {"id": 1, "authorId": 1, "title": "Project Hail Mary", "foreignEditionId": "999",
            "author": {"authorName": "Andy Weir"}, "added": "2026-09-27T15:58:32Z"}
  editions = [{"foreignEditionId": "54493401", "isbn13": "9780593135204", "monitored": True}]
  want = z.parse_wanted(record, editions)
  assert want.edition_id == "54493401"
  assert want.isbns == {"9780593135204"}
  assert want.added == pytest.approx(1790524712.0)


def test_a_wanted_record_with_no_editions_falls_back_to_its_top_level_edition():
  record = {"id": 2, "authorId": 1, "title": "The Martian", "foreignEditionId": "18007564"}
  assert z.parse_wanted(record, []).edition_id == "18007564"


def test_candidates_skip_records_with_no_md5_and_keep_the_download_hash():
  payload = {"books": [
    {"id": 1, "md5": None, "title": "x"},
    {"id": 17576434, "hash": "d82cc1", "md5": "9E23FF64F275B3A70EF0CE3E5249336D",
     "title": "Project Hail Mary", "author": "Andy Weir", "extension": "EPUB",
     "language": "English", "filesize": 1265015, "identifier": "9780593355275"},
  ]}
  (cand,) = z.parse_candidates(payload)
  assert cand.md5 == "9e23ff64f275b3a70ef0ce3e5249336d"
  assert (cand.extension, cand.zlib_hash) == ("epub", "d82cc1")


# --- retry bookkeeping -------------------------------------------------------------


def test_a_transient_failure_retries_soon_and_does_not_count():
  entry: dict = {"tries": 0}
  z.record_failure(entry, z.Result("LibGen 503", transient=True), now=1000.0,
                   retry_s=86400, transient_s=3600)
  assert entry["tries"] == 0 and entry["next"] == 4600.0


def test_a_real_failure_counts_and_waits_the_long_interval():
  entry: dict = {"tries": 2}
  z.record_failure(entry, z.Result("no acceptable match"), now=1000.0,
                   retry_s=86400, transient_s=3600)
  assert entry["tries"] == 3 and entry["next"] == 87400.0


def test_due_respects_both_the_next_time_and_the_try_budget():
  state = {"1": {"tries": 1, "next": 500.0}, "2": {"tries": 5, "next": 0.0}}
  assert not z.due(state, 1, now=400.0, max_tries=5)
  assert z.due(state, 1, now=600.0, max_tries=5)
  assert not z.due(state, 2, now=600.0, max_tries=5)
  assert z.due(state, 3, now=600.0, max_tries=5)


def _args(**kw) -> argparse.Namespace:
  return argparse.Namespace(**{"book_id": None, "max_tries": 5, "grace_hours": 6.0, **kw})


def test_select_gives_the_torrent_search_its_grace_period_and_skips_queued_books():
  now = 1_790_600_000.0
  recs = [
    {"id": 1, "added": "2026-09-01T00:00:00Z"},   # old: eligible
    {"id": 2, "added": "2026-09-01T00:00:00Z"},   # queued in Bookshelf
    {"id": 3, "added": "2026-09-28T13:00:00Z"},   # added an hour ago
  ]
  assert [r["id"] for r in z.select(recs, {2}, {}, _args(), now)] == [1]


def test_an_explicit_book_id_ignores_grace_and_retry_state():
  recs = [{"id": 3, "added": "2099-01-01T00:00:00Z"}]
  state = {"3": {"tries": 9, "next": 9e12}}
  assert [r["id"] for r in z.select(recs, set(), state, _args(book_id=[3]), 0.0)] == [3]


# --- LibGen + download ------------------------------------------------------------


class _Files(http.server.BaseHTTPRequestHandler):
  pages: dict[str, tuple[int, bytes]] = {}

  def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's contract
    status, body = type(self).pages.get(self.path.split("?")[0], (404, b""))
    self.send_response(status)
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def log_message(self, *_args):
    pass


@pytest.fixture
def server():
  srv = http.server.HTTPServer(("127.0.0.1", 0), _Files)
  thread = threading.Thread(target=srv.serve_forever, daemon=True)
  thread.start()
  yield f"http://127.0.0.1:{srv.server_port}"
  srv.shutdown()
  srv.server_close()


def test_libgen_links_yield_one_url_per_mirror_that_has_the_md5(server, monkeypatch):
  md5 = "9e23ff64f275b3a70ef0ce3e5249336d"
  _Files.pages = {"/ads.php": (200, f'<a href="get.php?md5={md5}&amp;key=ABC123">GET</a>'.encode())}
  monkeypatch.setattr(z, "LIBGEN_MIRRORS", (server, "http://127.0.0.1:9"))
  assert list(z.libgen_links(md5)) == [f"{server}/get.php?md5={md5}&key=ABC123"]


def test_libgen_links_yield_nothing_when_no_mirror_has_it(server, monkeypatch):
  _Files.pages = {"/ads.php": (200, b"<html>no such file</html>")}
  monkeypatch.setattr(z, "LIBGEN_MIRRORS", (server,))
  assert list(z.libgen_links("0" * 32)) == []


def test_a_file_that_hashes_right_is_kept(server, tmp_path: Path):
  body = b"PK\x03\x04 an epub, honestly"
  _Files.pages = {"/f": (200, body)}
  dest = tmp_path / "x" / "book.epub"
  z.download(f"{server}/f", dest, hashlib.md5(body).hexdigest())
  assert dest.read_bytes() == body


def test_a_file_that_hashes_wrong_never_lands(server, tmp_path: Path):
  _Files.pages = {"/f": (200, b"<html>Welcome to nginx!</html>")}
  dest = tmp_path / "x" / "book.epub"
  with pytest.raises(RuntimeError, match="md5 mismatch"):
    z.download(f"{server}/f", dest, "a" * 32)
  assert not dest.exists()
  assert not list(dest.parent.glob("*.part"))


class _FakeZLib:
  def __init__(self, left: int) -> None:
    self.left, self.link_calls = left, 0

  def search(self, _q):
    return [_cand(md5="f" * 32, isbns=frozenset({"9780593135204"}))]

  def downloads_left(self) -> int:
    return self.left

  def download_link(self, _c) -> str:
    self.link_calls += 1
    raise AssertionError("must not spend Z-Library quota here")


def test_a_flaky_libgen_mirror_defers_instead_of_spending_zlibrary_quota(monkeypatch, tmp_path):
  monkeypatch.setattr(z, "libgen_links", lambda _md5: iter(["http://mirror/get.php"]))

  def _boom(*_a, **_k):
    raise OSError("503 Service Unavailable")

  monkeypatch.setattr(z, "download", _boom)
  zl = _FakeZLib(left=10)
  result = z.attempt(_want(), bs=None, zl=zl, staging=tmp_path)
  assert result.transient and "503" in result.reason
  assert zl.link_calls == 0


def test_a_spent_zlibrary_quota_defers_rather_than_fails(monkeypatch, tmp_path):
  monkeypatch.setattr(z, "libgen_links", lambda _md5: iter([]))
  zl = _FakeZLib(left=0)
  result = z.attempt(_want(), bs=None, zl=zl, staging=tmp_path)
  assert result.transient and "spent" in result.reason
  assert zl.link_calls == 0


def test_max_books_zero_attempts_nothing(monkeypatch, tmp_path, capsys):
  """Regression: the limit was checked AFTER appending, so --max-books 0 still
  attempted one real book."""
  class FakeBookshelf:
    def __init__(self, *_a):
      pass

    def missing(self):
      return [{"id": 7, "authorId": 1, "title": "Choke", "foreignEditionId": "1",
               "added": "2020-01-01T00:00:00Z"}]

    def queued_book_ids(self):
      return set()

    def editions(self, _book_id):
      return []

  monkeypatch.setattr(z, "Bookshelf", FakeBookshelf)
  monkeypatch.setattr(z, "STATE_PATH", tmp_path / "state.json")
  monkeypatch.setattr(z, "attempt", lambda *_a, **_k: pytest.fail("attempted a book"))
  monkeypatch.setenv("API_KEY_BOOKSHELF", "k")
  monkeypatch.setenv("SHARE_DIRECTORY", str(tmp_path))
  assert z.main(["--max-books", "0"]) == 0
  assert "0 to attempt" in capsys.readouterr().out
