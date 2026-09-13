"""Tests for scripts/check_backup_contents.py — the archive-contents assertion.

This check exists because the nightly job ran, exited 0 and wrote 437 MB for
months while containing none of the databases. So the thing under test is
specifically: does a plausible-looking archive with a missing or empty database
get caught?
"""

from __future__ import annotations

import importlib.util
import sys
import tarfile
import time
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  spec = importlib.util.spec_from_file_location(
    "check_backup_contents", scripts_dir / "check_backup_contents.py"
  )
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


cbc = _load_module()

REQ = {
  "sonarr/sonarr.db": "series",
  "swag/nginx/proxy-confs/*.subdomain.conf": "the public surface",
}


def test_all_members_present_is_no_gap():
  names = ["sonarr/sonarr.db", "swag/nginx/proxy-confs/qui.subdomain.conf"]
  sizes = dict.fromkeys(names, 4096)
  assert cbc.missing_members(names, sizes, REQ) == []


def test_a_missing_database_is_a_gap():
  names = ["sonarr/MediaCover/1/poster.jpg", "swag/nginx/proxy-confs/qui.subdomain.conf"]
  gaps = cbc.missing_members(names, dict.fromkeys(names, 4096), REQ)
  assert len(gaps) == 1
  assert gaps[0].startswith("sonarr/sonarr.db")


def test_a_zero_byte_database_is_a_gap():
  """A truncated snapshot restores nothing, and tar happily stores it."""
  names = ["sonarr/sonarr.db", "swag/nginx/proxy-confs/qui.subdomain.conf"]
  sizes = {"sonarr/sonarr.db": 0, "swag/nginx/proxy-confs/qui.subdomain.conf": 4096}
  gaps = cbc.missing_members(names, sizes, REQ)
  assert len(gaps) == 1
  assert "0 bytes" in gaps[0]


def test_a_glob_needs_only_one_non_empty_match():
  names = [
    "sonarr/sonarr.db",
    "swag/nginx/proxy-confs/empty.subdomain.conf",
    "swag/nginx/proxy-confs/qui.subdomain.conf",
  ]
  sizes = {names[0]: 10, names[1]: 0, names[2]: 4096}
  assert cbc.missing_members(names, sizes, REQ) == []


def _write_archive(backup_dir: Path, stamp: str, members: dict[str, bytes]) -> Path:
  path = backup_dir / f"configs-{stamp}.tar.gz"
  with tarfile.open(path, "w:gz") as tar:
    for name, payload in members.items():
      src = backup_dir / "staging" / name
      src.parent.mkdir(parents=True, exist_ok=True)
      src.write_bytes(payload)
      tar.add(src, arcname=name)
  return path


def test_newest_archive_picks_the_most_recent(tmp_path: Path):
  old = _write_archive(tmp_path, "20260101-000000", {"a/b.db": b"x"})
  new = _write_archive(tmp_path, "20260202-000000", {"a/b.db": b"x"})
  import os

  os.utime(old, (1000, 1000))
  os.utime(new, (2000, 2000))
  assert cbc.newest_archive(tmp_path) == new


def test_no_archive_directory_is_fatal(tmp_path: Path):
  assert cbc.main(["--backup-dir", str(tmp_path / "nope")]) == 2


def test_empty_archive_directory_is_fatal(tmp_path: Path):
  assert cbc.main(["--backup-dir", str(tmp_path)]) == 2


def test_a_stale_archive_fails(tmp_path: Path, monkeypatch):
  archive = _write_archive(tmp_path, "20260101-000000", {"sonarr/sonarr.db": b"x" * 10})
  import os

  old = time.time() - 5 * 86400
  os.utime(archive, (old, old))
  monkeypatch.setattr(cbc, "REQUIRED", {"sonarr/sonarr.db": "series"})
  assert cbc.main(["--backup-dir", str(tmp_path), "--max-age-hours", "36"]) == 1


def test_a_fresh_complete_archive_passes(tmp_path: Path, monkeypatch):
  _write_archive(tmp_path, "20260101-000000", {"sonarr/sonarr.db": b"x" * 10})
  monkeypatch.setattr(cbc, "REQUIRED", {"sonarr/sonarr.db": "series"})
  assert cbc.main(["--backup-dir", str(tmp_path)]) == 0
