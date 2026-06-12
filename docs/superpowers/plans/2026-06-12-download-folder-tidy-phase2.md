# Download-folder tidy — Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable, triple-gated `scripts/slskd_incomplete_sweep.py` that deletes orphaned dirs from the slskd-owned zones of `incomplete/` (the 148 legacy flat dirs + future `incomplete/slskd/` orphans) without ever touching qBittorrent's temp, then clear the two remaining residue items (3 cruft qBit categories + the stale `/mnt/drive/.docker-config` tree).

**Architecture:** Single-file stdlib script mirroring `slskd_complete_sweep.py` / `slskd_cleanup.py` — pure planner `plan_incomplete_sweep` (unit-tested) + side effects (dir walk, API fetches, `rmtree`) in `main()`. Three safety gates: not-referenced-by-active-slskd-transfer, not-referenced-by-live-qBittorrent-torrent, and an age gate. The two residue ops are controller-run live actions in the final tasks.

**Tech Stack:** Python 3.11+ stdlib (`urllib`, `http.cookiejar`, `shutil`, `pathlib`, `datetime`, `argparse`), `python-dotenv` fallback, `pytest`, `ruff`.

**Spec:** `docs/superpowers/specs/2026-06-12-download-folder-tidy-phase2-design.md`

**Verified live facts (do not re-derive):**
- `INCOMPLETE_DIR` = `/mnt/drive/downloads/incomplete`; managed subdirs `qbittorrent` (qBit temp, NEVER touch) and `slskd` (current Soulseek temp). 148 legacy flat dirs at the root are orphans.
- slskd: `GET ${SLSKD_HOST}/api/v0/transfers/downloads`, header `X-API-Key`, `API_KEY_SLSKD`; `SLSKD_HOST` default `http://localhost:5030`.
- qBittorrent WebUI v2: `http://localhost:8080`, creds `QBITTORRENT_USER`/`QBITTORRENT_PASS`; **login returns HTTP 204** on success (v5.2.1) — accept 200 or 204.
- Cruft categories (0 torrents each): `movies-radarr`, `music-lidarr`, `tv-sonarr`.
- Stale tree `/mnt/drive/.docker-config` (2.5 GB, frozen 2026-05-23) — not mounted by any container; live tree is `/home/tom/nas/.docker-config`.
- Reuse the `_trailing_segment` and `_request` idioms verbatim from `scripts/slskd_cleanup.py`.

---

## File Structure

- Create: `scripts/slskd_incomplete_sweep.py` — the sweeper (module + `main()`).
- Create: `scripts/tests/test_slskd_incomplete_sweep.py` — pytest unit tests for the pure planner + helper.
- Modify: `scripts/README.md`, `AGENTS.md`, `package.json` — docs + pnpm wrappers.
- (No source file for the two residue ops — they are live commands in Tasks 8–9.)

---

## Task 1: Scaffold `slskd_incomplete_sweep.py`

**Files:**
- Create: `scripts/slskd_incomplete_sweep.py`

- [ ] **Step 1: Write the scaffold**

```python
#!/usr/bin/env python3
"""Sweep orphaned dirs from the slskd-owned zones of /downloads/incomplete.

Background
----------
slskd writes in-progress Soulseek downloads under an incomplete dir. After the
Phase 1 tidy, slskd uses /downloads/incomplete/slskd and qBittorrent uses
/downloads/incomplete/qbittorrent — but legacy orphan album folders remain at
the /downloads/incomplete root (from before the split), and new orphans can
accumulate under incomplete/slskd whenever a Soulseek transfer is cancelled or
dies mid-download. This script deletes those orphans safely.

It NEVER enters /downloads/incomplete/qbittorrent — qBittorrent owns that temp
dir and deleting it would corrupt live torrents.

A candidate dir is deleted only if it clears ALL three gates:
  1. not referenced by an active slskd transfer (by dir basename),
  2. not referenced by a live qBittorrent torrent (save_path/content_path/name
     basename) — qBittorrent historically shared /downloads/incomplete,
  3. its mtime is older than --min-age-hours (default 24).

If EITHER reference fetch fails, the sweep aborts (exit 2) rather than deleting
with an incomplete protection set.

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some rmtrees failed; details on stderr)
  2 fatal (config missing, slskd/qBittorrent unreachable, containment violation)

Environment
-----------
  API_KEY_SLSKD      (required) administrator key for slskd /api/v0
  SLSKD_HOST         (default: http://localhost:5030)
  QBITTORRENT_USER   (required) qBittorrent WebUI username
  QBITTORRENT_PASS   (required) qBittorrent WebUI password
  QBITTORRENT_HOST   (default: http://localhost:8080)
  INCOMPLETE_DIR     (default: /mnt/drive/downloads/incomplete)

Usage
-----
  python scripts/slskd_incomplete_sweep.py --dry-run
  python scripts/slskd_incomplete_sweep.py --min-age-hours 24 --limit 50
"""

from __future__ import annotations

import argparse
import datetime as _dt
import http.cookiejar
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

if "API_KEY_SLSKD" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_SLSKD_HOST = "http://localhost:5030"
DEFAULT_QBT_HOST = "http://localhost:8080"
DEFAULT_INCOMPLETE_DIR = "/mnt/drive/downloads/incomplete"
DEFAULT_MIN_AGE_HOURS = 24.0
MANAGED_SUBDIRS = frozenset({"qbittorrent", "slskd"})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Sweep orphaned dirs from the slskd-owned zones of /downloads/incomplete."
  )
  parser.add_argument(
    "--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
    help=f"Only delete dirs older than this (default {DEFAULT_MIN_AGE_HOURS}).",
  )
  parser.add_argument("--limit", type=int, default=0, help="Cap deletions per run (0 = unlimited).")
  parser.add_argument("--dry-run", action="store_true", help="Report the plan and exit 0.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  slskd_key = os.environ.get("API_KEY_SLSKD")
  qbt_host = os.environ.get("QBITTORRENT_HOST", DEFAULT_QBT_HOST).rstrip("/")
  qbt_user = os.environ.get("QBITTORRENT_USER")
  qbt_pass = os.environ.get("QBITTORRENT_PASS")
  incomplete_dir = Path(os.environ.get("INCOMPLETE_DIR", DEFAULT_INCOMPLETE_DIR))
  if not slskd_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2
  if not qbt_user or not qbt_pass:
    print("ERROR: QBITTORRENT_USER / QBITTORRENT_PASS not set (check .env)", file=sys.stderr)
    return 2
  # Wired in later tasks.
  return 0


if __name__ == "__main__":
  sys.exit(main())
```

- [ ] **Step 2: Verify import + lint**

Run: `. .venv/bin/activate && python -c "import importlib.util; s=importlib.util.spec_from_file_location('s','scripts/slskd_incomplete_sweep.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')" && ruff check scripts/slskd_incomplete_sweep.py`
Expected: prints `ok`, ruff `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/slskd_incomplete_sweep.py
git commit -m "feat(incomplete-sweep): scaffold slskd_incomplete_sweep"
```

---

## Task 2: `_trailing_segment` helper + test

**Files:**
- Modify: `scripts/slskd_incomplete_sweep.py`
- Create: `scripts/tests/test_slskd_incomplete_sweep.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_slskd_incomplete_sweep.py`:

```python
import datetime as _dt
import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "slskd_incomplete_sweep.py"
  spec = importlib.util.spec_from_file_location("slskd_incomplete_sweep", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


sweep = _load_module()


# ---- _trailing_segment ---------------------------------------------------


def test_trailing_segment_handles_separators():
  assert sweep._trailing_segment("music\\Artist\\Album") == "Album"
  assert sweep._trailing_segment("music/Artist/Album/") == "Album"
  assert sweep._trailing_segment("BareName") == "BareName"
  assert sweep._trailing_segment("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_incomplete_sweep.py -k trailing_segment -v`
Expected: FAIL with `AttributeError: ... has no attribute '_trailing_segment'`

- [ ] **Step 3: Add the helper** (after `parse_args`)

```python
def _trailing_segment(path: str) -> str:
  """Last path component, normalizing both `\\` and `/` separators."""
  if not path:
    return ""
  normalized = path.replace("/", "\\").rstrip("\\")
  if "\\" not in normalized:
    return normalized
  return normalized.rsplit("\\", 1)[-1]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_incomplete_sweep.py -k trailing_segment -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/slskd_incomplete_sweep.py scripts/tests/test_slskd_incomplete_sweep.py
git commit -m "feat(incomplete-sweep): _trailing_segment basename helper"
```

---

## Task 3: `plan_incomplete_sweep` — the triple-gated planner

**Files:**
- Modify: `scripts/slskd_incomplete_sweep.py`
- Modify: `scripts/tests/test_slskd_incomplete_sweep.py`

- [ ] **Step 1: Write the failing test** (append)

```python
# ---- plan_incomplete_sweep -----------------------------------------------

NOW = _dt.datetime(2026, 6, 12, 12, 0, 0)


def _cand(name, hours_old):
  mtime = (NOW - _dt.timedelta(hours=hours_old)).timestamp()
  return (Path(f"/mnt/drive/downloads/incomplete/{name}"), mtime)


def test_plan_skips_protected_and_recent_selects_orphans():
  candidates = [
    _cand("Old Orphan Album", 100),       # eligible
    _cand("Active Slskd Album", 100),      # protected by slskd ref
    _cand("Seeding Torrent Dir", 100),     # protected by qbt ref
    _cand("Fresh Download", 2),            # too recent (age gate)
  ]
  slskd_refs = {"Active Slskd Album"}
  qbt_refs = {"Seeding Torrent Dir"}
  out = sweep.plan_incomplete_sweep(
    candidates, slskd_refs, qbt_refs, now=NOW, min_age_hours=24
  )
  assert [p.name for p in out] == ["Old Orphan Album"]


def test_plan_empty_candidates():
  assert sweep.plan_incomplete_sweep([], set(), set(), now=NOW, min_age_hours=24) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_incomplete_sweep.py -k plan -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Add the planner** (after `_trailing_segment`)

```python
def plan_incomplete_sweep(
  candidates: list[tuple[Path, float]],
  slskd_refs: set[str],
  qbt_refs: set[str],
  *,
  now: _dt.datetime,
  min_age_hours: float,
) -> list[Path]:
  """Return candidate dirs to delete: orphaned by both ref sets AND old enough.

  Pure. ``candidates`` are (dir, mtime_epoch) pairs already restricted to the
  sweep zones by the caller. A dir is deleted only if its basename is in
  neither ``slskd_refs`` nor ``qbt_refs`` and its mtime is older than
  ``min_age_hours``. Order preserved.
  """
  cutoff = (now - _dt.timedelta(hours=min_age_hours)).timestamp()
  out: list[Path] = []
  for path, mtime in candidates:
    if path.name in slskd_refs or path.name in qbt_refs:
      continue
    if mtime >= cutoff:
      continue
    out.append(path)
  return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_incomplete_sweep.py -k plan -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/slskd_incomplete_sweep.py scripts/tests/test_slskd_incomplete_sweep.py
git commit -m "feat(incomplete-sweep): plan_incomplete_sweep triple-gate planner"
```

---

## Task 4: slskd + qBittorrent reference fetchers (side effects)

**Files:**
- Modify: `scripts/slskd_incomplete_sweep.py`

Thin HTTP wrappers (not unit-tested — verified live in Task 7).

- [ ] **Step 1: Add the fetchers** (after `plan_incomplete_sweep`)

```python
def _request(method: str, url: str, api_key: str, *, timeout: int = 15) -> tuple[int, bytes]:
  req = urllib.request.Request(url, method=method, headers={"X-API-Key": api_key})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return resp.status, resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read()


def fetch_slskd_refs(host: str, api_key: str) -> set[str]:
  """Basenames of dirs referenced by ANY current slskd transfer (all states).

  Raises RuntimeError on HTTP/JSON failure so main() aborts rather than sweeping
  with an incomplete protection set.
  """
  status, body = _request("GET", f"{host}/api/v0/transfers/downloads", api_key)
  if status >= 400:
    raise RuntimeError(f"slskd transfers returned HTTP {status}")
  try:
    data = json.loads(body) if body else []
  except json.JSONDecodeError as exc:
    raise RuntimeError(f"slskd transfers returned malformed JSON: {exc}") from exc
  refs: set[str] = set()
  for user in data if isinstance(data, list) else []:
    for directory in user.get("directories", []):
      seg = _trailing_segment(directory.get("directory", ""))
      if seg:
        refs.add(seg)
  return refs


def fetch_qbt_refs(host: str, user: str, pw: str) -> set[str]:
  """Basenames referenced by live qBittorrent torrents (save_path/content_path/name).

  Raises RuntimeError on auth/HTTP/JSON failure (main() aborts on a partial set).
  """
  opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
  )
  login = urllib.parse.urlencode({"username": user, "password": pw}).encode()
  try:
    req = urllib.request.Request(f"{host}/api/v2/auth/login", data=login, headers={"Referer": host})
    with opener.open(req, timeout=15) as resp:  # noqa: S310 - localhost
      if resp.status not in (200, 204):  # v5.2.1 returns 204
        raise RuntimeError(f"qBittorrent login HTTP {resp.status}")
    req2 = urllib.request.Request(f"{host}/api/v2/torrents/info", headers={"Referer": host})
    with opener.open(req2, timeout=15) as resp:  # noqa: S310 - localhost
      torrents = json.loads(resp.read())
  except (urllib.error.URLError, json.JSONDecodeError) as exc:
    raise RuntimeError(f"qBittorrent query failed: {exc}") from exc
  refs: set[str] = set()
  for t in torrents:
    for key in ("save_path", "content_path", "name"):
      val = t.get(key)
      if isinstance(val, str) and val:
        base = os.path.basename(val.rstrip("/").rstrip("\\"))
        if base:
          refs.add(base)
  return refs
```

- [ ] **Step 2: Verify import + lint**

Run: `. .venv/bin/activate && python -c "import importlib.util; s=importlib.util.spec_from_file_location('s','scripts/slskd_incomplete_sweep.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')" && ruff check scripts/slskd_incomplete_sweep.py`
Expected: prints `ok`, ruff `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/slskd_incomplete_sweep.py
git commit -m "feat(incomplete-sweep): slskd + qBittorrent reference fetchers"
```

---

## Task 5: `collect_candidates` — sweep-zone dir walker (side effects)

**Files:**
- Modify: `scripts/slskd_incomplete_sweep.py`

- [ ] **Step 1: Add the walker** (after `fetch_qbt_refs`)

```python
def collect_candidates(incomplete_dir: Path) -> list[tuple[Path, float]]:
  """(dir, mtime) pairs for the two slskd-owned sweep zones.

  Zone A: direct children of incomplete_dir EXCEPT the managed subdirs
          (qbittorrent, slskd).
  Zone B: direct children of incomplete_dir/slskd.
  qBittorrent's own temp (incomplete_dir/qbittorrent) is never entered.
  """
  out: list[tuple[Path, float]] = []

  def _children(root: Path) -> None:
    if not root.is_dir():
      return
    for child in sorted(root.iterdir()):
      if not child.is_dir():
        continue
      try:
        out.append((child, child.stat().st_mtime))
      except OSError:
        continue

  if incomplete_dir.is_dir():
    for child in sorted(incomplete_dir.iterdir()):
      if child.is_dir() and child.name not in MANAGED_SUBDIRS:
        try:
          out.append((child, child.stat().st_mtime))
        except OSError:
          continue
  _children(incomplete_dir / "slskd")
  return out
```

- [ ] **Step 2: Verify import + lint**

Run: `. .venv/bin/activate && python -c "import importlib.util; s=importlib.util.spec_from_file_location('s','scripts/slskd_incomplete_sweep.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')" && ruff check scripts/slskd_incomplete_sweep.py`
Expected: prints `ok`, ruff `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/slskd_incomplete_sweep.py
git commit -m "feat(incomplete-sweep): collect_candidates walks the two sweep zones"
```

---

## Task 6: Wire `main()` — gates, containment, dry-run, exit codes

**Files:**
- Modify: `scripts/slskd_incomplete_sweep.py:main` (replace `# Wired in later tasks.`)

- [ ] **Step 1: Replace the placeholder block in `main()`**

```python
  if not incomplete_dir.is_dir():
    print(f"ERROR: incomplete dir {incomplete_dir} not found", file=sys.stderr)
    return 2

  # Build protection sets first — abort if either fetch fails (degraded safety).
  try:
    slskd_refs = fetch_slskd_refs(slskd_host, slskd_key)
  except (urllib.error.URLError, RuntimeError) as exc:
    print(f"ERROR: cannot reach slskd: {exc}", file=sys.stderr)
    return 2
  try:
    qbt_refs = fetch_qbt_refs(qbt_host, qbt_user, qbt_pass)
  except (urllib.error.URLError, RuntimeError) as exc:
    print(f"ERROR: cannot reach qBittorrent: {exc}", file=sys.stderr)
    return 2

  candidates = collect_candidates(incomplete_dir)
  now = _dt.datetime.now()
  targets = plan_incomplete_sweep(
    candidates, slskd_refs, qbt_refs, now=now, min_age_hours=args.min_age_hours
  )

  # Containment: every target must resolve inside incomplete_dir and never BE a
  # managed subdir root.
  root_resolved = incomplete_dir.resolve()
  managed_roots = {(incomplete_dir / m).resolve() for m in MANAGED_SUBDIRS}
  for t in targets:
    rt = t.resolve()
    if root_resolved not in rt.parents:
      print(f"ERROR: refusing to act on {t} — escapes {incomplete_dir}", file=sys.stderr)
      return 2
    if rt in managed_roots:
      print(f"ERROR: refusing to delete managed subdir {t}", file=sys.stderr)
      return 2

  if args.limit and len(targets) > args.limit:
    print(f"limiting to first {args.limit} of {len(targets)} eligible dirs")
    targets = targets[: args.limit]

  bytes_to_free = 0
  for t in targets:
    for dp, _dirs, files in os.walk(t):
      for f in files:
        try:
          bytes_to_free += os.path.getsize(os.path.join(dp, f))
        except OSError:
          continue

  print(
    f"plan: delete {len(targets)} orphan dir(s) (~{bytes_to_free / 1e9:.2f} GB); "
    f"scanned {len(candidates)} candidate(s); "
    f"protected by slskd={len(slskd_refs)}, qbt={len(qbt_refs)}"
    + ("  [DRY RUN]" if args.dry_run else "")
  )

  if args.dry_run:
    for t in targets[:15]:
      print(f"  DRY rmtree {t.relative_to(incomplete_dir)}")
    if len(targets) > 15:
      print(f"  ... and {len(targets) - 15} more")
    return 0

  failed = 0
  for t in targets:
    try:
      shutil.rmtree(t)
    except OSError as exc:
      print(f"WARNING: rmtree {t}: {exc}", file=sys.stderr)
      failed += 1
  print(f"deleted {len(targets) - failed}/{len(targets)} dir(s) (~{bytes_to_free / 1e9:.2f} GB if all succeeded)")
  return 1 if failed else 0
```

- [ ] **Step 2: Run unit suite + lint**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_incomplete_sweep.py -v && ruff check scripts/slskd_incomplete_sweep.py`
Expected: all tests pass, ruff clean.

- [ ] **Step 3: Commit**

```bash
git add scripts/slskd_incomplete_sweep.py
git commit -m "feat(incomplete-sweep): wire main with triple gate, containment, dry-run"
```

---

## Task 7: Live `--dry-run` smoke test (manual)

**Files:** none (verification only)

- [ ] **Step 1: Run a real dry-run against live services**

Run: `. .venv/bin/activate && python scripts/slskd_incomplete_sweep.py --dry-run`
Expected: a `plan:` line reporting ~140+ orphan dirs eligible (the legacy flat root), a small GB figure, `protected by slskd=N, qbt=M` counts, and `[DRY RUN]`; exit 0. The `qbittorrent` and `slskd` subdir roots must NOT appear in the DRY rmtree list. If anything references qBittorrent's active torrents or the counts look wrong, STOP and report — do not run for real.

- [ ] **Step 2: No commit** (verification only).

---

## Task 8: Docs + pnpm wrappers

**Files:**
- Modify: `scripts/README.md`, `AGENTS.md`, `package.json`

- [ ] **Step 1: Add the script to `scripts/README.md`** (beside the other slskd scripts, matching their format)

```markdown
### `slskd_incomplete_sweep.py`

Reusable **gated sweeper** for orphaned dirs in the slskd-owned zones of
`/downloads/incomplete` — the legacy flat dirs at the root and orphans under
`incomplete/slskd`. Never enters `incomplete/qbittorrent` (qBit-owned). A dir is
deleted only if it clears all three gates: not referenced by an active slskd
transfer, not referenced by a live qBittorrent torrent, and older than
`--min-age-hours` (default 24). Aborts (exit 2) if either reference fetch fails.

Acts by default; `--dry-run` previews.

```bash
python scripts/slskd_incomplete_sweep.py --dry-run
python scripts/slskd_incomplete_sweep.py --min-age-hours 24 --limit 50
```

Env: `API_KEY_SLSKD`, `SLSKD_HOST`, `QBITTORRENT_USER`, `QBITTORRENT_PASS`,
`QBITTORRENT_HOST`, `INCOMPLETE_DIR`. Exit: `0` ok/dry-run/no-op, `1` partial,
`2` fatal.
```

- [ ] **Step 2: Add to the `AGENTS.md` operational-scripts list**

```markdown
- `slskd_incomplete_sweep.py` — deletes orphaned dirs from the slskd-owned zones
  of `/downloads/incomplete` (legacy flat root + `incomplete/slskd`), gated on
  live slskd transfers + qBittorrent torrents + an age gate; never touches
  `incomplete/qbittorrent`. Acts by default; `--dry-run` to preview.
```

- [ ] **Step 3: Add pnpm wrappers to `package.json`** (after the `qbt:tidy:dry` line)

```json
    "sweep:incomplete": "bash -c '. .venv/bin/activate && python scripts/slskd_incomplete_sweep.py'",
    "sweep:incomplete:dry": "bash -c '. .venv/bin/activate && python scripts/slskd_incomplete_sweep.py --dry-run'"
```

- [ ] **Step 4: Verify JSON + references**

Run: `cd /home/tom/nas && node -e "require('./package.json')" && grep -l slskd_incomplete_sweep scripts/README.md AGENTS.md`
Expected: no JSON error; both doc files match.

- [ ] **Step 5: Commit**

```bash
git add scripts/README.md AGENTS.md package.json
git commit -m "docs(incomplete-sweep): document slskd_incomplete_sweep + pnpm wrappers"
```

---

## Task 9: Final CI-parity gate (script work)

**Files:** none

- [ ] **Step 1: Run the gates CI runs**

Run: `. .venv/bin/activate && ruff check scripts/slskd_incomplete_sweep.py scripts/tests/test_slskd_incomplete_sweep.py && python scripts/test_scripts.py && pytest -q scripts/tests && cd /home/tom/nas && docker compose config > /dev/null && echo OK`
Expected: ruff clean on the new files, smoke harness exits 0, all pytest pass, compose validates, prints `OK`.

- [ ] **Step 2: Confirm clean tree (new work only)**

Run: `git status --short scripts/ package.json AGENTS.md`
Expected: clean for these paths.

---

## Task 10: Residue op — delete 3 cruft qBittorrent categories (controller live action)

**Files:** none (live API call)

- [ ] **Step 1: Re-confirm the 3 categories are still empty, then remove them**

Run (single python block — re-checks 0 torrents per category before removing):
```bash
.venv/bin/python - <<'PY'
import os, json, urllib.request, urllib.parse, http.cookiejar
from dotenv import load_dotenv
from collections import Counter
load_dotenv("/home/tom/nas/.env")
host="http://localhost:8080"
op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
d=urllib.parse.urlencode({"username":os.environ["QBITTORRENT_USER"],"password":os.environ["QBITTORRENT_PASS"]}).encode()
op.open(urllib.request.Request(f"{host}/api/v2/auth/login", data=d, headers={"Referer":host}), timeout=10).read()
tors=json.loads(op.open(f"{host}/api/v2/torrents/info", timeout=10).read())
used=Counter(t.get("category") or "" for t in tors)
cruft=["movies-radarr","music-lidarr","tv-sonarr"]
busy=[c for c in cruft if used.get(c,0)>0]
if busy:
    print("ABORT — categories not empty:", {c:used[c] for c in busy}); raise SystemExit(2)
body=urllib.parse.urlencode({"categories":"\n".join(cruft)}).encode()
r=op.open(urllib.request.Request(f"{host}/api/v2/torrents/removeCategories", data=body, headers={"Referer":host}), timeout=10)
print("removeCategories HTTP", r.status)
cats=json.loads(op.open(f"{host}/api/v2/torrents/categories", timeout=10).read())
print("remaining categories:", sorted(cats))
PY
```
Expected: `removeCategories HTTP 200`; the remaining-categories list no longer contains `movies-radarr` / `music-lidarr` / `tv-sonarr` (only `arr-*` remain). If it printed `ABORT`, stop — a category gained a torrent; do not force removal.

- [ ] **Step 2: No commit** (qBittorrent state change, not a repo change).

---

## Task 11: Residue op — delete stale `/mnt/drive/.docker-config` (controller live action)

**Files:** none (filesystem deletion)

- [ ] **Step 1: Assert no running container mounts the stale tree**

Run:
```bash
hits=$(docker ps -q | xargs -r docker inspect --format '{{range .Mounts}}{{.Source}}{{"\n"}}{{end}}' 2>/dev/null | grep -c '^/mnt/drive/.docker-config' || true)
echo "containers mounting /mnt/drive/.docker-config: $hits"
```
Expected: `0`. If non-zero, STOP — the tree is in use; do not delete.

- [ ] **Step 2: Delete the stale tree**

Run (only if Step 1 printed 0):
```bash
du -sh /mnt/drive/.docker-config && rm -rf /mnt/drive/.docker-config && echo "removed; remaining under /mnt/drive:" && ls -1 /mnt/drive
```
Expected: the 2.5 GB tree is gone; `/mnt/drive` no longer lists `.docker-config`.

- [ ] **Step 3: No commit** (filesystem change outside the repo).

---

## Self-Review Notes

- **Spec coverage:** Component 1 sweeper → Tasks 1–6 (+7 live verify, 9 gate); three gates → Task 3 planner (slskd_refs/qbt_refs) + Task 6 age/containment; zones (flat root + incomplete/slskd, never qbittorrent) → Task 5 `collect_candidates` + `MANAGED_SUBDIRS`; degraded-safety abort → Task 6 (return 2 on fetch failure); qBit 204 login → Task 4 `fetch_qbt_refs`; containment guard → Task 6. Component 2: cruft categories → Task 10; stale tree → Task 11 (with mount check). Docs/wrappers → Task 8.
- **Type consistency:** `plan_incomplete_sweep(candidates: list[(Path,float)], slskd_refs: set[str], qbt_refs: set[str], *, now, min_age_hours) -> list[Path]` — matches `collect_candidates` output and the `fetch_*_refs` set returns, and the `main()` call site. `_trailing_segment` used by `fetch_slskd_refs` and tested in Task 2. `MANAGED_SUBDIRS` defined Task 1, used Tasks 5 + 6.
- **No placeholders:** every code step shows complete code; every command step shows exact command + expected output.
