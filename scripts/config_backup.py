#!/usr/bin/env python3
"""Config backup & restore utility.

Creates timestamped tar.gz archives of service configuration directories.
Optionally prunes old backups and can restore a selected archive.

Environment variables:
  CONFIG_DIRECTORY (required)  Root directory containing per‑service subfolders
  BACKUP_DIR (optional)        Destination directory for archives (default: backups)
  BACKUP_RETAIN (optional)     How many most recent archives to keep (default: 7)

Usage examples:
  python scripts/config_backup.py                   # create backup
  python scripts/config_backup.py --retain 14       # override retention
  python scripts/config_backup.py --list            # list available archives
  python scripts/config_backup.py --restore <file>  # restore (extract) an archive

Exit codes:
  0 success
  1 partial (some paths missing) or non‑fatal warnings
  2 fatal error
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tarfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

# Best-effort load of .env so running via npm scripts / cron without explicit export works.
if "CONFIG_DIRECTORY" not in os.environ:
  try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except Exception:
    pass

DEFAULT_SERVICES = [
  "prowlarr",
  "sonarr",
  "radarr",
  "bazarr",
  "jellyfin",
  "swag",
  "qbittorrent",
  "lazylibrarian",
  "nextcloud",
]


def sha256_file(path: Path) -> str:
  h = hashlib.sha256()
  with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
      h.update(chunk)
  return h.hexdigest()


def list_archives(backup_dir: Path) -> list[Path]:
  return sorted(backup_dir.glob("configs-*.tar.gz"))


def create_backup(config_root: Path, backup_dir: Path, services: Iterable[str]) -> tuple[int, str]:
  missing = []
  added = []
  ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
  archive_path = backup_dir / f"configs-{ts}.tar.gz"

  try:
    with tarfile.open(archive_path, "w:gz") as tar:
      for svc in services:
        svc_path = config_root / svc
        if not svc_path.exists():
          missing.append(str(svc_path))
          continue
        tar.add(svc_path, arcname=svc)
        added.append(str(svc_path))
  except Exception as e:
    if archive_path.exists():
      archive_path.unlink(missing_ok=True)
    return 2, f"Failed to create archive: {e}"

  if not added:
    archive_path.unlink(missing_ok=True)
    return 2, "No service directories were found to back up (all missing?)"

  checksum = sha256_file(archive_path)
  size_mb = archive_path.stat().st_size / 1024 / 1024
  msg = f"Backup created: {archive_path} ({size_mb:.2f} MB)\nSHA256: {checksum}"
  if missing:
    msg += "\nMissing (not fatal):\n" + "\n".join(f"  - {m}" for m in missing)
    return 1, msg
  return 0, msg


def prune_archives(backup_dir: Path, retain: int) -> list[Path]:
  archives = list_archives(backup_dir)
  if len(archives) <= retain:
    return []
  to_delete = archives[:-retain]
  for old in to_delete:
    old.unlink(missing_ok=True)
  return to_delete


def restore_archive(backup_dir: Path, archive_name: str, target_root: Path) -> tuple[int, str]:
  archive_path = (
    (backup_dir / archive_name)
    if not archive_name.startswith(str(backup_dir))
    else Path(archive_name)
  )
  if not archive_path.exists():
    return 2, f"Archive not found: {archive_path}"
  try:
    with tarfile.open(archive_path, "r:gz") as tar:
      tar.extractall(path=target_root)
    return 0, f"Restored archive into {target_root} (NOTE: existing files overwritten)"
  except Exception as e:
    return 2, f"Restore failed: {e}"


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(description="Backup & restore service configuration directories")
  p.add_argument(
    "--services",
    nargs="*",
    default=DEFAULT_SERVICES,
    help="Subset of services to include (default: predefined list)",
  )
  p.add_argument(
    "--backup-dir", default=os.getenv("BACKUP_DIR", "backups"), help="Directory to store archives"
  )
  p.add_argument(
    "--retain",
    type=int,
    default=int(os.getenv("BACKUP_RETAIN", "7")),
    help="How many archives to retain after pruning",
  )
  p.add_argument("--list", action="store_true", help="List existing archives and exit")
  p.add_argument(
    "--restore", metavar="ARCHIVE", help="Restore the specified archive (filename or path)"
  )
  p.add_argument(
    "--prune-only", action="store_true", help="Only perform prune operation (no new backup)"
  )
  p.add_argument("--no-prune", action="store_true", help="Skip pruning old archives")
  return p.parse_args()


def main() -> int:
  args = parse_args()
  config_root_env = os.getenv("CONFIG_DIRECTORY")
  if not config_root_env:
    print(
      "❌ CONFIG_DIRECTORY environment variable is required (set in .env or export before running)"
    )
    return 2
  config_root = Path(config_root_env)
  if not config_root.exists():
    print(f"❌ CONFIG_DIRECTORY does not exist: {config_root}")
    return 2

  backup_dir = Path(args.backup_dir)
  backup_dir.mkdir(parents=True, exist_ok=True)

  if args.list:
    archives = list_archives(backup_dir)
    if not archives:
      print("(no archives found)")
      return 0
    for a in archives:
      size_mb = a.stat().st_size / 1024 / 1024
      print(f"{a.name}\t{size_mb:.2f} MB")
    return 0

  if args.restore:
    code, msg = restore_archive(backup_dir, args.restore, config_root)
    print(("✅" if code == 0 else "❌") + " " + msg)
    return code

  if args.prune_only:
    deleted = prune_archives(backup_dir, args.retain)
    print(f"Pruned {len(deleted)} archive(s)")
    return 0

  code, msg = create_backup(config_root, backup_dir, args.services)
  print(("✅" if code == 0 else ("⚠️" if code == 1 else "❌")) + " " + msg)

  if not args.no_prune:
    deleted = prune_archives(backup_dir, args.retain)
    if deleted:
      print(f"🧹 Pruned {len(deleted)} old archive(s)")
  return code


if __name__ == "__main__":
  sys.exit(main())
