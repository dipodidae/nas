#!/usr/bin/env python3
"""Config backup & restore utility.

Creates timestamped tar.gz archives of service configuration directories.
Optionally prunes old backups and can restore a selected archive.

NEW FEATURES:
  * Exclusion patterns (``--exclude`` / ``--exclude-from`` / ``--default-excludes``)
  * Fast mode (``--fast``) – curated excludes + size limit + logs excluded
  * Size threshold skipping (``--max-file-size``)
  * Optional checksum skipping (``--no-checksum``)
  * Progress feedback with counters (auto when TTY or ``--progress``)
  * Graceful interrupt handling (Ctrl+C cleans up partial archive unless ``--keep-partial``)

Environment variables:
  CONFIG_DIRECTORY (required)  Root directory containing per‑service subfolders
  BACKUP_DIR (optional)        Destination directory for archives (default: CONFIG_DIRECTORY/backups)
  BACKUP_RETAIN (optional)     How many most recent archives to keep (default: 7)

Usage examples:
  python scripts/config_backup.py                      # create backup
  python scripts/config_backup.py --retain 14          # override retention
  python scripts/config_backup.py --list               # list archives
  python scripts/config_backup.py --restore <file>     # restore (extract) an archive
  python scripts/config_backup.py --fast               # fast backup (excludes heavy dirs, big files, logs)
  python scripts/config_backup.py --exclude-from patterns.txt --no-checksum

Exit codes:
  0 success
  1 partial (some service directories missing)
  2 fatal error (including interrupted / no data)
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import sys
import tarfile
from collections.abc import Iterable
from datetime import UTC, datetime
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


DEFAULT_EXCLUDES = [
  "jellyfin/cache/**",
  "jellyfin/transcodes/**",
  "nextcloud/data/**",
  "qbittorrent/temp/**",
]
FAST_MODE_EXTRA_EXCLUDES = ["**/logs/**"]


def _compile_patterns(patterns: list[str]) -> list[str]:
  # Patterns are used directly with fnmatch.fnmatchcase; normalization placeholder if needed later.
  return patterns


def create_backup(
  config_root: Path,
  backup_dir: Path,
  services: Iterable[str],
  *,
  exclude_patterns: list[str],
  max_file_size_mb: float | None,
  progress: bool,
  progress_interval: int,
  keep_partial: bool,
  do_checksum: bool,
) -> tuple[int, str]:
  missing: list[str] = []
  ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
  archive_path = backup_dir / f"configs-{ts}.tar.gz"
  added_files = 0
  added_bytes = 0
  skipped_excluded = 0
  skipped_size = 0
  services_done = 0
  service_list = list(services)
  size_threshold = (max_file_size_mb * 1024 * 1024) if max_file_size_mb is not None else None
  patterns = _compile_patterns(exclude_patterns)

  def is_excluded(rel_path: str) -> bool:
    # Match path and path with trailing slash for directory semantics.
    for pat in patterns:
      if fnmatch.fnmatchcase(rel_path, pat) or fnmatch.fnmatchcase(rel_path.rstrip("/") + "/", pat):
        return True
    return False

  try:
    with tarfile.open(archive_path, "w:gz") as tar:
      for svc in service_list:
        svc_path = config_root / svc
        if not svc_path.exists():
          missing.append(str(svc_path))
          continue
        # Walk manually so we can exclude & enforce size limits.
        base_len = len(str(svc_path.parent)) + 1  # for relative slicing
        for root, dirs, files in os.walk(svc_path):
          # Derive a portable relative path (service-root-relative) without using fragile Path slicing.
          rel_root = str(root)[base_len:].replace(os.sep, "/")
          # Prune excluded directories in-place to prevent descending.
          pruned_dirs: list[str] = []
          for d in list(dirs):
            rel_dir_path = (
              f"{rel_root}/{d}" if rel_root else f"{svc}/{d}"
            )  # rel_root should normally start with service name
            if is_excluded(rel_dir_path.rstrip("/") + "/"):
              pruned_dirs.append(d)
          for d in pruned_dirs:
            dirs.remove(d)

          for fname in files:
            rel_file_path = f"{rel_root}/{fname}" if rel_root else f"{svc}/{fname}"
            if is_excluded(rel_file_path):
              skipped_excluded += 1
              continue
            fpath = Path(root) / fname
            try:
              st = fpath.stat()
            except FileNotFoundError:
              # Transient disappearance (e.g., file rotated) – skip silently.
              continue
            if size_threshold is not None and st.st_size > size_threshold:
              skipped_size += 1
              continue
            try:
              tar.add(fpath, arcname=rel_file_path)
              added_files += 1
              added_bytes += st.st_size
            except Exception:
              # Non-fatal; skip this file.
              continue
            if progress and added_files % progress_interval == 0:
              print(
                f"… {added_files} files ({added_bytes / 1024 / 1024:.1f} MB) | "
                f"skipped excl {skipped_excluded} size {skipped_size} | svc {services_done}/{len(service_list)}",
                file=sys.stderr,
              )
        services_done += 1
  except KeyboardInterrupt:
    # User interrupted – clean up unless keeping partial.
    if archive_path.exists() and not keep_partial:
      archive_path.unlink(missing_ok=True)
    return 2, (
      "Interrupted by user (Ctrl+C). Partial archive removed."
      if not keep_partial
      else "Interrupted by user (Ctrl+C). Partial archive kept."
    )
  except Exception as e:  # pragma: no cover - unexpected paths
    if archive_path.exists():
      archive_path.unlink(missing_ok=True)
    return 2, f"Failed to create archive: {e}"

  if added_files == 0:
    if archive_path.exists():
      archive_path.unlink(missing_ok=True)
    return (
      2,
      "No files were added to the archive (all services missing or exclusions removed everything).",
    )

  checksum = sha256_file(archive_path) if do_checksum else "(skipped)"
  size_mb = archive_path.stat().st_size / 1024 / 1024
  msg_lines = [
    f"Backup created: {archive_path} ({size_mb:.2f} MB)",
    f"SHA256: {checksum}",
    f"Services processed: {services_done}/{len(service_list)}",
    f"Files added: {added_files} | Skipped (exclude): {skipped_excluded} | Skipped (size): {skipped_size}",
    f"Added size (raw, uncompressed): {added_bytes / 1024 / 1024:.2f} MB",
  ]
  if missing:
    msg_lines.append("Missing service directories (not fatal):")
    msg_lines.extend(f"  - {m}" for m in missing)
  return (1 if missing else 0), "\n".join(msg_lines)


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
    "--backup-dir",
    default=None,
    help="Directory to store archives (default: CONFIG_DIRECTORY/backups or $BACKUP_DIR if set)",
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
  # Exclusions & performance tuning
  p.add_argument(
    "--exclude",
    action="append",
    default=[],
    metavar="PATTERN",
    help="Glob pattern to exclude (relative paths like 'jellyfin/cache/**'). Can be repeated.",
  )
  p.add_argument(
    "--exclude-from",
    metavar="FILE",
    help="File containing exclusion patterns (one per line, supports # comments)",
  )
  p.add_argument(
    "--default-excludes",
    action="store_true",
    help="Apply curated default heavy/cache exclusions",
  )
  p.add_argument(
    "--fast",
    action="store_true",
    help="Fast mode: default excludes + logs + size cap (implies --max-file-size and --default-excludes)",
  )
  p.add_argument(
    "--max-file-size",
    type=float,
    metavar="MB",
    help="Skip individual files larger than this size (MB)",
  )
  p.add_argument(
    "--progress",
    dest="progress_flag",
    action="store_true",
    help="Force enable progress output (stderr)",
  )
  p.add_argument(
    "--no-progress",
    dest="no_progress_flag",
    action="store_true",
    help="Force disable progress even if TTY",
  )
  p.add_argument(
    "--progress-interval",
    type=int,
    default=250,
    metavar="N",
    help=argparse.SUPPRESS,
  )
  p.add_argument(
    "--keep-partial",
    action="store_true",
    help="Keep a partially written archive if interrupted (Ctrl+C)",
  )
  p.add_argument(
    "--no-checksum",
    action="store_true",
    help="Skip SHA256 checksum calculation for speed",
  )
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

  env_backup_dir = os.getenv("BACKUP_DIR")
  if args.backup_dir is not None:
    backup_dir = Path(args.backup_dir)
  elif env_backup_dir:
    backup_dir = Path(env_backup_dir)
  else:
    backup_dir = config_root / "backups"
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

  # Build exclusion list
  patterns: list[str] = []
  if args.default_excludes or args.fast:
    patterns.extend(DEFAULT_EXCLUDES)
  if args.fast:
    # User accepted logs exclusion for fast mode.
    patterns.extend(FAST_MODE_EXTRA_EXCLUDES)
  if args.exclude_from:
    try:
      with open(args.exclude_from, encoding="utf-8") as ef:
        for line in ef:
          s = line.strip()
          if not s or s.startswith("#"):
            continue
          patterns.append(s)
    except FileNotFoundError:
      print(f"⚠️ Exclude file not found: {args.exclude_from}", file=sys.stderr)
  if args.exclude:
    patterns.extend(args.exclude)

  # Fast mode default size threshold unless user provided explicit value.
  max_file_size = args.max_file_size
  if args.fast and max_file_size is None:
    # User answered 'no' to 50MB; default to 25MB as a conservative fast size.
    max_file_size = 25.0

  auto_progress = sys.stderr.isatty()
  if args.progress_flag:
    auto_progress = True
  if args.no_progress_flag:
    auto_progress = False

  code, msg = create_backup(
    config_root,
    backup_dir,
    args.services,
    exclude_patterns=patterns,
    max_file_size_mb=max_file_size,
    progress=auto_progress,
    progress_interval=args.progress_interval,
    keep_partial=args.keep_partial,
    do_checksum=not args.no_checksum,
  )
  print(("✅" if code == 0 else ("⚠️" if code == 1 else "❌")) + " " + msg)

  if not args.no_prune:
    deleted = prune_archives(backup_dir, args.retain)
    if deleted:
      print(f"🧹 Pruned {len(deleted)} old archive(s)")
  return code


if __name__ == "__main__":
  sys.exit(main())
