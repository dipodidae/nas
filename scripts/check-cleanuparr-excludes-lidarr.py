#!/usr/bin/env python3
"""Assert Cleanuparr cannot touch Lidarr, and that Seeker stays off.

Why this exists
---------------
Cleanuparr is a deletion engine. ``docs/cleanuparr-configuration.md`` and
``CLAUDE.md`` both say Lidarr must never be enabled in any of its modules, and
that Seeker is off on purpose. Neither fact is expressible in the compose model:
Cleanuparr keeps its whole configuration in ``cleanuparr.db``, a gitignored
SQLite file, so ``make check`` is structurally blind to it and a UI click or a
config restore can undo it silently.

On 2026-09-17 both had drifted back on, and the pair formed a closed loop:

1. Queue Cleaner struck every Lidarr ``FailedImport`` row every 5 minutes and
   deleted it at 3 strikes -- about 15 minutes end to end.
2. Seeker then fired a replacement album search, Lidarr grabbed the same album
   from the next peer, and the new copy failed the same way.

Measured over 48h: 245 grabs against 79 imports, 25 albums grabbed 3+ times,
one album (Black Cilice, *Votive Fire*) grabbed 10 times and imported never.
It also made ``lidarr_queue_unstick.py`` structurally unable to work -- it runs
hourly behind a 1h age gate, so a row deleted in 15 minutes can never be seen,
and the job logged "nothing eligible" every hour for a week while the queue
churned.

``failed_import_skip_if_not_found_in_client`` being ``1`` is NOT sufficient
protection: it only skips rows with no content id, and Tubifarry supplies one
for most slskd downloads. Disabling the Lidarr *instance* is the control that
actually holds.

Exit codes
----------
  0 Lidarr is disabled (or absent) in Cleanuparr and Seeker search is off
  1 drift: Lidarr is enabled, or Seeker search is on
  2 fatal (config missing, DB unreadable)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

DB_NAME = "cleanuparr.db"


def _config_dir() -> Path:
  raw = os.environ.get("CONFIG_DIRECTORY")
  if not raw:
    print("ERROR: CONFIG_DIRECTORY not set (check .env)", file=sys.stderr)
    raise SystemExit(2)
  return Path(raw)


def _snapshot(db: Path) -> Path:
  """Copy the DB and its WAL sidecars into a temp dir before reading.

  Cleanuparr holds the database open in WAL mode, so reading ``.db`` alone
  returns values as of the last checkpoint -- the same trap that made a
  just-saved ``1`` read back as ``0`` when editing the *arr notification rows.
  """
  tmp = Path(tempfile.mkdtemp(prefix="cleanuparr-check-"))
  dest = tmp / DB_NAME
  dest.write_bytes(db.read_bytes())
  for suffix in ("-wal", "-shm"):
    side = db.with_name(db.name + suffix)
    if side.exists():
      dest.with_name(dest.name + suffix).write_bytes(side.read_bytes())
  return dest


def read_state(db_path: Path) -> tuple[list[tuple[str, int]], list[int]]:
  """Return ([(instance name, enabled)], [seeker search_enabled flags])."""
  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  try:
    instances = [
      (str(name), int(enabled))
      for name, enabled in conn.execute("select name, enabled from arr_instances")
    ]
    seeker = [
      int(v) for (v,) in conn.execute("select search_enabled from seeker_configs")
    ]
    return instances, seeker
  finally:
    conn.close()


def evaluate(
  instances: list[tuple[str, int]], seeker: list[int]
) -> tuple[bool, list[str]]:
  """Pure: (ok, failure messages). Lidarr must be off; Seeker must be off."""
  problems: list[str] = []
  problems.extend(
    f"arr instance {name!r} is ENABLED -- Cleanuparr can strike and delete "
    "Lidarr queue rows, whose only download client is slskd"
    for name, enabled in instances
    if name.strip().lower() == "lidarr" and enabled
  )
  if any(seeker):
    problems.append(
      "Seeker search_enabled is ON -- it fires replacement searches that "
      "re-grab the album Queue Cleaner just removed"
    )
  return (not problems), problems


def main(argv: list[str] | None = None) -> int:
  db = _config_dir() / "cleanuparr" / DB_NAME
  if not db.exists():
    print(f"ERROR: {db} not found", file=sys.stderr)
    return 2
  try:
    snapshot = _snapshot(db)
    instances, seeker = read_state(snapshot)
  except (OSError, sqlite3.Error) as exc:
    print(f"ERROR: cannot read {db}: {exc}", file=sys.stderr)
    return 2

  ok, problems = evaluate(instances, seeker)
  shown = ", ".join(f"{n}={'on' if e else 'off'}" for n, e in instances) or "(none)"
  if ok:
    print(f"    ok: Lidarr excluded, Seeker off [{shown}]")
    return 0
  for p in problems:
    print(f"    !!! {p}", file=sys.stderr)
  print(f"    instances: {shown}", file=sys.stderr)
  return 1


if __name__ == "__main__":
  sys.exit(main())
