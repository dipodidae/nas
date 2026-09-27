"""check-books-stack.py must FAIL on each drift it exists to catch (ADR-0057)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
  "check_books_stack", Path(__file__).resolve().parents[1] / "check-books-stack.py")
cbs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cbs)


def _lib(name, path, ctype="books", realtime=True):
  return {"Name": name, "CollectionType": ctype, "Locations": [path],
          "LibraryOptions": {"EnableRealtimeMonitor": realtime}}


GOOD = [_lib("Books", "/data/movies/books/ebooks"),
        _lib("Audiobooks", "/data/movies/books/audiobooks"),
        _lib("Movies", "/data/movies/movies", ctype="movies")]


def test_the_live_shape_passes():
  assert cbs.library_findings(GOOD) == []
  assert cbs.plugin_findings([{"Name": "Bookshelf", "Status": "Active"}]) == []


def test_a_missing_library_fails():
  assert "no 'Audiobooks'" in cbs.library_findings(GOOD[:1] + GOOD[2:])[0]


def test_the_realtime_monitor_off_fails():
  broken = [_lib("Books", "/data/movies/books/ebooks", realtime=False)] + GOOD[1:]
  assert "realtime monitor OFF" in cbs.library_findings(broken)[0]


def test_a_wrong_path_or_type_fails():
  broken = [_lib("Books", "/data/movies/books", ctype="mixed")] + GOOD[1:]
  found = cbs.library_findings(broken)
  assert any("not 'books'" in f for f in found)
  assert any("does not point at" in f for f in found)


def test_a_plugin_that_is_not_active_fails():
  assert cbs.plugin_findings([{"Name": "Bookshelf", "Status": "NotSupported"}])
  assert cbs.plugin_findings([])
