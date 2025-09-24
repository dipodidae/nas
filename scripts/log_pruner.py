#!/usr/bin/env python3
"""Log size pruner / compressor.

Scans one or more root paths (default: CONFIG_DIRECTORY) for *.log files that:
  • exceed a size threshold (LOG_PRUNE_MAX_MB, default 25 MB)
  • are older than a minimum age (LOG_PRUNE_MIN_AGE_DAYS, default 1)

Actions:
  • If LOG_PRUNE_COMPRESS=true (default) compress oversize logs to log.<ts>.gz and truncate original
  • Otherwise simply truncate (copy tail marker)

Binary dependencies: gzip (for compression). Falls back to truncate only if missing.

Usage:
  python scripts/log_pruner.py                # prune using env defaults
  python scripts/log_pruner.py --roots pathA pathB --max-mb 10 --min-age 0

Exit codes:
  0 operation successful
  1 non-fatal issues (e.g., some files could not be processed)
  2 fatal (invalid arguments / no roots)
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

# Attempt to load .env automatically so running via package.json (which does not
# source the .env file) still works if python-dotenv is available. This keeps
# behaviour non-fatal if the dependency were ever removed.
if "CONFIG_DIRECTORY" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except Exception:  # pragma: no cover - best effort only
    pass


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(description="Prune & optionally compress large .log files")
  p.add_argument(
    "--roots", nargs="*", default=[], help="Root directories to scan (default: CONFIG_DIRECTORY)"
  )
  p.add_argument(
    "--max-mb",
    type=int,
    default=int(os.getenv("LOG_PRUNE_MAX_MB", "25")),
    help="Size threshold in MB",
  )
  p.add_argument(
    "--min-age",
    type=int,
    default=int(os.getenv("LOG_PRUNE_MIN_AGE_DAYS", "1")),
    help="Minimum age in days before pruning",
  )
  p.add_argument(
    "--no-compress", action="store_true", help="Disable compression even if enabled via env"
  )
  p.add_argument("--dry-run", action="store_true", help="Show actions without changing files")
  return p.parse_args()


def gather_roots(args) -> list[Path]:
  roots: list[Path] = []
  if not args.roots:
    if cfg := os.getenv("CONFIG_DIRECTORY"):
      roots.append(Path(cfg))
  else:
    roots.extend(Path(r) for r in args.roots)
  return [r for r in roots if r.exists()]


def should_process(path: Path, max_size: int, min_age_days: int, now: float) -> bool:
  try:
    st = path.stat()
  except FileNotFoundError:
    return False
  if st.st_size < max_size:
    return False
  age_days = (now - st.st_mtime) / 86400
  return age_days >= min_age_days


def compress_and_truncate(path: Path, dry_run: bool) -> tuple[bool, str]:
  ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
  backup = path.with_suffix(path.suffix + f".{ts}.gz")
  if shutil.which("gzip") is None:
    return truncate(path, dry_run)
  try:
    if not dry_run:
      # Copy then compress
      import gzip

      with path.open("rb") as src, gzip.open(backup, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst)
      # Truncate original
      with path.open("w", encoding="utf-8") as f:
        f.write(f"[pruned @ {ts} archived -> {backup.name}]\n")
    return True, f"compressed-> {backup.name}"
  except Exception as e:  # noqa
    return False, f"compress failed: {e}"


def truncate(path: Path, dry_run: bool) -> tuple[bool, str]:
  ts = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
  try:
    if not dry_run:
      with path.open("w", encoding="utf-8") as f:
        f.write(f"[truncated @ {ts}]\n")
    return True, "truncated"
  except Exception as e:  # noqa
    return False, f"truncate failed: {e}"


def main() -> int:
  args = parse_args()
  compress_env = os.getenv("LOG_PRUNE_COMPRESS", "true").lower() == "true"
  do_compress = compress_env and not args.no_compress
  roots = gather_roots(args)
  if not roots:
    print("❌ No valid roots to scan. Set CONFIG_DIRECTORY in your .env or pass --roots <path>.")
    print("   Examples:")
    print("     pnpm run logs:prune -- --roots /path/to/config")
    print("     CONFIG_DIRECTORY=/path/to/config pnpm run logs:prune")
    return 2
  max_size_bytes = args.max_mb * 1024 * 1024
  now = time.time()
  processed = 0
  failures = 0
  for root in roots:
    for path in root.rglob("*.log"):
      if should_process(path, max_size_bytes, args.min_age, now):
        if do_compress:
          ok, action = compress_and_truncate(path, args.dry_run)
        else:
          ok, action = truncate(path, args.dry_run)
        processed += 1
        status = "✅" if ok else "❌"
        print(f"{status} {path} -> {action}{' (dry-run)' if args.dry_run else ''}")
        if not ok:
          failures += 1
  print(f"Summary: processed {processed} file(s); failures={failures}")
  if failures:
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
