"""Unit tests for the offline Lidarr repath.

The repath is destructive (ADR-0003: PUT /api/v1/artist/editor emptied 150,187
TrackFiles rows). These tests cover the part that decides what a path becomes,
and the part that decides which rows are eligible -- the two places a bug
writes a wrong path into 168,595 rows.
"""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "lidarr_repath_db.py"
  spec = importlib.util.spec_from_file_location("lidarr_repath_db", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


_m = _load_module()
REWRITE_TARGETS = _m.REWRITE_TARGETS
apply_rewrite = _m.apply_rewrite
plan_rewrite = _m.plan_rewrite
rewrite_path = _m.rewrite_path


# --- pure path logic ------------------------------------------------------


def test_rewrite_path_swaps_only_the_root_prefix():
  assert rewrite_path("/music/Aphex Twin", "/music", "/data/music") == "/data/music/Aphex Twin"


def test_rewrite_path_preserves_nested_structure():
  assert (
    rewrite_path("/music/Boards of Canada/Geogaddi/01.flac", "/music", "/data/music")
    == "/data/music/Boards of Canada/Geogaddi/01.flac"
  )


def test_rewrite_path_rewrites_the_bare_root_itself():
  """RootFolders.Path is exactly '/music', with no trailing segment."""
  assert rewrite_path("/music", "/music", "/data/music") == "/data/music"


def test_rewrite_path_is_idempotent_on_already_migrated_paths():
  """Running the tool twice must not produce /data/music/data/music/..."""
  assert rewrite_path("/data/music/Aphex Twin", "/music", "/data/music") == "/data/music/Aphex Twin"


def test_rewrite_path_refuses_a_path_outside_the_old_root():
  with pytest.raises(ValueError, match="not under"):
    rewrite_path("/downloads/thing", "/music", "/data/music")


def test_rewrite_path_does_not_match_a_sibling_with_a_shared_prefix():
  """/musicvideos must not be treated as living under /music."""
  with pytest.raises(ValueError, match="not under"):
    rewrite_path("/musicvideos/thing", "/music", "/data/music")


def test_rewrite_path_rejects_a_traversal_segment():
  with pytest.raises(ValueError, match="traversal"):
    rewrite_path("/music/../etc/passwd", "/music", "/data/music")


def test_rewrite_path_leaves_a_relative_path_alone():
  """MetadataFiles.RelativePath is 28,341 relative rows and 14,958 absolute
  ones in the live DB. The relative ones are not ours to touch."""
  with pytest.raises(ValueError, match="not under"):
    rewrite_path("artist.nfo", "/music", "/data/music")


# --- SQL, against a synthetic database ------------------------------------


def _db():
  c = sqlite3.connect(":memory:")
  c.executescript("""
    CREATE TABLE RootFolders (Id INTEGER PRIMARY KEY, Path TEXT NOT NULL);
    CREATE TABLE Artists (Id INTEGER PRIMARY KEY, Path TEXT NOT NULL);
    CREATE TABLE TrackFiles (Id INTEGER PRIMARY KEY, Path TEXT NOT NULL UNIQUE);
    CREATE TABLE MetadataFiles (Id INTEGER PRIMARY KEY, RelativePath TEXT NOT NULL);
    INSERT INTO RootFolders VALUES (1, '/music');
    INSERT INTO Artists VALUES (1, '/music/Burzum'), (2, '/music/Autechre');
    INSERT INTO TrackFiles VALUES (1, '/music/Burzum/01.mp3'), (2, '/music/Autechre/02.flac');
    INSERT INTO MetadataFiles VALUES
      (1, '/music/Burzum/album.nfo'), (2, 'artist.nfo'), (3, '/music/Autechre/album.nfo');
  """)
  return c


def test_rewrite_targets_covers_every_path_column_including_metadatafiles():
  """MetadataFiles.RelativePath holds 14,958 ABSOLUTE paths in the live DB
  despite its name. Omitting it orphans every .nfo."""
  assert ("MetadataFiles", "RelativePath") in REWRITE_TARGETS
  assert ("TrackFiles", "Path") in REWRITE_TARGETS
  assert ("Artists", "Path") in REWRITE_TARGETS
  assert ("RootFolders", "Path") in REWRITE_TARGETS


def test_plan_rewrite_counts_only_eligible_rows():
  plan = {p["table"]: p for p in plan_rewrite(_db(), "/music", "/data/music")}
  assert plan["RootFolders"]["eligible"] == 1
  assert plan["Artists"]["eligible"] == 2
  assert plan["TrackFiles"]["eligible"] == 2
  # 2 of the 3 MetadataFiles rows are absolute; 'artist.nfo' is skipped
  assert plan["MetadataFiles"]["eligible"] == 2
  assert plan["MetadataFiles"]["skipped_relative"] == 1


def test_apply_rewrite_changes_every_eligible_row_and_nothing_else():
  conn = _db()
  apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
  assert conn.execute("SELECT Path FROM RootFolders").fetchone()[0] == "/data/music"
  assert [r[0] for r in conn.execute("SELECT Path FROM TrackFiles ORDER BY Id")] == [
    "/data/music/Burzum/01.mp3", "/data/music/Autechre/02.flac"]
  assert [r[0] for r in conn.execute("SELECT RelativePath FROM MetadataFiles ORDER BY Id")] == [
    "/data/music/Burzum/album.nfo", "artist.nfo", "/data/music/Autechre/album.nfo"]


def test_apply_rewrite_preserves_row_counts_exactly():
  """The ADR-0003 failure was rows disappearing. Assert the count, always."""
  conn = _db()
  before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("RootFolders", "Artists", "TrackFiles", "MetadataFiles")}
  apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
  after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
  assert before == after


def test_apply_rewrite_is_idempotent():
  conn = _db()
  apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
  second = plan_rewrite(conn, "/music", "/data/music")
  assert sum(p["eligible"] for p in second) == 0


def test_apply_rewrite_rolls_back_entirely_on_error():
  """A partial rewrite is worse than none: half the rows pointing at a root
  that no longer exists is not a state anything recovers from."""
  conn = _db()
  conn.execute("INSERT INTO TrackFiles VALUES (99, '/data/music/Burzum/01.mp3')")
  with pytest.raises(sqlite3.IntegrityError):
    apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
  unchanged = conn.execute("SELECT Path FROM TrackFiles WHERE Id=1").fetchone()[0]
  assert unchanged == "/music/Burzum/01.mp3"
