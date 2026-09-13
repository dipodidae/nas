#!/usr/bin/env python3
"""Config backup & restore utility.

Creates timestamped tar.gz archives of service configuration directories.
Optionally prunes old backups and can restore a selected archive.

What it protects, and what it deliberately does not
---------------------------------------------------
The service set is **discovered** from ``CONFIG_DIRECTORY`` rather than
hard-coded. The hard-coded list this replaced (2026-09-13) named nine services
of the twenty-eight on disk and had drifted: it still listed ``lazylibrarian``,
retired long ago, which made the job exit 1 every night -- and it silently
omitted ``lidarr``, ``tinyauth``, ``slskd``, ``qui``, ``cleanuparr``, ``ntfy``
and ``jellyseerr``. ADR-0036 tells you to copy ``${CONFIG_DIRECTORY}/tinyauth``
before a tag bump because its migration is one-way; nothing was copying it.

Two rules keep the archive both complete and small:

  * ``SKIP_SERVICES`` -- whole directories that must NOT be tarred, each with
    the reason. A live Postgres data directory belongs here: a file-level tar
    of a running cluster is not a restorable backup.
  * ``DEFAULT_EXCLUDES`` -- regenerable subtrees (poster caches, extracted
    subtitle streams, each app's own DB dumps, the Nextcloud PHP tree). A
    pattern may start with ``!`` to re-include a subtree the line above it
    excluded, gitignore-style; that is how ``nextcloud/www`` is dropped while
    ``nextcloud/www/nextcloud/config`` is kept.

Databases are snapshotted, not copied
-------------------------------------
Every *arr, Jellyfin, slskd and qBittorrent database here is SQLite in **WAL**
mode, where the newest committed rows live in ``<db>-wal`` until a checkpoint.
Tarring ``sonarr.db`` alone reads back a stale -- possibly torn -- database, the
same trap CLAUDE.md documents for reading a just-saved setting. So any file with
the SQLite header is copied through ``sqlite3.Connection.backup()``, the online
backup API, which produces one consistent file with the WAL already folded in;
``-wal``/``-shm``/``-journal`` sidecars are then excluded as redundant.

``--max-file-size`` never applies to a database. It used to: ``--fast`` sets a
25 MB cap, and that cap alone was quietly dropping ``sonarr.db`` (41 MB),
``prowlarr.db`` (32 MB) and Jellyfin's ``library.db`` out of every nightly
archive while the run reported success. Files that ARE skipped by size are now
named in the summary instead of being counted, so it cannot hide again.

Verify by effect, not by exit code: ``scripts/check_backup_contents.py``
(wired into ``make verify-runtime``) opens the newest archive and asserts the
databases are actually inside it.

Environment variables:
  CONFIG_DIRECTORY (required)  Root directory containing per-service subfolders
  BACKUP_DIR (optional)        Destination directory for archives (default: CONFIG_DIRECTORY/backups)
  BACKUP_RETAIN (optional)     How many most recent archives to keep (default: 7)

Usage examples:
  python scripts/config_backup.py                      # create backup (all discovered services)
  python scripts/config_backup.py --plan               # show what WOULD be archived, write nothing
  python scripts/config_backup.py --retain 14          # override retention
  python scripts/config_backup.py --list               # list archives
  python scripts/config_backup.py --restore <file>     # restore (extract) an archive
  python scripts/config_backup.py --fast               # fast backup (excludes heavy dirs, big files, logs)
  python scripts/config_backup.py --services sonarr radarr   # explicit subset

Exit codes:
  0 success
  1 partial (an explicitly requested service directory is missing)
  2 fatal error (including interrupted / no data)
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import os
import sqlite3
import sys
import tarfile
import tempfile
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

# Directories under CONFIG_DIRECTORY that must NOT be archived, with the reason.
# Anything not listed here is discovered and included, so a new service is
# protected the day it is created rather than the day someone remembers a list.
SKIP_SERVICES = {
  "whisper": "~460 MB of re-downloadable model weights",
  "playlist-generator-db": "live Postgres data dir - needs pg_dump; a file tar of a running cluster does not restore",
  "streamystats-db": "live Postgres data dir - same reason",
  "beszel-agent": "the agent keeps no state of its own",
  "backups": "the archive destination itself, when it lives under CONFIG_DIRECTORY",
}

# Ad-hoc manual snapshots (e.g. qbittorrent.bak.1788278865) are skipped by shape.
SKIP_SERVICE_GLOBS = ["*.bak.*", "*.old", "*.orig"]


def discover_services(config_root: Path) -> list[str]:
  """Every service directory under config_root that is not deliberately skipped.

  Pure apart from the directory listing. Sorted, so an archive's member order
  is stable and two runs diff cleanly.
  """
  found: list[str] = []
  try:
    entries = sorted(p.name for p in config_root.iterdir() if p.is_dir())
  except OSError:
    return []
  for name in entries:
    if name in SKIP_SERVICES:
      continue
    if any(fnmatch.fnmatchcase(name, g) for g in SKIP_SERVICE_GLOBS):
      continue
    found.append(name)
  return found



def sha256_file(path: Path) -> str:
  h = hashlib.sha256()
  with path.open("rb") as f:
    for chunk in iter(lambda: f.read(1024 * 1024), b""):
      h.update(chunk)
  return h.hexdigest()


def list_archives(backup_dir: Path) -> list[Path]:
  return sorted(backup_dir.glob("configs-*.tar.gz"))


DEFAULT_EXCLUDES = [
  # Regenerated on next start, or re-fetched from the internet.
  "*/cache/**",
  "jellyfin/transcodes/**",
  "jellyfin/data/transcodes/**",
  "jellyfin/data/temp/**",
  "jellyfin/data/metadata/**",  # ~18 GB of re-fetchable artwork and NFO
  "jellyfin/data/data/subtitles/**",  # extracted .sup streams, re-extractable
  "*/MediaCover/**",  # *arr poster caches: 7.7 GB in lidarr alone
  "*/Backups/**",  # each *arr's own DB dump; this archive supersedes it
  "recyclarr/resources/**",  # git clone of the TRaSH guides
  # Nextcloud: the PHP application reinstalls, the config does not.
  "nextcloud/data/**",  # user files -- that is SHARE_DIRECTORY's job
  "nextcloud/www/**",
  "!nextcloud/www/nextcloud/config/**",
  "qbittorrent/temp/**",
  # slskd rebuilds all of these; events.db alone is ~460 MB of history.
  "slskd/data/backups/**",
  "slskd/data/browse.cache",
  "slskd/data/*.bak.db",
  "slskd/data/events.db*",
  "slskd/data/search.db*",
  "slskd/data/shares.local*",
  # Logs, in every dialect this stack spells them.
  "**/logs.db*",
  "**/log/**",
  # Redundant once a database is snapshotted through the online backup API.
  "**/*-wal",
  "**/*-shm",
  "**/*-journal",
]

SQLITE_MAGIC = b"SQLite format 3\x00"


def is_sqlite(path: Path) -> bool:
  """True if the file carries the SQLite header. Cheap: reads 16 bytes."""
  try:
    with path.open("rb") as f:
      return f.read(16) == SQLITE_MAGIC
  except OSError:
    return False


def snapshot_sqlite(src: Path, dest: Path) -> bool:
  """Consistent copy of a live WAL database via sqlite3's online backup API.

  Returns False (leaving no partial file) if the database is locked, encrypted
  or corrupt, so the caller can fall back to a raw copy rather than lose it.
  """
  source = target = None
  try:
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
    target = sqlite3.connect(dest)
    source.backup(target)
    return True
  except (sqlite3.Error, OSError):
    dest.unlink(missing_ok=True)
    return False
  finally:
    for conn in (target, source):
      if conn is not None:
        with contextlib.suppress(sqlite3.Error):
          conn.close()


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
  snapshot_databases: bool = True,
) -> tuple[int, str]:
  missing: list[str] = []
  ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
  archive_path = backup_dir / f"configs-{ts}.tar.gz"
  added_files = 0
  added_bytes = 0
  skipped_excluded = 0
  skipped_size: list[str] = []
  db_snapshots = 0
  db_fallbacks: list[str] = []
  snap_seq = 0
  services_done = 0
  service_list = list(services)
  size_threshold = (max_file_size_mb * 1024 * 1024) if max_file_size_mb is not None else None
  # Re-includes (gitignore-style "!pattern") win over every exclude, so a small
  # config subtree can be kept out of a large excluded one.
  keeps = [pat[1:] for pat in exclude_patterns if pat.startswith("!")]
  patterns = _compile_patterns([pat for pat in exclude_patterns if not pat.startswith("!")])

  def _matches(rel_path: str, pats: list[str]) -> bool:
    # Match path and path with trailing slash for directory semantics.
    return any(
      fnmatch.fnmatchcase(rel_path, pat) or fnmatch.fnmatchcase(rel_path.rstrip("/") + "/", pat)
      for pat in pats
    )

  def is_excluded(rel_path: str) -> bool:
    if _matches(rel_path, keeps):
      return False
    return _matches(rel_path, patterns)

  def can_prune(rel_dir: str) -> bool:
    """Excluded, and no re-include can still match something beneath it.

    Pruning is what makes the walk cheap, but pruning ``nextcloud/www/`` would
    also drop ``!nextcloud/www/nextcloud/config/**`` before the walk ever
    reaches it -- a re-include that silently does nothing is worse than none.
    """
    if not is_excluded(rel_dir):
      return False
    prefix = rel_dir.rstrip("/") + "/"
    return not any(pat.startswith(prefix) for pat in keeps)

  # Stage database snapshots beside the archive, not in /tmp: lidarr.db alone is
  # 2.3 GB and the backup target is the disk with room for it.
  staging = tempfile.TemporaryDirectory(prefix=".dbsnap-", dir=str(backup_dir))
  staging_root = Path(staging.name)

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
            if can_prune(rel_dir_path.rstrip("/") + "/"):
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
              # Transient disappearance (e.g., file rotated) - skip silently.
              continue

            # A database is never dropped for being large: the size cap exists
            # to skip bulky media leftovers, and applying it to sonarr.db is
            # how three databases fell out of every archive unnoticed.
            source_path = fpath
            is_db = snapshot_databases and is_sqlite(fpath)
            if is_db:
              snap_seq += 1
              snap = staging_root / f"{snap_seq}-{fname}"
              if snapshot_sqlite(fpath, snap):
                source_path = snap
                db_snapshots += 1
              else:
                # Not really a database after all, or unreadable. Fall back to a
                # raw copy, but put it back under the size cap: the exemption is
                # for databases, and a file that failed to open as one is not
                # entitled to smuggle 110 MB past the limit.
                db_fallbacks.append(rel_file_path)
                is_db = False
            if not is_db and size_threshold is not None and st.st_size > size_threshold:
              skipped_size.append(f"{rel_file_path} ({st.st_size / 1024 / 1024:.1f} MB)")
              continue

            try:
              tar.add(source_path, arcname=rel_file_path)
              added_files += 1
              added_bytes += source_path.stat().st_size
            except Exception:
              # Non-fatal; skip this file.
              continue
            finally:
              if source_path is not fpath:
                source_path.unlink(missing_ok=True)
            if progress and added_files % progress_interval == 0:
              print(
                f"... {added_files} files ({added_bytes / 1024 / 1024:.1f} MB) | "
                f"skipped excl {skipped_excluded} size {len(skipped_size)} | "
                f"svc {services_done}/{len(service_list)}",
                file=sys.stderr,
              )
        services_done += 1
  except KeyboardInterrupt:
    # User interrupted - clean up unless keeping partial (the finally below
    # removes the snapshot staging directory either way).
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
  finally:
    staging.cleanup()

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
    f"Files added: {added_files} | Skipped (exclude): {skipped_excluded} | "
    f"Skipped (size): {len(skipped_size)}",
    f"Databases snapshotted (WAL-consistent): {db_snapshots}",
    f"Added size (raw, uncompressed): {added_bytes / 1024 / 1024:.2f} MB",
  ]
  if skipped_size:
    # Named, not counted: a silent count is what hid three missing databases.
    msg_lines.append("Skipped for size (raise --max-file-size to include):")
    msg_lines.extend(f"  - {n}" for n in skipped_size[:20])
    if len(skipped_size) > 20:
      msg_lines.append(f"  ... and {len(skipped_size) - 20} more")
  if db_fallbacks:
    msg_lines.append("Databases copied RAW (snapshot failed - may be WAL-stale):")
    msg_lines.extend(f"  - {n}" for n in db_fallbacks[:20])
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
    default=None,
    help="Subset of services to include (default: every directory under "
    "CONFIG_DIRECTORY except the documented SKIP_SERVICES)",
  )
  p.add_argument(
    "--plan",
    action="store_true",
    help="Print the services and excludes that WOULD be archived, then exit without writing",
  )
  p.add_argument(
    "--no-db-snapshot",
    dest="no_db_snapshot",
    action="store_true",
    help="Copy SQLite databases raw instead of through the online backup API (not recommended: "
    "a raw copy of a WAL database reads back stale)",
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

  services = args.services if args.services else discover_services(config_root)
  explicit = bool(args.services)

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

  if args.plan:
    print(f"CONFIG_DIRECTORY: {config_root}")
    print(f"Archive destination: {backup_dir}")
    print(f"Services ({len(services)}): {', '.join(services)}")
    if not explicit:
      print("Skipped by name:")
      for name, why in sorted(SKIP_SERVICES.items()):
        marker = "" if (config_root / name).exists() else "  (not present)"
        print(f"  - {name}: {why}{marker}")
    print(f"Exclude patterns ({len(patterns)}):")
    for pat in patterns:
      print(f"  {pat}")
    print(f"Max file size: {'none' if max_file_size is None else f'{max_file_size} MB'} "
          "(never applied to a SQLite database)")
    return 0

  code, msg = create_backup(
    config_root,
    backup_dir,
    services,
    exclude_patterns=patterns,
    max_file_size_mb=max_file_size,
    progress=auto_progress,
    progress_interval=args.progress_interval,
    keep_partial=args.keep_partial,
    do_checksum=not args.no_checksum,
    snapshot_databases=not args.no_db_snapshot,
  )
  print(("✅" if code == 0 else ("⚠️" if code == 1 else "❌")) + " " + msg)

  if not args.no_prune:
    deleted = prune_archives(backup_dir, args.retain)
    if deleted:
      print(f"🧹 Pruned {len(deleted)} old archive(s)")
  return code


if __name__ == "__main__":
  sys.exit(main())
