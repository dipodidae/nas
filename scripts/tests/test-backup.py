import importlib.util
import sys
import tarfile
from pathlib import Path


def _load_backup_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "config-backup.py"
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
