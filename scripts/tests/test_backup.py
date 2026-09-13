import importlib.util
import sys
import tarfile
from pathlib import Path


def _load_backup_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "config_backup.py"
  spec = importlib.util.spec_from_file_location("config_backup", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  spec.loader.exec_module(module)  # type: ignore[attr-defined]
  return module


cb = _load_backup_module()


def create_file(path: Path, size: int = 0):
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("wb") as f:
    if size:
      f.write(b"x" * size)
    else:
      f.write(b"data")


def test_create_backup_success(tmp_path: Path):
  # Arrange
  config_root = tmp_path / "config"
  svc_a = config_root / "prowlarr"
  svc_b = config_root / "sonarr"
  create_file(svc_a / "file1.txt")
  create_file(svc_b / "nested" / "file2.log")

  backup_dir = tmp_path / "backups"
  backup_dir.mkdir()

  # Act
  code, msg = cb.create_backup(
    config_root,
    backup_dir,
    services=["prowlarr", "sonarr"],
    exclude_patterns=[],
    max_file_size_mb=None,
    progress=False,
    progress_interval=1000,
    keep_partial=False,
    do_checksum=False,
  )

  # Assert
  assert code == 0, msg
  archives = list(backup_dir.glob("configs-*.tar.gz"))
  assert len(archives) == 1
  with tarfile.open(archives[0], "r:gz") as tar:
    names = tar.getnames()
    assert "prowlarr/file1.txt" in names
    assert "sonarr/nested/file2.log" in names


def test_create_backup_with_missing_service(tmp_path: Path):
  config_root = tmp_path / "config"
  svc_a = config_root / "prowlarr"
  create_file(svc_a / "only.txt")
  backup_dir = tmp_path / "backups"
  backup_dir.mkdir()

  code, msg = cb.create_backup(
    config_root,
    backup_dir,
    services=["prowlarr", "missing"],
    exclude_patterns=[],
    max_file_size_mb=None,
    progress=False,
    progress_interval=500,
    keep_partial=False,
    do_checksum=False,
  )
  # Expect non-fatal missing service -> code 1
  assert code == 1
  assert "Missing service directories" in msg


# --- discovery ---------------------------------------------------------------
# The hard-coded list these replace named 9 of 28 directories, still listed the
# retired lazylibrarian (exit 1 every night, invisible under --ok-codes 0,1) and
# omitted lidarr, tinyauth, slskd and cleanuparr. ADR-0037.


def test_discovery_finds_every_service_directory(tmp_path: Path):
  root = tmp_path / "config"
  for name in ("lidarr", "tinyauth", "slskd", "cleanuparr"):
    (root / name).mkdir(parents=True)
  assert cb.discover_services(root) == ["cleanuparr", "lidarr", "slskd", "tinyauth"]


def test_discovery_skips_the_documented_names(tmp_path: Path):
  root = tmp_path / "config"
  for name in ("lidarr", "whisper", "streamystats-db", "backups"):
    (root / name).mkdir(parents=True)
  assert cb.discover_services(root) == ["lidarr"]


def test_discovery_skips_manual_snapshot_directories(tmp_path: Path):
  root = tmp_path / "config"
  (root / "qbittorrent").mkdir(parents=True)
  (root / "qbittorrent.bak.1788278865").mkdir(parents=True)
  assert cb.discover_services(root) == ["qbittorrent"]


def test_discovery_ignores_files(tmp_path: Path):
  root = tmp_path / "config"
  (root / "sonarr").mkdir(parents=True)
  (root / "notes.txt").write_text("x")
  assert cb.discover_services(root) == ["sonarr"]


# --- SQLite handling ---------------------------------------------------------


def _make_wal_db(path: Path, rows: int) -> None:
  import sqlite3

  path.parent.mkdir(parents=True, exist_ok=True)
  conn = sqlite3.connect(path)
  conn.execute("PRAGMA journal_mode=WAL")
  conn.execute("CREATE TABLE t (v INTEGER)")
  conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
  conn.commit()
  conn.close()


def test_is_sqlite_recognises_a_database(tmp_path: Path):
  db = tmp_path / "x.db"
  _make_wal_db(db, 3)
  assert cb.is_sqlite(db)
  plain = tmp_path / "x.conf"
  plain.write_text("[General]")
  assert not cb.is_sqlite(plain)


def test_snapshot_of_a_wal_database_contains_the_uncheckpointed_rows(tmp_path: Path):
  """The whole point: a raw copy of the .db alone would read back short."""
  import sqlite3

  db = tmp_path / "live.db"
  _make_wal_db(db, 500)
  conn = sqlite3.connect(db)  # hold it open, WAL not checkpointed
  conn.execute("INSERT INTO t VALUES (999999)")
  conn.commit()

  snap = tmp_path / "snap.db"
  assert cb.snapshot_sqlite(db, snap)
  conn.close()

  out = sqlite3.connect(snap)
  assert out.execute("SELECT count(*) FROM t").fetchone()[0] == 501
  out.close()


def test_snapshot_of_a_non_database_fails_cleanly(tmp_path: Path):
  junk = tmp_path / "junk.db"
  junk.write_bytes(b"SQLite format 3\x00" + b"garbage" * 100)
  dest = tmp_path / "out.db"
  assert not cb.snapshot_sqlite(junk, dest)
  assert not dest.exists()


def test_size_cap_never_drops_a_database(tmp_path: Path):
  """--fast's 25 MB cap silently dropped sonarr.db, prowlarr.db and jellyfin.db."""
  config_root = tmp_path / "config"
  _make_wal_db(config_root / "sonarr" / "sonarr.db", 20000)
  create_file(config_root / "sonarr" / "bulky.bin", size=400_000)
  backup_dir = tmp_path / "backups"
  backup_dir.mkdir()

  code, msg = cb.create_backup(
    config_root,
    backup_dir,
    services=["sonarr"],
    exclude_patterns=[],
    max_file_size_mb=0.1,  # 100 KB: smaller than both files
    progress=False,
    progress_interval=1000,
    keep_partial=False,
    do_checksum=False,
  )
  assert code == 0, msg
  with tarfile.open(next(backup_dir.glob("configs-*.tar.gz")), "r:gz") as tar:
    names = tar.getnames()
  assert "sonarr/sonarr.db" in names
  assert "sonarr/bulky.bin" not in names
  # And the skip is NAMED, not just counted -- a silent count is what hid this.
  assert "sonarr/bulky.bin" in msg
  assert "Databases snapshotted (WAL-consistent): 1" in msg


# --- exclusion patterns ------------------------------------------------------


def test_a_bang_pattern_reincludes_a_subtree(tmp_path: Path):
  config_root = tmp_path / "config"
  create_file(config_root / "nextcloud" / "www" / "nextcloud" / "apps" / "big.php")
  create_file(config_root / "nextcloud" / "www" / "nextcloud" / "config" / "config.php")
  backup_dir = tmp_path / "backups"
  backup_dir.mkdir()

  code, msg = cb.create_backup(
    config_root,
    backup_dir,
    services=["nextcloud"],
    exclude_patterns=["nextcloud/www/**", "!nextcloud/www/nextcloud/config/**"],
    max_file_size_mb=None,
    progress=False,
    progress_interval=1000,
    keep_partial=False,
    do_checksum=False,
  )
  assert code == 0, msg
  with tarfile.open(next(backup_dir.glob("configs-*.tar.gz")), "r:gz") as tar:
    names = tar.getnames()
  assert "nextcloud/www/nextcloud/config/config.php" in names
  assert "nextcloud/www/nextcloud/apps/big.php" not in names


def test_a_fake_database_falls_back_to_raw_and_obeys_the_size_cap(tmp_path: Path):
  """The SQLite exemption is for databases, not for anything wearing the header."""
  config_root = tmp_path / "config"
  fake = config_root / "svc" / "fake.db"
  fake.parent.mkdir(parents=True)
  fake.write_bytes(b"SQLite format 3\x00" + b"g" * 300_000)
  backup_dir = tmp_path / "backups"
  backup_dir.mkdir()

  code, msg = cb.create_backup(
    config_root,
    backup_dir,
    services=["svc"],
    exclude_patterns=[],
    max_file_size_mb=0.1,
    progress=False,
    progress_interval=1000,
    keep_partial=False,
    do_checksum=False,
  )
  assert code == 2  # nothing left to archive, which is the honest answer here
  assert "No files were added" in msg
