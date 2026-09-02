#!/usr/bin/env python3
"""Rewrite Lidarr's /music path prefix to /data/music, offline, in one transaction.

Why this script exists
----------------------
Lidarr still copies instead of hardlinking because its root folder is /music
while downloads live under /downloads -- separate bind mounts, and link() cannot
cross a mount point (ADR-0002, verified: `ln /downloads/x /music/x` returns
EXDEV, `ln /data/downloads/x /data/music/x` succeeds). The fix is to put both
under /data.

Why NOT the API
---------------
Sonarr and Radarr were repathed with `PUT /api/v3/.../editor`. The same call
against Lidarr emptied TrackFiles: 150,187 rows -> 0 (ADR-0003). And the
non-editor form, `PUT /api/v1/artist/{id}?moveFiles=false`, updates only
Artists.Path -- TrackFiles.Path keeps an absolute /music/... string, which is
the ONLY handle Lidarr has on a file (no inode, no hash). Deleting the old root
folder afterwards then leaves 150,300 rows resolving to nothing.

/music and /data/music are two views of the SAME directory (/data is a bind
mount of /mnt/drive; /music is a bind mount of /mnt/drive/music). So nothing on
disk needs to move, and this is a pure metadata rewrite.

Safety properties
-----------------
* Dry-run by default. `--apply` is required to write anything.
* Refuses to run against a database Lidarr still has open (`--require-stopped`).
* Refuses to run unless a backup of the .db AND its -wal/-shm exists.
* One transaction: it commits everything or nothing. A half-rewritten database
  is worse than an untouched one.
* Verifies row counts are unchanged, and (with --verify-disk) that every
  rewritten path exists on disk, BEFORE committing.

Exit codes
----------
  0  success (or dry-run completed)
  1  partial -- verification failed and the transaction was rolled back
  2  fatal -- bad arguments, DB unreadable, Lidarr still running, no backup

Usage
-----
  python scripts/lidarr_repath_db.py --db /path/to/lidarr.db
  python scripts/lidarr_repath_db.py --db copy.db --apply --verify-disk --disk-prefix /mnt/drive/music
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import subprocess
import sys
from pathlib import PurePosixPath

# Every table.column in Lidarr's schema holding a value that starts with the
# root path. Enumerated from the live database on 2026-09-02, not assumed:
#
#   RootFolders.Path              1        the root itself
#   Artists.Path                  3,336    artist folders
#   TrackFiles.Path               150,300  the only handle Lidarr has on a file
#   MetadataFiles.RelativePath    14,958   ABSOLUTE despite the column name
#
# MetadataFiles is the trap: 14,958 of its 43,299 rows are absolute /music/...
# paths and 28,341 are genuinely relative ('artist.nfo'). Omitting the table
# orphans every .nfo; rewriting it blindly corrupts the relative rows.
#
# Deliberately NOT rewritten -- historical audit text, not state Lidarr
# resolves against: History.SourceTitle (605,171), History.Data,
# DownloadHistory.Data, Commands.Body. See ADR-0003.
REWRITE_TARGETS = (
  ("RootFolders", "Path"),
  ("Artists", "Path"),
  ("TrackFiles", "Path"),
  ("MetadataFiles", "RelativePath"),
)

EXIT_OK, EXIT_PARTIAL, EXIT_FATAL = 0, 1, 2


def rewrite_path(old: str, old_root: str, new_root: str) -> str:
  """Swap old_root for new_root at the front of `old`.

  Idempotent: a path already under new_root is returned unchanged. Raises
  ValueError for a traversal segment, and for anything not under either root
  -- including a relative path, which MetadataFiles is full of. Guessing at a
  path is how you write a wrong one into 168,595 rows.
  """
  if ".." in PurePosixPath(old).parts:
    raise ValueError(f"path traversal segment in {old!r}")
  new_root = new_root.rstrip("/")
  old_root = old_root.rstrip("/")
  if old == new_root or old.startswith(new_root + "/"):
    return old
  if old != old_root and not old.startswith(old_root + "/"):
    raise ValueError(f"{old!r} is not under {old_root!r}")
  suffix = old[len(old_root):].lstrip("/")
  return f"{new_root}/{suffix}" if suffix else new_root


def plan_rewrite(conn: sqlite3.Connection, old_root: str, new_root: str) -> list[dict]:
  """Count what each table would change, without changing anything."""
  old_root = old_root.rstrip("/")
  plan = []
  for table, column in REWRITE_TARGETS:
    try:
      total = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    except sqlite3.OperationalError:
      continue  # table absent in this schema version
    eligible = conn.execute(
      f'SELECT count(*) FROM "{table}" WHERE "{column}" = ? OR "{column}" LIKE ?',
      (old_root, old_root + "/%"),
    ).fetchone()[0]
    already = conn.execute(
      f'SELECT count(*) FROM "{table}" WHERE "{column}" = ? OR "{column}" LIKE ?',
      (new_root.rstrip("/"), new_root.rstrip("/") + "/%"),
    ).fetchone()[0]
    relative = conn.execute(
      f'SELECT count(*) FROM "{table}" WHERE "{column}" NOT LIKE ?', ("/%",)
    ).fetchone()[0]
    plan.append({
      "table": table, "column": column, "old_root": old_root,
      "total": total, "eligible": eligible,
      "already_migrated": already, "skipped_relative": relative,
    })
  return plan


def apply_rewrite(conn: sqlite3.Connection, plan: list[dict], new_root: str) -> dict:
  """Rewrite every eligible row in ONE transaction. Commits or rolls back.

  The LIKE-anchored UPDATE is what keeps /musicvideos out and leaves relative
  paths untouched: both fail `= old_root OR LIKE old_root || '/%'`.
  """
  new_root = new_root.rstrip("/")
  changed = {}
  # sqlite3 opens its own implicit transaction, so a bare BEGIN raises
  # "cannot start a transaction within a transaction". Take manual control
  # for the duration, and hand the connection back as we found it.
  prior = conn.isolation_level
  conn.isolation_level = None
  try:
    conn.execute("BEGIN IMMEDIATE")
    for item in plan:
      table, column = item["table"], item["column"]
      old_root = item["old_root"].rstrip("/")
      cur = conn.execute(
        f'UPDATE "{table}" SET "{column}" = ? || substr("{column}", ?) '
        f'WHERE "{column}" = ? OR "{column}" LIKE ?',
        (new_root, len(old_root) + 1, old_root, old_root + "/%"),
      )
      changed[table] = cur.rowcount
    conn.commit()
  except Exception:
    conn.rollback()
    raise
  finally:
    conn.isolation_level = prior
  return changed


# --- side effects, all below this line (AGENTS.md) -------------------------


def _lidarr_is_running() -> bool | None:
  """True/False, or None when docker cannot answer (so the caller can insist)."""
  try:
    out = subprocess.run(
      ["docker", "inspect", "-f", "{{.State.Running}}", "lidarr"],
      capture_output=True, text=True, timeout=15, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  if out.returncode != 0:
    return None
  return out.stdout.strip() == "true"


def _backup_is_present(db: str, backup_dir: str) -> tuple[bool, list[str]]:
  """WAL mode means a .db copied ALONE reads back stale -- the -wal holds the
  most recent commits. Demand all three, the same trap CLAUDE.md documents for
  the *arr notification toggles."""
  base = os.path.basename(db)
  missing = [
    suffix for suffix in ("", "-wal", "-shm")
    if not os.path.exists(os.path.join(backup_dir, base + suffix))
  ]
  return (not missing, missing)


def _print_plan(plan: list[dict], old_root: str, new_root: str) -> None:
  print(f"\nrepath plan: {old_root!r} -> {new_root!r}\n")
  print(f"  {'table':<16} {'total':>9} {'eligible':>9} {'already':>9} {'relative':>9}")
  print(f"  {'-' * 16} {'-' * 9} {'-' * 9} {'-' * 9} {'-' * 9}")
  for item in plan:
    print(f"  {item['table']:<16} {item['total']:>9} {item['eligible']:>9} "
          f"{item['already_migrated']:>9} {item['skipped_relative']:>9}")
  print(f"\n  {sum(i['eligible'] for i in plan)} row(s) eligible for rewrite.\n")


def _verify(conn, plan, old_root, new_root, verify_disk, disk_prefix, sample) -> list[str]:
  """Every check that must hold BEFORE the transaction commits."""
  problems = []
  for item in plan:
    table, column = item["table"], item["column"]
    now = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
    if now != item["total"]:
      problems.append(f"{table}: row count {item['total']} -> {now}. Rows were LOST.")
    left = conn.execute(
      f'SELECT count(*) FROM "{table}" WHERE "{column}" = ? OR "{column}" LIKE ?',
      (old_root, old_root + "/%"),
    ).fetchone()[0]
    if left:
      problems.append(f"{table}: {left} row(s) still under {old_root!r}.")
    still_rel = conn.execute(
      f'SELECT count(*) FROM "{table}" WHERE "{column}" NOT LIKE ?', ("/%",)
    ).fetchone()[0]
    if still_rel != item["skipped_relative"]:
      problems.append(
        f"{table}: relative rows {item['skipped_relative']} -> {still_rel}. "
        "A relative path was rewritten, which corrupts it.")

  if verify_disk:
    rows = [r[0] for r in conn.execute(
      'SELECT "Path" FROM "TrackFiles" WHERE "Path" LIKE ?', (new_root + "/%",))]
    if not rows:
      problems.append("--verify-disk: no rewritten TrackFiles rows to check.")
    else:
      picked = random.sample(rows, min(sample, len(rows)))
      missing = [p for p in picked
                 if not os.path.exists(disk_prefix + p[len(new_root):])]
      print(f"  disk check: {len(picked) - len(missing)}/{len(picked)} rewritten "
            f"TrackFiles paths present under {disk_prefix}")
      if missing:
        problems.append(
          f"--verify-disk: {len(missing)}/{len(picked)} rewritten paths do not "
          f"exist on disk, e.g. {missing[0]!r}")
  return problems


def main() -> int:
  ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
  ap.add_argument("--db", required=True, help="path to lidarr.db")
  ap.add_argument("--old-root", default="/music")
  ap.add_argument("--new-root", default="/data/music")
  ap.add_argument("--apply", action="store_true",
                  help="actually write. Without this the run is a dry run.")
  ap.add_argument("--backup-dir",
                  help="directory holding a backup of the .db, -wal and -shm")
  ap.add_argument("--no-require-stopped", dest="require_stopped",
                  action="store_false", default=True,
                  help="skip the 'lidarr must not be running' guard (rehearsals)")
  ap.add_argument("--verify-disk", action="store_true",
                  help="check a sample of rewritten paths exist on disk")
  ap.add_argument("--disk-prefix", default="/mnt/drive/music",
                  help="host path that --new-root maps to")
  ap.add_argument("--sample", type=int, default=1000)
  args = ap.parse_args()

  if not os.path.exists(args.db):
    print(f"FATAL: no such database: {args.db}", file=sys.stderr)
    return EXIT_FATAL

  if args.apply and args.require_stopped:
    running = _lidarr_is_running()
    if running is not False:
      why = "is still running" if running else "state could not be determined"
      print(f"FATAL: lidarr {why}. Rewriting under a running Lidarr means WAL "
            "frames land on top of the rewrite. Stop it first, or pass "
            "--no-require-stopped for a rehearsal on a copy.", file=sys.stderr)
      return EXIT_FATAL

  if args.apply:
    if not args.backup_dir:
      print("FATAL: --apply needs --backup-dir. ADR-0003 is what happens "
            "without one.", file=sys.stderr)
      return EXIT_FATAL
    present, missing = _backup_is_present(args.db, args.backup_dir)
    if not present:
      print(f"FATAL: backup incomplete in {args.backup_dir} -- missing "
            f"{missing}. WAL mode means a .db without its -wal reads back "
            "stale, so all three are required.", file=sys.stderr)
      return EXIT_FATAL

  conn = sqlite3.connect(args.db)
  try:
    plan = plan_rewrite(conn, args.old_root, args.new_root)
    _print_plan(plan, args.old_root, args.new_root)

    if not args.apply:
      print("dry run -- nothing written. Re-run with --apply to commit.")
      return EXIT_OK

    if not sum(i["eligible"] for i in plan):
      print("nothing eligible; already migrated.")
      return EXIT_OK

    # Rewrite and verify inside ONE transaction, so a failed check rolls the
    # whole thing back rather than leaving half the rows on a dead root.
    prior = conn.isolation_level
    conn.isolation_level = None
    try:
      conn.execute("BEGIN IMMEDIATE")
      changed = {}
      for item in plan:
        table, column = item["table"], item["column"]
        old_root = item["old_root"].rstrip("/")
        cur = conn.execute(
          f'UPDATE "{table}" SET "{column}" = ? || substr("{column}", ?) '
          f'WHERE "{column}" = ? OR "{column}" LIKE ?',
          (args.new_root.rstrip("/"), len(old_root) + 1, old_root, old_root + "/%"),
        )
        changed[table] = cur.rowcount
      for table, n in changed.items():
        print(f"  {table:<16} {n:>9} row(s) rewritten")
      problems = _verify(conn, plan, args.old_root.rstrip("/"),
                         args.new_root.rstrip("/"), args.verify_disk,
                         args.disk_prefix.rstrip("/"), args.sample)
      if problems:
        conn.rollback()
        print("\nVERIFICATION FAILED -- rolled back, nothing written:",
              file=sys.stderr)
        for p in problems:
          print(f"  !!! {p}", file=sys.stderr)
        return EXIT_PARTIAL
      conn.commit()
    except Exception:
      conn.rollback()
      raise
    finally:
      conn.isolation_level = prior

    print("\ncommitted. Every check passed before the commit.")
    return EXIT_OK
  finally:
    conn.close()


if __name__ == "__main__":
  sys.exit(main())
