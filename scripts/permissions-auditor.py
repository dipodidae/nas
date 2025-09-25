#!/usr/bin/env python3
"""Permissions & ownership auditor (with optional fixer).

Scans configured directory trees and reports files/dirs that do not match
expected ownership (PUID/PGID) or have permissive modes.

Environment variables:
  PUID / PGID           Expected numeric owner/group (fallback to current process ids)
  CONFIG_DIRECTORY      Will be scanned if it exists
  SHARE_DIRECTORY       Will be scanned if provided AND --include-share specified

Typical usage:
  python scripts/permissions-auditor.py                # report only
  python scripts/permissions-auditor.py --fix           # attempt to correct issues
  python scripts/permissions-auditor.py --paths /data/custom /other --max 2000

Exit codes:
  0 no issues (or all fixed)
  1 issues found (report mode) or partially fixed
  2 fatal error / invalid arguments
"""

from __future__ import annotations

import argparse
import os
import stat as statmod
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# Best-effort .env autoload so running via npm scripts (which don't source .env)
# still picks up CONFIG_DIRECTORY / SHARE_DIRECTORY / PUID / PGID.
if "CONFIG_DIRECTORY" not in os.environ or "SHARE_DIRECTORY" not in os.environ:
  try:  # pragma: no cover - optional convenience
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except Exception:  # noqa: E722
    pass


@dataclass
class Issue:
  path: Path
  kind: str  # owner|group|mode
  detail: str


DEFAULT_DIR_MODE = 0o755
DEFAULT_FILE_MODE = 0o644

EXCLUDE_DIR_NAMES = {".venv", "__pycache__", "node_modules", "dist", "cache"}


def walk_paths(root: Path, max_items: int, max_depth: int) -> Iterator[Path]:
  """Yield paths under root honoring max_items and max_depth using os.walk for efficiency."""
  count = 0
  root_depth = len(root.parts)
  for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    current_depth = len(Path(dirpath).parts) - root_depth
    if max_depth >= 0 and current_depth > max_depth:
      continue
    # Prune excluded directories in-place
    dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIR_NAMES]
    # Yield directory itself (skip root already accounted for)
    if current_depth > 0:
      if count >= max_items:
        break
      p_dir = Path(dirpath)
      if any(part in EXCLUDE_DIR_NAMES for part in p_dir.parts):
        continue
      count += 1
      yield p_dir
    for fname in filenames:
      if count >= max_items:
        break
      p_file = Path(dirpath) / fname
      if any(part in EXCLUDE_DIR_NAMES for part in p_file.parts):
        continue
      count += 1
      yield p_file
    if count >= max_items:
      break


def audit(
  paths: list[Path],
  puid: int,
  pgid: int,
  max_items: int,
  strict: bool,
  max_depth: int,
  fast_fail: bool,
) -> list[Issue]:
  issues: list[Issue] = []
  for root in paths:
    if not root.exists():
      continue
    for p in walk_paths(root, max_items, max_depth):
      try:
        st = p.lstat()
      except FileNotFoundError:
        continue
      # Ownership
      if st.st_uid != puid:
        issues.append(Issue(p, "owner", f"uid {st.st_uid} != {puid}"))
        if fast_fail and len(issues) > 100:
          return issues
      if st.st_gid != pgid:
        issues.append(Issue(p, "group", f"gid {st.st_gid} != {pgid}"))
        if fast_fail and len(issues) > 100:
          return issues
      # Mode check
      mode = statmod.S_IMODE(st.st_mode)
      desired = DEFAULT_DIR_MODE if p.is_dir() else DEFAULT_FILE_MODE
      if strict:
        if mode != desired:
          issues.append(Issue(p, "mode", f"{oct(mode)} != {oct(desired)}"))
          if fast_fail and len(issues) > 100:
            return issues
      else:
        # Loose: only flag world-writable
        if mode & 0o002:
          issues.append(Issue(p, "mode", f"world-writable {oct(mode)}"))
          if fast_fail and len(issues) > 100:
            return issues
  return issues


def fix_issues(issues: list[Issue], puid: int, pgid: int, dry_run: bool) -> tuple[int, int]:
  fixed = 0
  failed = 0
  seen = set()
  for issue in issues:
    if issue.path in seen:
      continue
    seen.add(issue.path)
    try:
      if issue.kind in {"owner", "group"}:
        if not dry_run:
          os.chown(issue.path, puid, pgid)
        fixed += 1
      elif issue.kind == "mode":
        if not dry_run:
          if issue.path.is_dir():
            os.chmod(issue.path, DEFAULT_DIR_MODE)
          else:
            os.chmod(issue.path, DEFAULT_FILE_MODE)
        fixed += 1
    except PermissionError:
      failed += 1
  return fixed, failed


def parse_args() -> argparse.Namespace:
  p = argparse.ArgumentParser(description="Audit and optionally fix file permissions/ownership")
  p.add_argument("--paths", nargs="*", default=[], help="Additional explicit root paths to scan")
  p.add_argument(
    "--max", type=int, default=10_000, help="Max filesystem entries to examine (default 10k)"
  )
  p.add_argument(
    "--max-depth",
    type=int,
    default=4,
    help="Maximum directory depth to recurse (-1 for unlimited, default 4)",
  )
  p.add_argument("--fix", action="store_true", help="Attempt to correct issues")
  p.add_argument(
    "--dry-run", action="store_true", help="Show what would be fixed without changing anything"
  )
  p.add_argument(
    "--strict",
    action="store_true",
    help="Enforce exact mode (644/755) instead of only flagging world-writable",
  )
  p.add_argument("--no-share", action="store_true", help="Exclude SHARE_DIRECTORY even if detected")
  p.add_argument("--summary", action="store_true", help="Only print counts (quieter output)")
  return p.parse_args()


def deduce_roots(args) -> tuple[list[Path], list[str]]:
  """Determine which roots to scan based on args + environment + compose file.

  Returns (roots, notes) where notes are explanatory messages.
  """
  notes: list[str] = []
  roots: list[Path] = []

  # 1. User-specified paths take precedence
  for p in args.paths:
    path_obj = Path(p).resolve()
    if path_obj.exists():
      roots.append(path_obj)
    else:
      notes.append(f"Skipping non-existent user path: {path_obj}")

  # 2. CONFIG_DIRECTORY (entire tree) for broad coverage
  cfg = os.getenv("CONFIG_DIRECTORY")
  if cfg:
    cfg_path = Path(cfg).resolve()
    if cfg_path.exists() and cfg_path not in roots:
      roots.append(cfg_path)
      notes.append(f"Detected CONFIG_DIRECTORY: {cfg_path}")
    elif not cfg_path.exists():
      notes.append(f"CONFIG_DIRECTORY set but missing: {cfg_path}")
  else:
    notes.append("CONFIG_DIRECTORY not set; consider adding to .env")

  # 3. SHARE_DIRECTORY (optional) included unless explicitly excluded
  share = os.getenv("SHARE_DIRECTORY")
  if share and not args.no_share:
    share_path = Path(share).resolve()
    if share_path.exists() and share_path not in roots:
      roots.append(share_path)
      notes.append(f"Detected SHARE_DIRECTORY: {share_path}")
    elif not share_path.exists():
      notes.append(f"SHARE_DIRECTORY set but missing: {share_path}")

  # 4. Fallback: parse docker-compose.yml for explicit mounted config subdirs if root not found
  if not roots:
    compose_file = Path("docker-compose.yml")
    if compose_file.exists():
      try:
        import re

        content = compose_file.read_text(encoding="utf-8")
        # Match lines like: - ${CONFIG_DIRECTORY}/sonarr:/config
        matches = re.findall(r"\$\{CONFIG_DIRECTORY}/([\w.-]+)/:*/config", content)
        # Deduce potential base directories (guess common ones)
        candidate_bases = [
          Path("/mnt/drive/.docker-config"),
          Path("/srv/docker/config"),
          Path("/data/docker-config"),
        ]
        for base in candidate_bases:
          if base.exists():
            for sub in matches[:25]:  # safety cap
              sub_path = base / sub
              if sub_path.exists():
                roots.append(sub_path)
            if roots:
              notes.append(f"Guessed config subdirs under {base}")
              break
      except Exception:  # pragma: no cover
        notes.append("Compose parsing failed (non-fatal)")

  return roots, notes


def main() -> int:
  args = parse_args()
  puid = int(os.getenv("PUID", str(os.getuid())))
  pgid = int(os.getenv("PGID", str(os.getgid())))

  roots, notes = deduce_roots(args)
  if not roots:
    print("❌ No valid paths to scan after deduction.")
    for n in notes:
      print(f" - {n}")
    print("Provide explicit paths: permissions-auditor.py --paths /path/one /path/two")
    return 2

  print("Scanning roots:")
  for r in roots:
    print(f" • {r}")
  for n in notes:
    print(f"   note: {n}")

  fast_fail = args.summary  # when only summary desired, we can exit early after enough issues
  issues = audit(roots, puid, pgid, args.max, args.strict, args.max_depth, fast_fail)
  if not issues:
    print("✅ No issues detected")
    return 0

  if args.summary:
    owners = sum(1 for i in issues if i.kind == "owner")
    groups = sum(1 for i in issues if i.kind == "group")
    modes = sum(1 for i in issues if i.kind == "mode")
    print(f"Issues: total={len(issues)} owner={owners} group={groups} mode={modes}")
  else:
    print(f"Found {len(issues)} issue(s):")
    for issue in issues[:50]:  # cap output
      print(f" - [{issue.kind}] {issue.path} :: {issue.detail}")
    if len(issues) > 50:
      print(f" ... ({len(issues) - 50} more omitted)")

  if args.fix:
    fixed, failed = fix_issues(issues, puid, pgid, args.dry_run)
    action = "(dry-run) would fix" if args.dry_run else "fixed"
    print(f"Attempted fixes: {action} {fixed}, failed {failed}")
    return 0 if failed == 0 else 1
  return 1


if __name__ == "__main__":
  sys.exit(main())
