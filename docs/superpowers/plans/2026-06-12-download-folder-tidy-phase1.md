# Download-folder tidy — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make qBittorrent categories actually drive save paths (enable Auto TMM + relocate the 107 existing torrents out of `complete/manual/` into their category folders) and give slskd its own `incomplete/` dir so it stops sharing qBittorrent's temp path.

**Architecture:** A new idempotent `scripts/qbittorrent_settings_enforce.py` (stdlib `urllib` + cookie-jar auth, pure planning functions + side effects in `main()`, exit 0/1/2, acts by default with `--dry-run`) drives the qBittorrent WebUI API v2. Config edits to `docker-compose.yml` + the live `slskd.yml` plus one guarded slskd recreate handle the incomplete split. Mounts are untouched.

**Tech Stack:** Python 3.11+ stdlib (`urllib`, `http.cookiejar`, `argparse`, `json`, `dataclasses`), `python-dotenv` fallback, `pytest`, `ruff`; Docker Compose; slskd env/yaml.

**Spec:** `docs/superpowers/specs/2026-06-12-download-folder-tidy-phase1-design.md`

**Verified live facts (do not re-derive):**
- qBittorrent WebUI: `http://localhost:8080`, creds `QBITTORRENT_USER` / `QBITTORRENT_PASS` (in `.env`).
- Live prefs to change: `auto_tmm_enabled` False→True, `category_changed_tmm_enabled` False→True, `save_path_changed_tmm_enabled` False→True, `temp_path` `/downloads/incomplete`→`/downloads/incomplete/qbittorrent`, `temp_path_enabled` already True.
- 107 torrents (95 `arr-sonarr`, 12 `arr-radarr`), all `auto_tmm=false`, all saved in `/downloads/complete/manual`.
- Live category paths already correct: `arr-sonarr`→`/downloads/complete/sonarr`, `arr-radarr`→`/downloads/complete/radarr`, `arr-lidarr`→`/downloads/complete/lidarr`, `arr-slskd`→`/downloads/complete/slskd`.
- LIVE config dir = `${CONFIG_DIRECTORY}` = `/home/tom/nas/.docker-config` (the containers mount this; `/mnt/drive/.docker-config` is a stale duplicate — never edit it).
- Live `slskd.yml` (`/home/tom/nas/.docker-config/slskd/slskd.yml`): `directories.incomplete: /downloads/incomplete`, `directories.downloads: /downloads/complete/slskd`.
- Compose slskd `environment:` has `SLSKD_DOWNLOADS_DIR=/downloads/complete/slskd` (≈ line 239); slskd env overrides yaml.

---

## File Structure

- Create: `scripts/qbittorrent_settings_enforce.py` — the enforcement script (module + `main()`).
- Create: `scripts/tests/test_qbittorrent_settings_enforce.py` — pytest unit tests for the pure planners.
- Modify: `docker-compose.yml` — add `SLSKD_INCOMPLETE_DIR` env to slskd.
- Modify: `/home/tom/nas/.docker-config/slskd/slskd.yml` — align `directories.incomplete`.
- Modify: `scripts/README.md`, `AGENTS.md`, `package.json` — docs + wrappers.

Pure functions and `QbtClient` helper live in the single script file. Tests use the `importlib.util.spec_from_file_location` loader idiom.

---

## Task 1: Scaffold `qbittorrent_settings_enforce.py`

**Files:**
- Create: `scripts/qbittorrent_settings_enforce.py`

- [ ] **Step 1: Write the scaffold**

```python
#!/usr/bin/env python3
"""Enforce qBittorrent Auto Torrent Management so categories drive save paths.

Background
----------
The *arr apps tag torrents with categories (arr-sonarr / arr-radarr) whose save
paths are correct (/downloads/complete/{sonarr,radarr}), but qBittorrent's Auto
Torrent Management (TMM) is OFF — so the category never drives the save path and
every torrent lands in the global default /downloads/complete/manual. This
script turns TMM on and flips existing torrents to auto-managed so qBittorrent
relocates each into its category folder (an instant same-filesystem rename;
hardlinks into the library are preserved). It also points qBittorrent's temp
(incomplete) path at /downloads/incomplete/qbittorrent so it stops sharing one
flat incomplete dir with slskd.

Idempotent: a run with TMM already on and all torrents managed is a no-op.

Exit codes
----------
  0 success (or dry-run / nothing to change)
  1 partial (some API calls failed; details on stderr)
  2 fatal (config missing, qBittorrent unreachable, auth failed)

Environment
-----------
  QBITTORRENT_USER   (required) WebUI username
  QBITTORRENT_PASS   (required) WebUI password
  QBITTORRENT_HOST   (default: http://localhost:8080)

Usage
-----
  python scripts/qbittorrent_settings_enforce.py            # ACT
  python scripts/qbittorrent_settings_enforce.py --dry-run  # preview, exit 0
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

if "QBITTORRENT_USER" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass

DEFAULT_QBT_HOST = "http://localhost:8080"
DESIRED_PREFS = {
  "auto_tmm_enabled": True,
  "category_changed_tmm_enabled": True,
  "save_path_changed_tmm_enabled": True,
  "temp_path_enabled": True,
  "temp_path": "/downloads/incomplete/qbittorrent",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Enable qBittorrent Auto TMM and relocate existing torrents into category folders."
  )
  parser.add_argument("--dry-run", action="store_true", help="Report the plan and exit 0.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  host = os.environ.get("QBITTORRENT_HOST", DEFAULT_QBT_HOST).rstrip("/")
  user = os.environ.get("QBITTORRENT_USER")
  pw = os.environ.get("QBITTORRENT_PASS")
  if not user or not pw:
    print("ERROR: QBITTORRENT_USER / QBITTORRENT_PASS not set (check .env)", file=sys.stderr)
    return 2
  # Wired in later tasks.
  return 0


if __name__ == "__main__":
  sys.exit(main())
```

- [ ] **Step 2: Verify import + lint**

Run: `. .venv/bin/activate && python -c "import importlib.util; s=importlib.util.spec_from_file_location('q','scripts/qbittorrent_settings_enforce.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')" && ruff check scripts/qbittorrent_settings_enforce.py`
Expected: prints `ok`, ruff `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/qbittorrent_settings_enforce.py
git commit -m "feat(qbt-tidy): scaffold qbittorrent_settings_enforce"
```

---

## Task 2: `plan_pref_changes` — minimal pref diff

**Files:**
- Modify: `scripts/qbittorrent_settings_enforce.py`
- Create: `scripts/tests/test_qbittorrent_settings_enforce.py`

- [ ] **Step 1: Write the failing test**

```python
import importlib.util
import sys
from pathlib import Path


def _load_module():
  root = Path(__file__).resolve().parents[2]
  scripts_dir = root / "scripts"
  if str(scripts_dir) not in sys.path:
    sys.path.insert(0, str(scripts_dir))
  script_path = scripts_dir / "qbittorrent_settings_enforce.py"
  spec = importlib.util.spec_from_file_location("qbittorrent_settings_enforce", script_path)
  module = importlib.util.module_from_spec(spec)
  assert spec.loader is not None
  sys.modules[spec.name] = module
  spec.loader.exec_module(module)
  return module


qbt = _load_module()


# ---- plan_pref_changes ---------------------------------------------------


def test_plan_pref_changes_returns_only_differing_keys():
  current = {
    "auto_tmm_enabled": False,
    "category_changed_tmm_enabled": True,
    "save_path_changed_tmm_enabled": False,
    "temp_path_enabled": True,
    "temp_path": "/downloads/incomplete",
    "unrelated": "x",
  }
  desired = qbt.DESIRED_PREFS
  changes = qbt.plan_pref_changes(current, desired)
  assert changes == {
    "auto_tmm_enabled": True,
    "save_path_changed_tmm_enabled": True,
    "temp_path": "/downloads/incomplete/qbittorrent",
  }


def test_plan_pref_changes_empty_when_already_correct():
  current = dict(qbt.DESIRED_PREFS)
  assert qbt.plan_pref_changes(current, qbt.DESIRED_PREFS) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_qbittorrent_settings_enforce.py -k plan_pref_changes -v`
Expected: FAIL with `AttributeError: ... has no attribute 'plan_pref_changes'`

- [ ] **Step 3: Add the function** (after `parse_args`)

```python
def plan_pref_changes(current: dict, desired: dict) -> dict:
  """Return the subset of ``desired`` whose value differs from ``current``.

  Pure. Keys absent from ``current`` count as differing (will be set).
  """
  return {k: v for k, v in desired.items() if current.get(k) != v}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_qbittorrent_settings_enforce.py -k plan_pref_changes -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/qbittorrent_settings_enforce.py scripts/tests/test_qbittorrent_settings_enforce.py
git commit -m "feat(qbt-tidy): plan_pref_changes computes minimal pref diff"
```

---

## Task 3: `collect_unmanaged_hashes` — torrents needing Auto TMM

**Files:**
- Modify: `scripts/qbittorrent_settings_enforce.py`
- Modify: `scripts/tests/test_qbittorrent_settings_enforce.py`

- [ ] **Step 1: Write the failing test** (append)

```python
# ---- collect_unmanaged_hashes --------------------------------------------


def test_collect_unmanaged_hashes_picks_non_auto_tmm():
  torrents = [
    {"hash": "a", "auto_tmm": False},
    {"hash": "b", "auto_tmm": True},
    {"hash": "c", "auto_tmm": False},
    {"hash": "d"},  # missing -> treated as unmanaged
  ]
  assert qbt.collect_unmanaged_hashes(torrents) == ["a", "c", "d"]


def test_collect_unmanaged_hashes_empty():
  assert qbt.collect_unmanaged_hashes([]) == []
  assert qbt.collect_unmanaged_hashes([{"hash": "x", "auto_tmm": True}]) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_qbittorrent_settings_enforce.py -k collect_unmanaged_hashes -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Add the function** (after `plan_pref_changes`)

```python
def collect_unmanaged_hashes(torrents: list[dict]) -> list[str]:
  """Hashes of torrents not already auto-managed (TMM off / missing).

  Pure over ``GET /api/v2/torrents/info``. Order preserved.
  """
  return [t["hash"] for t in torrents if t.get("hash") and not t.get("auto_tmm", False)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_qbittorrent_settings_enforce.py -k collect_unmanaged_hashes -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/qbittorrent_settings_enforce.py scripts/tests/test_qbittorrent_settings_enforce.py
git commit -m "feat(qbt-tidy): collect_unmanaged_hashes selects TMM-off torrents"
```

---

## Task 4: `summarize_targets` — relocate preview by category

**Files:**
- Modify: `scripts/qbittorrent_settings_enforce.py`
- Modify: `scripts/tests/test_qbittorrent_settings_enforce.py`

- [ ] **Step 1: Write the failing test** (append)

```python
# ---- summarize_targets ---------------------------------------------------


def test_summarize_targets_counts_by_target_path():
  torrents = [
    {"hash": "a", "category": "arr-sonarr"},
    {"hash": "b", "category": "arr-sonarr"},
    {"hash": "c", "category": "arr-radarr"},
    {"hash": "d", "category": ""},  # uncategorized -> default/manual
  ]
  categories = {
    "arr-sonarr": {"savePath": "/downloads/complete/sonarr"},
    "arr-radarr": {"savePath": "/downloads/complete/radarr"},
  }
  out = qbt.summarize_targets(torrents, categories)
  assert out == {
    "/downloads/complete/sonarr": 2,
    "/downloads/complete/radarr": 1,
    "(default save path)": 1,
  }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_qbittorrent_settings_enforce.py -k summarize_targets -v`
Expected: FAIL with `AttributeError`

- [ ] **Step 3: Add the function** (after `collect_unmanaged_hashes`)

```python
def summarize_targets(torrents: list[dict], categories: dict) -> dict[str, int]:
  """Count where each torrent will land once auto-managed.

  Pure. A torrent's target is its category's ``savePath``; an empty/missing
  category or empty savePath falls back to the qBittorrent default save path
  (reported as the literal ``"(default save path)"``).
  """
  out: dict[str, int] = {}
  for t in torrents:
    cat = t.get("category") or ""
    save = categories.get(cat, {}).get("savePath") or ""
    key = save if save else "(default save path)"
    out[key] = out.get(key, 0) + 1
  return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `. .venv/bin/activate && pytest scripts/tests/test_qbittorrent_settings_enforce.py -k summarize_targets -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add scripts/qbittorrent_settings_enforce.py scripts/tests/test_qbittorrent_settings_enforce.py
git commit -m "feat(qbt-tidy): summarize_targets previews relocate destinations"
```

---

## Task 5: `QbtClient` — cookie-auth API wrapper (side effects)

**Files:**
- Modify: `scripts/qbittorrent_settings_enforce.py`

Thin wrapper over the qBittorrent WebUI API v2 (not unit-tested — it only does HTTP; the planners it feeds are tested). Verified live in Task 7.

- [ ] **Step 1: Add the client class** (after `summarize_targets`)

```python
class QbtClient:
  """Minimal qBittorrent WebUI API v2 client (cookie-jar session)."""

  def __init__(self, host: str):
    self.host = host
    self._opener = urllib.request.build_opener(
      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )

  def _post(self, path: str, data: dict, timeout: int = 30) -> tuple[int, bytes]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
      f"{self.host}{path}", data=body, headers={"Referer": self.host}
    )
    try:
      with self._opener.open(req, timeout=timeout) as resp:  # noqa: S310 - localhost
        return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
      return exc.code, exc.read()

  def _get_json(self, path: str, timeout: int = 30):
    req = urllib.request.Request(f"{self.host}{path}", headers={"Referer": self.host})
    with self._opener.open(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read())

  def login(self, user: str, pw: str) -> bool:
    status, body = self._post("/api/v2/auth/login", {"username": user, "password": pw})
    # qBittorrent returns 200 + "Ok." on success; localhost auth-bypass returns 200/empty.
    return status == 200 and b"Fails" not in body

  def get_preferences(self) -> dict:
    return self._get_json("/api/v2/app/preferences")

  def set_preferences(self, changes: dict) -> bool:
    status, _ = self._post("/api/v2/app/setPreferences", {"json": json.dumps(changes)})
    return status == 200

  def get_torrents(self) -> list[dict]:
    return self._get_json("/api/v2/torrents/info")

  def get_categories(self) -> dict:
    return self._get_json("/api/v2/torrents/categories")

  def set_auto_management(self, hashes: list[str], enable: bool = True) -> bool:
    status, _ = self._post(
      "/api/v2/torrents/setAutoManagement",
      {"hashes": "|".join(hashes), "enable": "true" if enable else "false"},
    )
    return status == 200
```

- [ ] **Step 2: Verify import + lint**

Run: `. .venv/bin/activate && python -c "import importlib.util; s=importlib.util.spec_from_file_location('q','scripts/qbittorrent_settings_enforce.py'); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); print('ok')" && ruff check scripts/qbittorrent_settings_enforce.py`
Expected: prints `ok`, ruff `All checks passed!`

- [ ] **Step 3: Commit**

```bash
git add scripts/qbittorrent_settings_enforce.py
git commit -m "feat(qbt-tidy): add QbtClient WebUI API wrapper"
```

---

## Task 6: Wire `main()` — dry-run, act, summary, exit codes

**Files:**
- Modify: `scripts/qbittorrent_settings_enforce.py:main` (replace `# Wired in later tasks.`)

- [ ] **Step 1: Replace the placeholder block in `main()`**

```python
  client = QbtClient(host)
  if not client.login(user, pw):
    print(f"ERROR: qBittorrent auth failed at {host}", file=sys.stderr)
    return 2
  try:
    prefs = client.get_preferences()
    torrents = client.get_torrents()
    categories = client.get_categories()
  except (urllib.error.URLError, json.JSONDecodeError) as exc:
    print(f"ERROR: cannot read qBittorrent state: {exc}", file=sys.stderr)
    return 2

  pref_changes = plan_pref_changes(prefs, DESIRED_PREFS)
  unmanaged = collect_unmanaged_hashes(torrents)
  targets = summarize_targets([t for t in torrents if t.get("hash") in set(unmanaged)], categories)

  print("=== qBittorrent settings enforce ===" + ("  [DRY RUN]" if args.dry_run else ""))
  print(f"pref changes: {pref_changes or 'none'}")
  print(f"torrents to auto-manage: {len(unmanaged)} of {len(torrents)}")
  for path, n in sorted(targets.items()):
    print(f"  -> {path}: {n}")

  if args.dry_run:
    return 0

  failures = 0
  if pref_changes:
    if client.set_preferences(pref_changes):
      print(f"applied {len(pref_changes)} pref change(s)")
    else:
      failures += 1
      print("WARNING: setPreferences failed", file=sys.stderr)

  if unmanaged:
    # Chunk to keep the request body sane on large libraries.
    chunk = 200
    done = 0
    for i in range(0, len(unmanaged), chunk):
      batch = unmanaged[i : i + chunk]
      if client.set_auto_management(batch, enable=True):
        done += len(batch)
      else:
        failures += 1
        print(f"WARNING: setAutoManagement failed for {len(batch)} torrents", file=sys.stderr)
    print(f"auto-managed {done}/{len(unmanaged)} torrent(s) (relocating into category folders)")

  return 1 if failures else 0
```

- [ ] **Step 2: Run unit suite + lint**

Run: `. .venv/bin/activate && pytest scripts/tests/test_qbittorrent_settings_enforce.py -v && ruff check scripts/qbittorrent_settings_enforce.py`
Expected: all tests pass, ruff clean.

- [ ] **Step 3: Commit**

```bash
git add scripts/qbittorrent_settings_enforce.py
git commit -m "feat(qbt-tidy): wire main with dry-run, apply, exit codes"
```

---

## Task 7: Live `--dry-run` smoke test (manual)

**Files:** none (verification only)

- [ ] **Step 1: Run a real dry-run against live qBittorrent**

Run: `. .venv/bin/activate && python scripts/qbittorrent_settings_enforce.py --dry-run`
Expected: banner `=== qBittorrent settings enforce ===  [DRY RUN]`; `pref changes` lists the TMM keys + temp_path; `torrents to auto-manage: 107 of 107` (or current count); target breakdown shows `/downloads/complete/sonarr: 95` and `/downloads/complete/radarr: 12` (or current). Exit 0. If counts or paths look wrong, STOP and report — do not run for real.

- [ ] **Step 2: No commit** (verification only).

---

## Task 8: slskd incomplete-dir split (config edits + guarded recreate)

**Files:**
- Modify: `docker-compose.yml`
- Modify: `/home/tom/nas/.docker-config/slskd/slskd.yml`

- [ ] **Step 1: Pre-create the per-client incomplete dirs**

Run:
```bash
set -a && . /home/tom/nas/.env && set +a
mkdir -p "$SHARE_DIRECTORY/downloads/incomplete/qbittorrent" "$SHARE_DIRECTORY/downloads/incomplete/slskd"
chown "$PUID:$PGID" "$SHARE_DIRECTORY/downloads/incomplete/qbittorrent" "$SHARE_DIRECTORY/downloads/incomplete/slskd"
ls -ld "$SHARE_DIRECTORY/downloads/incomplete/qbittorrent" "$SHARE_DIRECTORY/downloads/incomplete/slskd"
```
Expected: both dirs exist, owned by `$PUID:$PGID`.

- [ ] **Step 2: Add `SLSKD_INCOMPLETE_DIR` to compose**

In `docker-compose.yml`, in the slskd `environment:` block, add the line immediately after `- SLSKD_DOWNLOADS_DIR=/downloads/complete/slskd`:

```yaml
      - SLSKD_INCOMPLETE_DIR=/downloads/incomplete/slskd
```

- [ ] **Step 3: Align the live `slskd.yml`**

In `/home/tom/nas/.docker-config/slskd/slskd.yml`, change the `directories.incomplete` line:

```yaml
directories:
  incomplete: /downloads/incomplete/slskd
  downloads: /downloads/complete/slskd
```

- [ ] **Step 4: Validate compose**

Run: `cd /home/tom/nas && docker compose config > /dev/null && echo "compose OK"`
Expected: `compose OK`.

- [ ] **Step 5: Recreate slskd (guarded) and verify**

slskd has no active transfers (just nuked), so this is the low-risk window. Run:
```bash
cd /home/tom/nas && docker compose up -d slskd && sleep 8 && docker logs slskd --tail 20
```
Expected: slskd starts; healthcheck recovers. **Watch the Soulseek login** per the ghost-session rule in CLAUDE.md — if login hangs at 5 s repeatedly, leave slskd DOWN 15–30 min then cold-start; do NOT restart-spiral. Confirm the new incomplete dir is in effect:
```bash
docker exec slskd sh -c 'echo $SLSKD_INCOMPLETE_DIR' 2>/dev/null || true
```
Expected: `/downloads/incomplete/slskd`.

- [ ] **Step 6: Commit the config change**

```bash
git add docker-compose.yml
git commit -m "feat(qbt-tidy): give slskd its own incomplete dir (SLSKD_INCOMPLETE_DIR)"
```
(Note: `slskd.yml` lives under the untracked config dir — it is not part of the git commit; only `docker-compose.yml` is committed.)

---

## Task 9: Docs + pnpm wrappers

**Files:**
- Modify: `scripts/README.md`, `AGENTS.md`, `package.json`

- [ ] **Step 1: Add the script to `scripts/README.md`**

Add an entry beside the other operational scripts in the same format as its neighbours:

```markdown
### `qbittorrent_settings_enforce.py`

Enforces qBittorrent **Auto Torrent Management** so category tags actually drive
save paths. Sets `auto_tmm_enabled` / `category_changed_tmm_enabled` /
`save_path_changed_tmm_enabled` and points the temp path at
`/downloads/incomplete/qbittorrent`, then flips existing torrents to
auto-managed so qBittorrent relocates them from `complete/manual/` into their
category folders (`complete/sonarr`, `complete/radarr`, …). Same-filesystem
rename — instant, hardlinks preserved, seeding uninterrupted. Idempotent.

Acts by default; `--dry-run` previews the pref diff and relocate plan.

```bash
python scripts/qbittorrent_settings_enforce.py --dry-run
python scripts/qbittorrent_settings_enforce.py
```

Env: `QBITTORRENT_USER`, `QBITTORRENT_PASS`, `QBITTORRENT_HOST`
(default `http://localhost:8080`). Exit: `0` ok/dry-run/no-op, `1` partial,
`2` fatal.
```

- [ ] **Step 2: Add to the `AGENTS.md` operational-scripts list**

```markdown
- `qbittorrent_settings_enforce.py` — enables qBittorrent Auto TMM and flips
  existing torrents to auto-managed so categories drive save paths (relocating
  out of `complete/manual/`). Acts by default; `--dry-run` to preview. Uses
  `QBITTORRENT_USER` / `QBITTORRENT_PASS` / `QBITTORRENT_HOST`.
```

- [ ] **Step 3: Add pnpm wrappers to `package.json`** (after the `nuke:dry` line)

```json
    "qbt:tidy": "bash -c '. .venv/bin/activate && python scripts/qbittorrent_settings_enforce.py'",
    "qbt:tidy:dry": "bash -c '. .venv/bin/activate && python scripts/qbittorrent_settings_enforce.py --dry-run'"
```

- [ ] **Step 4: Verify JSON + references**

Run: `cd /home/tom/nas && node -e "require('./package.json')" && grep -l qbittorrent_settings_enforce scripts/README.md AGENTS.md`
Expected: no JSON error; both doc files match.

- [ ] **Step 5: Commit**

```bash
git add scripts/README.md AGENTS.md package.json
git commit -m "docs(qbt-tidy): document qbittorrent_settings_enforce + pnpm wrappers"
```

---

## Task 10: Final CI-parity gate

**Files:** none

- [ ] **Step 1: Run the gates CI runs**

Run: `. .venv/bin/activate && ruff check scripts/qbittorrent_settings_enforce.py scripts/tests/test_qbittorrent_settings_enforce.py && python scripts/test_scripts.py && pytest -q scripts/tests && cd /home/tom/nas && docker compose config > /dev/null && echo OK`
Expected: ruff clean on the new files, smoke harness exits 0, all pytest pass, compose validates, prints `OK`.

- [ ] **Step 2: Confirm clean tree**

Run: `git status --short`
Expected: clean (all committed).

---

## Self-Review Notes

- **Spec coverage:** Component 1 (script) → Tasks 1–6 (+7 live verify); Component 2 (slskd incomplete) → Task 8; target layout achieved by Task 6 (relocate) + Task 8 (incomplete split); docs → Task 9; gates → Task 10. Pure functions `plan_pref_changes`/`collect_unmanaged_hashes`/`summarize_targets` all defined + tested (Tasks 2–4). Side-findings (stale config tree, cruft categories) correctly left out of scope.
- **Type consistency:** `plan_pref_changes(current, desired)->dict`, `collect_unmanaged_hashes(torrents)->list[str]`, `summarize_targets(torrents, categories)->dict[str,int]` used identically in `main()` (Task 6). `QbtClient` method names (`login`, `get_preferences`, `set_preferences`, `get_torrents`, `get_categories`, `set_auto_management`) match their `main()` call sites. `DESIRED_PREFS` referenced in Task 2 test and Task 6.
- **No placeholders:** every code step shows complete code; every command step shows exact command + expected output.
