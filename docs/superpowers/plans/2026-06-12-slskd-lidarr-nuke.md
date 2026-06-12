# slskd↔Lidarr clean-slate "nuke" button — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `scripts/slskd_lidarr_nuke.py` — an aggressive, idempotent, on-demand command that resets the slskd↔Lidarr download pipeline to zero: graceful Lidarr queue teardown → full slskd transfer wipe → completed-folder sweep.

**Architecture:** Single-file Python stdlib script mirroring `lidarr_stuck_download_reaper.py` / `slskd_complete_sweep.py`. Pure planning functions (`plan_lidarr_nuke`, `collect_slskd_transfers`, `spare_basenames`, `plan_folder_sweep`) are unit-tested with no network; side effects (`urllib` calls, `rmtree`) live only in thin helpers and `main()`. Three phases run in order so each phase's effects make the next phase's reads accurate.

**Tech Stack:** Python 3.11+, stdlib only (`urllib`, `argparse`, `shutil`, `pathlib`, `dataclasses`), `python-dotenv` fallback, `pytest` for tests, `ruff` for lint.

**Spec:** `docs/superpowers/specs/2026-06-12-slskd-lidarr-nuke-design.md`

---

## File Structure

- Create: `scripts/slskd_lidarr_nuke.py` — the script (module + `main()`).
- Create: `scripts/tests/test_slskd_lidarr_nuke.py` — pytest unit tests for the pure planners.
- Modify: `scripts/README.md` — add script entry (flags, exit codes, workflow).
- Modify: `AGENTS.md` — add the script to the script list (reuses existing env vars; no new ones).

All four pure functions and their dataclasses are defined in the single script file. Tests import the module via the same `importlib.util.spec_from_file_location` idiom the existing tests use.

---

## Task 1: Scaffold the script — module header, env wiring, `_request`, exit-code skeleton

**Files:**
- Create: `scripts/slskd_lidarr_nuke.py`

- [ ] **Step 1: Write the script scaffold**

Create `scripts/slskd_lidarr_nuke.py` with the docstring, imports, dotenv fallback, constants, the shared `_request` helper (copied verbatim from `lidarr_stuck_download_reaper.py:108-122` for consistency), and a `main()` that only parses args and validates env so far.

```python
#!/usr/bin/env python3
"""Clean-slate the slskd<->Lidarr pipeline: nuke the Lidarr queue, wipe slskd
transfers, and sweep the slskd completed-downloads folder.

This is the aggressive on-demand counterpart to the gated/throttled reapers
(lidarr_stuck_download_reaper.py, slskd_cleanup.py, slskd_complete_sweep.py).
It resets the whole pipeline to zero, GRACEFULLY:

  Phase 1 (Lidarr, first): DELETE every queue row with
    removeFromClient=true&blocklist=true&skipRedownload=true so Lidarr cancels
    the slskd-side transfer via Tubifarry (nothing orphaned), blocklists the
    dead release (album stays monitored), and does NOT auto re-search.
  Phase 2 (slskd, mop-up): cancel every still-active transfer and clear all
    terminal records -> empty transfer manager.
  Phase 3 (disk, last): rmtree every dir under SLSKD_COMPLETE_DIR except those
    an active Lidarr import still references.

Lidarr on this host uses slskd (Tubifarry) as its ONLY download client, so the
entire queue is slskd-sourced and is wiped wholesale.

Exit codes
----------
  0 success (or dry-run / nothing to do)
  1 partial (some deletes/cancels/rmtrees failed; details on stderr)
  2 fatal (config missing, slskd/Lidarr unreachable, containment violation)

Environment
-----------
  API_KEY_LIDARR       (required) Lidarr API key
  API_KEY_SLSKD        (required) administrator key for slskd /api/v0
  LIDARR_HOST          (default: http://localhost:8686)
  SLSKD_HOST           (default: http://localhost:5030)
  SLSKD_COMPLETE_DIR   (default: /mnt/drive/downloads/complete/slskd)

Usage
-----
  python scripts/slskd_lidarr_nuke.py            # ACT: full clean slate
  python scripts/slskd_lidarr_nuke.py --dry-run  # preview, exit 0
  python scripts/slskd_lidarr_nuke.py --skip-folder-sweep
  python scripts/slskd_lidarr_nuke.py --skip-lidarr --skip-slskd  # folder only
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

if "API_KEY_SLSKD" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_SLSKD_HOST = "http://localhost:5030"
DEFAULT_SLSKD_COMPLETE_DIR = "/mnt/drive/downloads/complete/slskd"
TERMINAL_PREFIX = "Completed"


def _request(
  method: str,
  url: str,
  api_key: str,
  *,
  header: str = "X-API-Key",
  data: bytes | None = None,
  timeout: int = 20,
) -> tuple[int, bytes]:
  headers = {header: api_key}
  if data is not None:
    headers["Content-Type"] = "application/json"
  req = urllib.request.Request(url, method=method, headers=headers, data=data)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return resp.status, resp.read()
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Clean-slate the slskd<->Lidarr pipeline (nuke queue + transfers + folder)."
  )
  parser.add_argument("--dry-run", action="store_true", help="Report the plan and exit 0.")
  parser.add_argument("--skip-lidarr", action="store_true", help="Skip Phase 1 (Lidarr queue).")
  parser.add_argument("--skip-slskd", action="store_true", help="Skip Phase 2 (slskd wipe).")
  parser.add_argument(
    "--skip-folder-sweep", action="store_true", help="Skip Phase 3 (completed-folder sweep)."
  )
  parser.add_argument(
    "--slskd-complete-dir", type=Path, default=None,
    help=f"Override SLSKD_COMPLETE_DIR (env or {DEFAULT_SLSKD_COMPLETE_DIR}).",
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  lidarr_host = os.environ.get("LIDARR_HOST", DEFAULT_LIDARR_HOST).rstrip("/")
  lidarr_key = os.environ.get("API_KEY_LIDARR")
  slskd_host = os.environ.get("SLSKD_HOST", DEFAULT_SLSKD_HOST).rstrip("/")
  slskd_key = os.environ.get("API_KEY_SLSKD")
  if not lidarr_key:
    print("ERROR: API_KEY_LIDARR not set (check .env)", file=sys.stderr)
    return 2
  if not slskd_key:
    print("ERROR: API_KEY_SLSKD not set (check .env)", file=sys.stderr)
    return 2
  # Phases wired in later tasks.
  return 0


if __name__ == "__main__":
  sys.exit(main())
```

- [ ] **Step 2: Verify it parses, imports, and lints**

Run: `. .venv/bin/activate && python -c "import importlib.util,pathlib; s=importlib.util.spec_from_file_location('n','scripts/slskd_lidarr_nuke.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')" && ruff check scripts/slskd_lidarr_nuke.py`
Expected: prints `ok` and ruff reports `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/slskd_lidarr_nuke.py
git commit -m "feat(nuke): scaffold slskd<->lidarr clean-slate script"
```

---

## Task 2: `plan_lidarr_nuke` — select every queue id to delete

**Files:**
- Modify: `scripts/slskd_lidarr_nuke.py`
- Create: `scripts/tests/test_slskd_lidarr_nuke.py`

- [ ] **Step 1: Write the failing test**

Create `scripts/tests/test_slskd_lidarr_nuke.py`:

```python
import importlib.util
import sys
from pathlib import Path

import pytest


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "slskd_lidarr_nuke.py"
  spec = importlib.util.spec_from_file_location("slskd_lidarr_nuke", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


nuke = _load_module()


# ---- plan_lidarr_nuke ----------------------------------------------------


def test_plan_lidarr_nuke_empty_queue():
  assert nuke.plan_lidarr_nuke([]) == []


def test_plan_lidarr_nuke_selects_all_states():
  records = [
    {"id": 1, "status": "downloading"},
    {"id": 2, "status": "importPending"},
    {"id": 3, "status": "completed"},
    {"id": 4, "status": "warning"},
  ]
  assert nuke.plan_lidarr_nuke(records) == [1, 2, 3, 4]


def test_plan_lidarr_nuke_skips_rows_without_int_id():
  records = [{"id": 1}, {"id": None}, {"title": "no id"}, {"id": "x"}]
  assert nuke.plan_lidarr_nuke(records) == [1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_lidarr_nuke.py -v`
Expected: FAIL with `AttributeError: module 'slskd_lidarr_nuke' has no attribute 'plan_lidarr_nuke'`

- [ ] **Step 3: Add the function to `scripts/slskd_lidarr_nuke.py`** (place after `_request`)

```python
def plan_lidarr_nuke(records: list[dict]) -> list[int]:
  """Return every Lidarr queue id to delete (the whole queue).

  Pure over a ``/api/v1/queue`` records list. Rows without an integer ``id``
  are skipped defensively. Order is preserved; ids are unique within a queue.
  """
  return [r["id"] for r in records if isinstance(r.get("id"), int)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_lidarr_nuke.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/slskd_lidarr_nuke.py scripts/tests/test_slskd_lidarr_nuke.py
git commit -m "feat(nuke): plan_lidarr_nuke selects all queue ids"
```

---

## Task 3: `collect_slskd_transfers` — partition active vs terminal

**Files:**
- Modify: `scripts/slskd_lidarr_nuke.py`
- Modify: `scripts/tests/test_slskd_lidarr_nuke.py`

- [ ] **Step 1: Write the failing test** (append to the test file)

```python
# ---- collect_slskd_transfers ---------------------------------------------


def _dl(username, directory, files):
  return {"username": username, "directories": [{"directory": directory, "files": files}]}


def test_collect_slskd_transfers_partitions_active_and_terminal():
  payload = [
    _dl("alice", "music\\A\\X", [
      {"id": "t1", "state": "Queued, Remotely"},
      {"id": "t2", "state": "InProgress"},
      {"id": "t3", "state": "Completed, Succeeded"},
      {"id": "t4", "state": "Completed, Errored"},
    ]),
  ]
  active, terminal = nuke.collect_slskd_transfers(payload)
  assert {(t.username, t.transfer_id) for t in active} == {("alice", "t1"), ("alice", "t2")}
  assert terminal == 2


def test_collect_slskd_transfers_empty():
  assert nuke.collect_slskd_transfers([]) == ([], 0)
  assert nuke.collect_slskd_transfers("not a list") == ([], 0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_lidarr_nuke.py -k collect_slskd_transfers -v`
Expected: FAIL with `AttributeError: ... has no attribute 'collect_slskd_transfers'`

- [ ] **Step 3: Add the dataclass + function** (dataclass near top after constants; function after `plan_lidarr_nuke`)

```python
@dataclass(frozen=True)
class SlskdTransfer:
  username: str
  transfer_id: str
  state: str
```

```python
def collect_slskd_transfers(downloads: object) -> tuple[list[SlskdTransfer], int]:
  """Partition slskd downloads into (active-to-cancel, terminal_record_count).

  Pure over the ``/api/v0/transfers/downloads`` payload. Any transfer whose
  state does NOT start with ``Completed`` is "active" and must be cancelled;
  terminal ``Completed,*`` rows are counted (they are cleared in bulk).
  """
  if not isinstance(downloads, list):
    return [], 0
  active: list[SlskdTransfer] = []
  terminal = 0
  for user in downloads:
    username = user.get("username", "")
    for directory in user.get("directories", []):
      for file in directory.get("files", []):
        state = str(file.get("state", ""))
        if state.startswith(TERMINAL_PREFIX):
          terminal += 1
        else:
          active.append(
            SlskdTransfer(
              username=username,
              transfer_id=file.get("id", ""),
              state=state,
            )
          )
  return active, terminal
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_lidarr_nuke.py -k collect_slskd_transfers -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/slskd_lidarr_nuke.py scripts/tests/test_slskd_lidarr_nuke.py
git commit -m "feat(nuke): collect_slskd_transfers partitions active vs terminal"
```

---

## Task 4: `spare_basenames` + `plan_folder_sweep` — disk sweep planning with containment

**Files:**
- Modify: `scripts/slskd_lidarr_nuke.py`
- Modify: `scripts/tests/test_slskd_lidarr_nuke.py`

- [ ] **Step 1: Write the failing test** (append)

```python
# ---- spare_basenames -----------------------------------------------------


def test_spare_basenames_extracts_path_basenames():
  records = [
    {"outputPath": "/data/downloads/complete/slskd/Album One"},
    {"downloadForcedClientPath": "music\\Artist\\Album Two\\"},
    {"title": "Album Three"},
    {"outputPath": ""},
  ]
  assert nuke.spare_basenames(records) == {"Album One", "Album Two", "Album Three"}


# ---- plan_folder_sweep ---------------------------------------------------


def test_plan_folder_sweep_selects_unspared_children(tmp_path):
  root = tmp_path / "slskd"
  root.mkdir()
  keep = root / "Importing Now"
  drop = root / "Orphan Album"
  keep.mkdir()
  drop.mkdir()
  (root / "loose.txt").write_text("not a dir")  # files ignored
  targets = nuke.plan_folder_sweep(root, {"Importing Now"})
  assert targets == [drop]


def test_plan_folder_sweep_containment_rejects_escape(tmp_path):
  root = tmp_path / "slskd"
  root.mkdir()
  outside = tmp_path / "outside"
  outside.mkdir()
  (root / "link").symlink_to(outside, target_is_directory=True)
  with pytest.raises(ValueError):
    nuke.plan_folder_sweep(root, set())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_lidarr_nuke.py -k "spare_basenames or plan_folder_sweep" -v`
Expected: FAIL with `AttributeError` for `spare_basenames`

- [ ] **Step 3: Add both functions** (after `collect_slskd_transfers`)

```python
def spare_basenames(records: list[dict]) -> set[str]:
  """Path basenames any active Lidarr import still references.

  Mirrors slskd_complete_sweep.active_queue_paths: reduces each path-like field
  to its basename so it can be matched against a completed-dir name.
  """
  out: set[str] = set()
  for r in records:
    for key in ("outputPath", "downloadForcedClientPath", "title"):
      val = r.get(key)
      if isinstance(val, str) and val:
        name = os.path.basename(val.rstrip("/").rstrip("\\"))
        if name:
          out.add(name)
  return out


def plan_folder_sweep(complete_dir: Path, spare: set[str]) -> list[Path]:
  """Direct-child dirs under ``complete_dir`` to delete (not in ``spare``).

  Containment guard: every candidate's resolved parent must equal the resolved
  ``complete_dir``; a child that escapes (e.g. a symlink to elsewhere) raises
  ``ValueError`` so the caller aborts rather than deleting outside the folder.
  """
  resolved_root = complete_dir.resolve()
  targets: list[Path] = []
  for child in sorted(complete_dir.iterdir()):
    if not child.is_dir():
      continue
    if child.resolve().parent != resolved_root:
      raise ValueError(f"{child} escapes {complete_dir} — refusing to sweep")
    if child.name in spare:
      continue
    targets.append(child)
  return targets
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_lidarr_nuke.py -k "spare_basenames or plan_folder_sweep" -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/slskd_lidarr_nuke.py scripts/tests/test_slskd_lidarr_nuke.py
git commit -m "feat(nuke): spare_basenames + plan_folder_sweep with containment guard"
```

---

## Task 5: Side-effecting helpers — fetch/delete wrappers

**Files:**
- Modify: `scripts/slskd_lidarr_nuke.py`

These are thin `urllib` wrappers (not unit-tested — they only call `_request`; the planners they feed are tested). Verify against live services in Task 7's manual `--dry-run`.

- [ ] **Step 1: Add the helpers** (after `plan_folder_sweep`)

```python
def fetch_lidarr_queue(host: str, api_key: str) -> list[dict]:
  url = f"{host}/api/v1/queue?pageSize=1000&includeUnknownArtistItems=true"
  status, body = _request("GET", url, api_key, header="X-Api-Key")
  if status >= 400:
    raise RuntimeError(f"GET /api/v1/queue returned HTTP {status}")
  return json.loads(body).get("records", []) if body else []


def bulk_delete_lidarr(host: str, api_key: str, ids: list[int]) -> bool:
  """DELETE /api/v1/queue/bulk with the graceful teardown params.

  removeFromClient cancels the slskd transfer via Tubifarry; blocklist marks
  the dead release (album stays monitored); skipRedownload suppresses auto
  re-search. Returns True on 200/204.
  """
  params = urllib.parse.urlencode(
    {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"}
  )
  url = f"{host}/api/v1/queue/bulk?{params}"
  payload = json.dumps({"ids": ids}).encode()
  status, _ = _request("DELETE", url, api_key, header="X-Api-Key", data=payload)
  return status in (200, 204)


def delete_lidarr_item(host: str, api_key: str, queue_id: int) -> bool:
  params = urllib.parse.urlencode(
    {"removeFromClient": "true", "blocklist": "true", "skipRedownload": "true"}
  )
  url = f"{host}/api/v1/queue/{queue_id}?{params}"
  status, _ = _request("DELETE", url, api_key, header="X-Api-Key")
  return status in (200, 204)


def fetch_slskd_downloads(host: str, api_key: str) -> list[dict]:
  status, body = _request("GET", f"{host}/api/v0/transfers/downloads", api_key)
  if status >= 400:
    raise RuntimeError(f"GET /api/v0/transfers/downloads returned HTTP {status}")
  return json.loads(body) if body else []


def cancel_slskd_transfer(host: str, api_key: str, t: SlskdTransfer) -> bool:
  user = urllib.parse.quote(t.username, safe="")
  url = f"{host}/api/v0/transfers/downloads/{user}/{t.transfer_id}?remove=true"
  status, _ = _request("DELETE", url, api_key)
  return status in (200, 204, 404)  # 404 == already gone


def clear_slskd_completed(host: str, api_key: str) -> bool:
  """Bulk-clear all terminal slskd download records.

  Returns True on 200/204. Returns False if the endpoint is unavailable (404 /
  405 / >=400) so main() can fall back to per-transfer cleanup.
  """
  url = f"{host}/api/v0/transfers/downloads/all/completed"
  status, _ = _request("DELETE", url, api_key)
  return status in (200, 204)
```

- [ ] **Step 2: Verify import + lint**

Run: `. .venv/bin/activate && python -c "import importlib.util; s=importlib.util.spec_from_file_location('n','scripts/slskd_lidarr_nuke.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')" && ruff check scripts/slskd_lidarr_nuke.py`
Expected: prints `ok`, ruff `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/slskd_lidarr_nuke.py
git commit -m "feat(nuke): add Lidarr/slskd HTTP helpers"
```

---

## Task 6: Wire `main()` — three phases, dry-run, summary, exit codes

**Files:**
- Modify: `scripts/slskd_lidarr_nuke.py:main` (replace the placeholder body after env validation)

- [ ] **Step 1: Replace the `# Phases wired in later tasks.` block in `main()`**

```python
  complete_dir = args.slskd_complete_dir or Path(
    os.environ.get("SLSKD_COMPLETE_DIR", DEFAULT_SLSKD_COMPLETE_DIR)
  )

  print("=== slskd<->Lidarr CLEAN SLATE ===" + ("  [DRY RUN]" if args.dry_run else ""))
  failures = 0

  # --- Phase 1: Lidarr queue teardown (graceful, first) ---
  if not args.skip_lidarr:
    try:
      records = fetch_lidarr_queue(lidarr_host, lidarr_key)
    except (urllib.error.URLError, RuntimeError) as exc:
      print(f"ERROR: cannot reach Lidarr: {exc}", file=sys.stderr)
      return 2
    ids = plan_lidarr_nuke(records)
    print(f"Phase 1 (Lidarr): {len(ids)} queue row(s) -> remove+blocklist+skipRedownload")
    if ids and not args.dry_run:
      if not bulk_delete_lidarr(lidarr_host, lidarr_key, ids):
        # Fall back to per-id deletes if the bulk endpoint failed.
        ok = 0
        for qid in ids:
          if delete_lidarr_item(lidarr_host, lidarr_key, qid):
            ok += 1
          else:
            failures += 1
            print(f"WARNING: failed to delete Lidarr queue/{qid}", file=sys.stderr)
        print(f"  deleted {ok}/{len(ids)} row(s) (per-id fallback)")
      else:
        print(f"  deleted {len(ids)} row(s) (bulk)")

  # --- Phase 2: slskd full wipe (mop-up) ---
  if not args.skip_slskd:
    try:
      downloads = fetch_slskd_downloads(slskd_host, slskd_key)
    except (urllib.error.URLError, RuntimeError) as exc:
      print(f"ERROR: cannot reach slskd: {exc}", file=sys.stderr)
      return 2
    active, terminal = collect_slskd_transfers(downloads)
    print(f"Phase 2 (slskd): cancel {len(active)} active transfer(s), clear {terminal} record(s)")
    if not args.dry_run:
      cancelled = 0
      for t in active:
        if cancel_slskd_transfer(slskd_host, slskd_key, t):
          cancelled += 1
        else:
          failures += 1
          print(f"WARNING: failed to cancel slskd {t.username}/{t.transfer_id}", file=sys.stderr)
      if active:
        print(f"  cancelled {cancelled}/{len(active)} active transfer(s)")
      if not clear_slskd_completed(slskd_host, slskd_key):
        # Endpoint unavailable: re-fetch and remove terminal records per-transfer.
        leftovers = fetch_slskd_downloads(slskd_host, slskd_key)
        for user in leftovers if isinstance(leftovers, list) else []:
          for directory in user.get("directories", []):
            for file in directory.get("files", []):
              if str(file.get("state", "")).startswith(TERMINAL_PREFIX):
                t = SlskdTransfer(user.get("username", ""), file.get("id", ""), file.get("state", ""))
                if not cancel_slskd_transfer(slskd_host, slskd_key, t):
                  failures += 1
        print("  cleared terminal records (per-transfer fallback)")
      else:
        print("  cleared all terminal records (bulk)")

  # --- Phase 3: completed-folder sweep (disk, last) ---
  if not args.skip_folder_sweep:
    if not complete_dir.is_dir():
      print(f"Phase 3 (folder): {complete_dir} not found — skipping", file=sys.stderr)
    else:
      spare: set[str] = set()
      if not args.skip_lidarr:
        try:
          spare = spare_basenames(fetch_lidarr_queue(lidarr_host, lidarr_key))
        except (urllib.error.URLError, RuntimeError):
          spare = set()  # queue already drained / unreachable -> nothing to spare
      try:
        targets = plan_folder_sweep(complete_dir, spare)
      except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
      bytes_to_free = 0
      for d in targets:
        for dp, _dirs, files in os.walk(d):
          for f in files:
            try:
              bytes_to_free += os.path.getsize(os.path.join(dp, f))
            except OSError:
              continue
      print(
        f"Phase 3 (folder): delete {len(targets)} dir(s) (~{bytes_to_free / 1e9:.2f} GB), "
        f"sparing {len(spare)} active import(s)"
      )
      if args.dry_run:
        for d in targets[:15]:
          print(f"  DRY rmtree {d.name}")
        if len(targets) > 15:
          print(f"  ... and {len(targets) - 15} more")
      else:
        for d in targets:
          try:
            shutil.rmtree(d)
          except OSError as exc:
            print(f"WARNING: rmtree {d}: {exc}", file=sys.stderr)
            failures += 1
        print(f"  deleted {len(targets)} dir(s) (~{bytes_to_free / 1e9:.2f} GB if all succeeded)")

  if args.dry_run:
    return 0
  return 1 if failures else 0
```

- [ ] **Step 2: Run the full unit suite + lint**

Run: `. .venv/bin/activate && pytest scripts/tests/test_slskd_lidarr_nuke.py -v && ruff check scripts/slskd_lidarr_nuke.py`
Expected: all tests pass, ruff `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/slskd_lidarr_nuke.py
git commit -m "feat(nuke): wire three-phase main with dry-run and exit codes"
```

---

## Task 7: Live `--dry-run` smoke test (manual, API verification)

**Files:** none (verification only)

- [ ] **Step 1: Confirm the slskd bulk-completed endpoint exists**

Run: `. .venv/bin/activate && curl -s -o /dev/null -w "%{http_code}\n" -X DELETE -H "X-API-Key: $API_KEY_SLSKD" "${SLSKD_HOST:-http://localhost:5030}/api/v0/transfers/downloads/all/completed"`
Expected: `200` or `204` (endpoint exists). If `404`/`405`, the per-transfer fallback in `main()` already covers it — note it in the README but no code change needed.

- [ ] **Step 2: Run a real dry-run against the live stack**

Run: `. .venv/bin/activate && python scripts/slskd_lidarr_nuke.py --dry-run`
Expected: prints the `=== slskd<->Lidarr CLEAN SLATE ===  [DRY RUN]` banner and three `Phase N` lines with plausible counts (matching what you see in Lidarr's queue / slskd's transfers), exits 0. Confirms `fetch_lidarr_queue` and `fetch_slskd_downloads` parse the live payloads.

- [ ] **Step 3: No commit** (verification only). If a payload-shape mismatch surfaces, fix it in the relevant pure function, add a regression test, and re-run the suite before proceeding.

---

## Task 8: Docs — `scripts/README.md` and `AGENTS.md`

**Files:**
- Modify: `scripts/README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Add the script to `scripts/README.md`**

Find the section listing the slskd/Lidarr scripts (near `slskd_complete_sweep.py` / `lidarr_stuck_download_reaper.py`) and add an entry matching the surrounding format:

```markdown
### `slskd_lidarr_nuke.py`

**Clean-slate button** for the slskd↔Lidarr pipeline — aggressive, on-demand,
idempotent. The opposite of the gated reapers: it resets everything to zero.

1. **Lidarr queue teardown** — deletes every queue row with
   `removeFromClient=true&blocklist=true&skipRedownload=true`. Lidarr cancels
   the slskd transfer via Tubifarry (nothing orphaned), blocklists the dead
   release (album stays monitored/missing), and does **not** auto re-search —
   re-kick searches yourself (e.g. lidarr-bulk) to re-grab fresh copies.
2. **slskd full wipe** — cancels every active/queued transfer and clears all
   terminal records (`DELETE .../downloads/all/completed`, per-transfer
   fallback).
3. **Completed-folder sweep** — `rmtree`s every dir under
   `SLSKD_COMPLETE_DIR` except those an active Lidarr import references.

Acts by default; `--dry-run` previews. Phase toggles: `--skip-lidarr`,
`--skip-slskd`, `--skip-folder-sweep`.

```bash
python scripts/slskd_lidarr_nuke.py --dry-run   # preview
python scripts/slskd_lidarr_nuke.py             # ACT: full clean slate
```

Env: `API_KEY_LIDARR`, `API_KEY_SLSKD`, `LIDARR_HOST`, `SLSKD_HOST`,
`SLSKD_COMPLETE_DIR`. Exit: `0` ok/dry-run/noop, `1` partial, `2` fatal.
```

- [ ] **Step 2: Add the script to the `AGENTS.md` script list**

Find the list of scripts in `AGENTS.md` and add a one-line entry in the same style as the neighbours (reuses existing env vars — no `.env.example` change needed):

```markdown
- `slskd_lidarr_nuke.py` — on-demand clean-slate: nukes the whole Lidarr queue
  (remove+blocklist+skipRedownload), wipes all slskd transfers, and sweeps the
  slskd completed folder. Acts by default; `--dry-run` to preview.
```

- [ ] **Step 3: Verify the docs reference real flags/env**

Run: `grep -n "slskd_lidarr_nuke" scripts/README.md AGENTS.md`
Expected: matches in both files.

- [ ] **Step 4: Commit**

```bash
git add scripts/README.md AGENTS.md
git commit -m "docs(nuke): document slskd_lidarr_nuke in README + AGENTS"
```

---

## Task 9: Final gate — full local CI parity

**Files:** none

- [ ] **Step 1: Run the same gates CI runs**

Run: `. .venv/bin/activate && ruff check scripts && python scripts/test_scripts.py && pytest -q scripts/tests`
Expected: ruff clean, smoke harness exits 0, all pytest tests pass (including the new `test_slskd_lidarr_nuke.py`).

- [ ] **Step 2: Confirm no stray changes**

Run: `git status --short`
Expected: clean tree (everything committed).

---

## Self-Review Notes

- **Spec coverage:** Phase 1/2/3 → Tasks 2/3/4 (planners) + Task 6 (wiring); graceful params → Task 5 `bulk_delete_lidarr`; containment → Task 4 `plan_folder_sweep`; dry-run/skip flags/exit codes → Tasks 1+6; docs → Task 8; slskd `all/completed` verification → Task 7. All spec sections covered.
- **Type consistency:** `SlskdTransfer(username, transfer_id, state)` used identically in Tasks 3, 5, 6. `plan_lidarr_nuke -> list[int]` feeds `bulk_delete_lidarr(ids)`. `spare_basenames -> set[str]` feeds `plan_folder_sweep(complete_dir, spare)`. `collect_slskd_transfers -> (list, int)` matches the Task 3 test and Task 6 unpacking.
- **No placeholders:** every code step shows complete code; every run step shows the exact command and expected output.
