#!/usr/bin/env python3
"""Assert the newest config archive actually contains the databases. Runtime check.

Why this exists
---------------
A backup that runs, exits 0 and writes a 437 MB file is not evidence of a
backup. On 2026-09-13 the nightly archive had for months contained eight of the
twenty-eight service directories and **none** of the large databases: ``--fast``
sets a 25 MB per-file cap, which silently dropped ``sonarr.db`` (41 MB),
``prowlarr.db`` (32 MB) and Jellyfin's ``jellyfin.db``, while the run reported
success and the summary said only "Skipped (size): 9". The archive held
Nextcloud's PHP tree, extracted subtitle streams and a yt-dlp temp file.

So the assertion has to be about the archive's *contents*, not the job's exit
code -- the verifying-by-effect rule. This opens the newest archive and checks
that every member of ``REQUIRED`` is present and non-empty. It is deliberately
a runtime check (``make verify-runtime``, not ``make check``): the compose model
and the script can both be correct while the archive on disk is not.

Restoring: ``python scripts/config_backup.py --restore <name>``.

Exit codes
----------
  0  the newest archive is fresh and holds every required member
  1  a required member is missing/empty, or the newest archive is too old
  2  no archive directory, no archive in it, or the archive cannot be read
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import tarfile
import time
from pathlib import Path

DEFAULT_BACKUP_DIR = "/mnt/drive/backups/nas-configs"
DEFAULT_MAX_AGE_H = 36.0

# Glob -> why it has to be in there. Every one of these is state that cannot be
# rebuilt from this repo, and all but two are SQLite databases in WAL mode.
REQUIRED: dict[str, str] = {
  "sonarr/sonarr.db": "series, quality profiles, the MediaBrowser connection and its delete toggles",
  "radarr/radarr.db": "movies, quality profiles, the MediaBrowser connection and its delete toggles",
  "lidarr/lidarr.db": "the entire music pipeline: artists, MBIDs, indexer 4's fallback flags (ADR-0003)",
  "prowlarr/prowlarr.db": "indexer definitions, API keys and the byparr/cloudflare tagging",
  "bazarr/db/bazarr.db": "subtitle provider config and history",
  "jellyfin/data/data/jellyfin.db": "users, libraries, playstate, the ProviderIds that fix a mis-matched series",
  "tinyauth/tinyauth.db": "the one auth door's schema -- ADR-0036: its migration is one-way, so this IS the rollback",
  "cleanuparr/cleanuparr.db": "a deletion engine's rules, with dryRun false",
  "qui/*.db": "the qBittorrent UI's instance registration",
  "qbittorrent/qBittorrent/qBittorrent.conf": "the enforced prefs, incl. the upload cap that is not cosmetic",
  "slskd/slskd.yml": "the slskd pins verify-runtime asserts against",
  "swag/nginx/proxy-confs/*.subdomain.conf": "the public surface (ADR-0022)",
  "nextcloud/www/nextcloud/config/config.php": "instanceid, secret and the DB password -- Nextcloud does not come back without it",
}


def newest_archive(backup_dir: Path) -> Path | None:
  """Most recently modified configs-*.tar.gz in backup_dir, or None. Pure-ish."""
  archives = sorted(backup_dir.glob("configs-*.tar.gz"), key=lambda p: p.stat().st_mtime)
  return archives[-1] if archives else None


def missing_members(names: list[str], sizes: dict[str, int], required: dict[str, str]) -> list[str]:
  """Return a description for each required glob with no non-empty match. Pure."""
  gaps: list[str] = []
  for pattern, why in required.items():
    hits = [n for n in names if fnmatch.fnmatchcase(n, pattern)]
    if not hits:
      gaps.append(f"{pattern} -- {why}")
      continue
    if not any(sizes.get(n, 0) > 0 for n in hits):
      gaps.append(f"{pattern} -- present but 0 bytes -- {why}")
  return gaps


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__)
  ap.add_argument("--backup-dir", default=os.getenv("BACKUP_DIR") or DEFAULT_BACKUP_DIR)
  ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_H)
  return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  backup_dir = Path(args.backup_dir)
  if not backup_dir.is_dir():
    print(f"    !!! no backup directory at {backup_dir}", file=sys.stderr)
    return 2

  archive = newest_archive(backup_dir)
  if archive is None:
    print(f"    !!! no configs-*.tar.gz in {backup_dir}", file=sys.stderr)
    return 2

  age_h = (time.time() - archive.stat().st_mtime) / 3600
  try:
    with tarfile.open(archive, "r:gz") as tar:
      members = [m for m in tar.getmembers() if m.isfile()]
  except (OSError, tarfile.TarError) as exc:
    print(f"    !!! cannot read {archive.name}: {exc}", file=sys.stderr)
    return 2

  names = [m.name for m in members]
  sizes = {m.name: m.size for m in members}
  gaps = missing_members(names, sizes, REQUIRED)

  if age_h > args.max_age_hours:
    print(
      f"    !!! newest archive {archive.name} is {age_h:.1f}h old, allowed {args.max_age_hours:g}h",
      file=sys.stderr,
    )
    return 1
  if gaps:
    print(
      f"    !!! {archive.name} ({len(names)} files) is missing state that cannot be rebuilt:",
      file=sys.stderr,
    )
    for gap in gaps:
      print(f"        - {gap}", file=sys.stderr)
    return 1

  services = len({n.split("/", 1)[0] for n in names})
  print(
    f"    ok: {archive.name} is {age_h:.1f}h old, {len(names)} files across "
    f"{services} services, all {len(REQUIRED)} required members present"
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
