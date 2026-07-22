# Album-art overwrite-once Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the weekly `album_art.py` cron overwrite each album folder's cover exactly once with a better sacad-sourced image, then never touch that folder again — while never blanking an existing cover.

**Architecture:** A new opt-in `--overwrite-once` mode partitions album dirs by a hidden `.album_art_done` sidecar marker, runs `sacad_r -i` per unmarked folder (capped at `--limit`/run), and writes the marker iff a cover exists after the attempt. Unfillable gaps stay unmarked and retry like today; everything else freezes after one pass. Plain `--apply` gap-fill and `--dry-run` behaviour are unchanged.

**Tech Stack:** Python 3.11+ stdlib (argparse, subprocess, pathlib, dataclasses), `sacad_r` CLI, pytest.

## Global Constraints

- Script contract (from `AGENTS.md`): exit `0` success / `1` partial / `2` fatal; side effects only in `main()`, pure logic in standalone functions for testability.
- No new env vars (spec: "Out of scope"). Do **not** touch `.env` / `.env.example` / `AGENTS.md` env list.
- Dry-run stays the default; must run without `sacad_r` installed (pure filesystem preview).
- Never blank a cover: overwrite is always `sacad_r -i` per-folder (sacad leaves the file untouched when no source is found).
- Marker filename constant: `.album_art_done`. Cover filename default: `folder.jpg`.
- `--limit` default: `300`. A value `<= 0` means no cap.
- Determinism: `discover_album_dirs` already returns sorted dirs; preserve order through partition/batch so the drip is stable across runs.
- Run linters before commits: `ruff check scripts` must pass.

---

### Task 1: Config & CLI plumbing

Add the marker constant, the three new flags, and the matching `RunConfig` fields. No behaviour change yet — just plumbing that later tasks consume.

**Files:**
- Modify: `scripts/album_art.py` (constants ~line 77-79; `RunConfig` ~line 87-96; `parse_args` ~line 167-213; `_resolve_config` ~line 216-225)
- Test: `scripts/tests/test_album_art.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `DEFAULT_MARKER_FILENAME: str = ".album_art_done"`
  - `DEFAULT_LIMIT: int = 300`
  - `RunConfig` gains fields `overwrite_once: bool`, `limit: int`, `marker_filename: str`
  - CLI flags `--overwrite-once` (store_true, default False), `--limit PX-style int` (default `DEFAULT_LIMIT`), `--marker` (default `DEFAULT_MARKER_FILENAME`)

- [ ] **Step 1: Write the failing tests**

Add to `scripts/tests/test_album_art.py` (append near the other `_resolve_config`/arg tests):

```python
# --- overwrite-once config plumbing ---


def test_overwrite_once_defaults_off(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path)]))
    assert cfg.overwrite_once is False
    assert cfg.limit == aa.DEFAULT_LIMIT
    assert cfg.marker_filename == aa.DEFAULT_MARKER_FILENAME


def test_overwrite_once_flag_and_limit(tmp_path):
    cfg = aa._resolve_config(
        aa.parse_args(
            ["--music-dir", str(tmp_path), "--apply", "--overwrite-once", "--limit", "50"]
        )
    )
    assert cfg.overwrite_once is True
    assert cfg.apply is True
    assert cfg.limit == 50


def test_marker_override(tmp_path):
    cfg = aa._resolve_config(
        aa.parse_args(["--music-dir", str(tmp_path), "--marker", ".done"])
    )
    assert cfg.marker_filename == ".done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `. .venv/bin/activate && pytest scripts/tests/test_album_art.py -k "overwrite_once_defaults or overwrite_once_flag or marker_override" -v`
Expected: FAIL — `AttributeError: module 'album_art' has no attribute 'DEFAULT_MARKER_FILENAME'` (and unknown args).

- [ ] **Step 3: Add the constants**

In `scripts/album_art.py`, next to the existing defaults (after line 79):

```python
DEFAULT_COVER_FILENAME = "folder.jpg"
DEFAULT_MARKER_FILENAME = ".album_art_done"
DEFAULT_LIMIT = 300
```

- [ ] **Step 4: Extend `RunConfig`**

Add three fields to the frozen dataclass (after `ignore_existing: bool`):

```python
    ignore_existing: bool
    overwrite_once: bool
    limit: int
    marker_filename: str
```

- [ ] **Step 5: Add the CLI flags**

In `parse_args`, after the `--ignore-existing` argument block, add:

```python
    parser.add_argument(
        "--overwrite-once",
        action="store_true",
        default=False,
        help=(
            "Overwrite each album's cover ONCE (sacad_r -i per folder), then "
            "mark it done so consecutive runs skip it. Requires --apply."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        metavar="N",
        help=(
            f"Max album folders to process per --overwrite-once run "
            f"(default {DEFAULT_LIMIT}; <=0 means no cap)."
        ),
    )
    parser.add_argument(
        "--marker",
        default=DEFAULT_MARKER_FILENAME,
        help=(
            f"Sidecar filename marking a folder as already overwritten "
            f"(default {DEFAULT_MARKER_FILENAME})."
        ),
    )
```

- [ ] **Step 6: Wire fields in `_resolve_config`**

```python
    return RunConfig(
        music_dir=Path(args.music_dir),
        dry_run=not args.apply,
        apply=args.apply,
        size=args.size,
        cover_filename=args.filename,
        ignore_existing=args.ignore_existing,
        overwrite_once=args.overwrite_once,
        limit=args.limit,
        marker_filename=args.marker,
    )
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest scripts/tests/test_album_art.py -v && ruff check scripts`
Expected: PASS (new tests green, existing tests still green, ruff clean).

Note: existing tests build `RunConfig(...)` directly (e.g. `test_build_cmd_defaults`). Adding required dataclass fields will break them. Fix each direct `RunConfig(...)` construction in the test file by adding `overwrite_once=False, limit=aa.DEFAULT_LIMIT, marker_filename=aa.DEFAULT_MARKER_FILENAME`. Re-run until green.

- [ ] **Step 8: Commit**

```bash
git add scripts/album_art.py scripts/tests/test_album_art.py
git commit -m "feat(album_art): add --overwrite-once / --limit / --marker plumbing"
```

---

### Task 2: Pure helpers

Add the standalone, side-effect-free functions the `main()` loop will use. All independently unit-tested.

**Files:**
- Modify: `scripts/album_art.py` (add functions in the "Pure / testable functions" section, after `summarize_plan`)
- Test: `scripts/tests/test_album_art.py`

**Interfaces:**
- Consumes: `Path`, `dirs_missing_cover` (existing).
- Produces:
  - `dir_is_marked(d: Path, marker_filename: str) -> bool`
  - `partition_by_marker(dirs: list[Path], marker_filename: str) -> tuple[list[Path], list[Path]]` → `(marked, unmarked)`, order preserved.
  - `select_batch(unmarked: list[Path], limit: int) -> tuple[list[Path], list[Path]]` → `(batch, deferred)`; `limit <= 0` → `(unmarked, [])`.
  - `build_overwrite_cmd(target_dir: Path, size: int, cover_filename: str) -> list[str]` → `["sacad_r", "-i", str(target_dir), str(size), cover_filename]`.
  - `summarize_overwrite_plan(*, total: int, n_marked: int, n_overwrite: int, n_gap: int, n_batch: int, n_deferred: int, sample: list[Path], cover_filename: str) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_album_art.py`:

```python
# --- overwrite-once pure helpers ---


def test_dir_is_marked(tmp_path):
    d = tmp_path / "album"
    d.mkdir()
    assert aa.dir_is_marked(d, ".album_art_done") is False
    (d / ".album_art_done").touch()
    assert aa.dir_is_marked(d, ".album_art_done") is True


def test_partition_by_marker(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / ".album_art_done").touch()
    marked, unmarked = aa.partition_by_marker([a, b], ".album_art_done")
    assert marked == [a]
    assert unmarked == [b]


def test_select_batch_caps(tmp_path):
    dirs = [tmp_path / str(i) for i in range(5)]
    batch, deferred = aa.select_batch(dirs, 2)
    assert batch == dirs[:2]
    assert deferred == dirs[2:]


def test_select_batch_no_cap(tmp_path):
    dirs = [tmp_path / str(i) for i in range(3)]
    assert aa.select_batch(dirs, 0) == (dirs, [])
    assert aa.select_batch(dirs, -1) == (dirs, [])


def test_build_overwrite_cmd(tmp_path):
    assert aa.build_overwrite_cmd(tmp_path, 1000, "folder.jpg") == [
        "sacad_r",
        "-i",
        str(tmp_path),
        "1000",
        "folder.jpg",
    ]


def test_summarize_overwrite_plan_counts(tmp_path):
    out = aa.summarize_overwrite_plan(
        total=10,
        n_marked=4,
        n_overwrite=3,
        n_gap=3,
        n_batch=5,
        n_deferred=1,
        sample=[tmp_path / "x"],
        cover_filename="folder.jpg",
    )
    assert "10" in out and "4" in out  # total + marked
    assert "overwrite" in out.lower()
    assert "defer" in out.lower()
    assert str(tmp_path / "x") in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `. .venv/bin/activate && pytest scripts/tests/test_album_art.py -k "marked or select_batch or overwrite_cmd or summarize_overwrite" -v`
Expected: FAIL — `AttributeError: module 'album_art' has no attribute 'dir_is_marked'`.

- [ ] **Step 3: Implement the helpers**

In `scripts/album_art.py`, after `summarize_plan` (before the "Argument parsing" divider):

```python
def dir_is_marked(d: Path, marker_filename: str) -> bool:
    """True if *d* already carries the overwrite-once sidecar marker."""
    return (d / marker_filename).exists()


def partition_by_marker(
    dirs: list[Path], marker_filename: str
) -> tuple[list[Path], list[Path]]:
    """Split *dirs* into (marked, unmarked), preserving input order.

    Marked dirs have already had their one overwrite and are skipped forever.
    """
    marked: list[Path] = []
    unmarked: list[Path] = []
    for d in dirs:
        (marked if dir_is_marked(d, marker_filename) else unmarked).append(d)
    return marked, unmarked


def select_batch(unmarked: list[Path], limit: int) -> tuple[list[Path], list[Path]]:
    """Return (batch, deferred): the first *limit* dirs to process this run.

    ``limit <= 0`` disables the cap (process everything now).
    """
    if limit <= 0:
        return list(unmarked), []
    return unmarked[:limit], unmarked[limit:]


def build_overwrite_cmd(target_dir: Path, size: int, cover_filename: str) -> list[str]:
    """Build a per-folder ``sacad_r -i`` command that force-refreshes one album.

    ``-i`` ignores any existing cover and re-downloads; sacad leaves the
    existing file in place when no source has art, so a cover is never blanked.
    """
    return ["sacad_r", "-i", str(target_dir), str(size), cover_filename]


def summarize_overwrite_plan(
    *,
    total: int,
    n_marked: int,
    n_overwrite: int,
    n_gap: int,
    n_batch: int,
    n_deferred: int,
    sample: list[Path],
    cover_filename: str,
    sample_n: int = 5,
) -> str:
    """Human-readable dry-run summary for --overwrite-once (no I/O)."""
    lines: list[str] = [
        f"Found {total} album director{'y' if total == 1 else 'ies'}.",
        f"  {n_marked} already marked done (skip).",
        f"  {n_overwrite} unmarked with existing art (overwrite once).",
        f"  {n_gap} unmarked missing {cover_filename} (gap fill).",
        f"  {n_batch} will be processed this run; {n_deferred} deferred to a later run.",
    ]
    if sample:
        shown = sample[:sample_n]
        lines.append(f"Sample to process (first {len(shown)}):")
        lines.extend(f"  {d}" for d in shown)
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest scripts/tests/test_album_art.py -v && ruff check scripts`
Expected: PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add scripts/album_art.py scripts/tests/test_album_art.py
git commit -m "feat(album_art): pure helpers for overwrite-once partitioning + batching"
```

---

### Task 3: Wire overwrite-once into `main()`

Add the overwrite-once branch: partition, report, and (in apply mode) run `sacad_r -i` per batched folder, writing the marker iff a cover exists afterward. Downgrade exit to 1 if any per-folder run fails.

**Files:**
- Modify: `scripts/album_art.py` (`main()` ~line 233-306)
- Test: `scripts/tests/test_album_art.py`

**Interfaces:**
- Consumes: `partition_by_marker`, `dirs_missing_cover`, `select_batch`, `build_overwrite_cmd`, `summarize_overwrite_plan` (Task 2); `RunConfig.overwrite_once/limit/marker_filename` (Task 1).
- Produces: no new public functions — behaviour lives in `main()`.

- [ ] **Step 1: Write the failing tests**

Append to `scripts/tests/test_album_art.py`. Helper to build a fake library:

```python
# --- overwrite-once main() behaviour ---


def _make_album(tmp_path, name, *, cover=False, marker=False):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "track.flac").touch()
    if cover:
        (d / "folder.jpg").write_bytes(b"old")
    if marker:
        (d / ".album_art_done").touch()
    return d


def test_overwrite_once_skips_marked(tmp_path):
    done = _make_album(tmp_path, "Marked", cover=True, marker=True)
    fresh = _make_album(tmp_path, "Fresh", cover=True)

    def fake_run(cmd, check=False):
        target = Path(cmd[2])
        (target / "folder.jpg").write_bytes(b"new")
        return mock.Mock(returncode=0)

    with mock.patch("shutil.which", return_value="/usr/bin/sacad_r"), mock.patch.object(
        aa.subprocess, "run", side_effect=fake_run
    ) as run:
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 0
    # Only the unmarked album is processed
    called_dirs = {Path(c.args[0][2]) for c in run.call_args_list}
    assert called_dirs == {fresh}
    # Both end up marked (marked one already was; fresh one now is)
    assert (done / ".album_art_done").exists()
    assert (fresh / ".album_art_done").exists()


def test_overwrite_once_no_source_keeps_art_and_marks(tmp_path):
    album = _make_album(tmp_path, "BadArt", cover=True)  # sacad finds nothing

    with mock.patch("shutil.which", return_value="/usr/bin/sacad_r"), mock.patch.object(
        aa.subprocess, "run", return_value=mock.Mock(returncode=0)
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 0
    assert (album / "folder.jpg").read_bytes() == b"old"  # never blanked
    assert (album / ".album_art_done").exists()  # attempt spent -> marked


def test_overwrite_once_unfilled_gap_not_marked(tmp_path):
    gap = _make_album(tmp_path, "Obscure")  # no cover, sacad finds nothing

    with mock.patch("shutil.which", return_value="/usr/bin/sacad_r"), mock.patch.object(
        aa.subprocess, "run", return_value=mock.Mock(returncode=0)
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 0
    assert not (gap / "folder.jpg").exists()
    assert not (gap / ".album_art_done").exists()  # stays unmarked -> retried


def test_overwrite_once_limit_bounds_calls(tmp_path):
    for i in range(4):
        _make_album(tmp_path, f"A{i}", cover=True)

    with mock.patch("shutil.which", return_value="/usr/bin/sacad_r"), mock.patch.object(
        aa.subprocess, "run", return_value=mock.Mock(returncode=0)
    ) as run:
        rc = aa.main(
            ["--music-dir", str(tmp_path), "--apply", "--overwrite-once", "--limit", "2"]
        )

    assert rc == 0
    assert run.call_count == 2


def test_overwrite_once_partial_exit_on_failure(tmp_path):
    _make_album(tmp_path, "A", cover=True)

    with mock.patch("shutil.which", return_value="/usr/bin/sacad_r"), mock.patch.object(
        aa.subprocess, "run", return_value=mock.Mock(returncode=3)
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply", "--overwrite-once"])

    assert rc == 1  # per-folder sacad_r non-zero -> partial


def test_overwrite_once_dry_run_no_calls(tmp_path):
    _make_album(tmp_path, "A", cover=True)
    with mock.patch.object(aa.subprocess, "run") as run:
        rc = aa.main(["--music-dir", str(tmp_path), "--overwrite-once"])
    assert rc == 0
    run.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `. .venv/bin/activate && pytest scripts/tests/test_album_art.py -k "overwrite_once_skips or no_source or unfilled_gap or limit_bounds or partial_exit or dry_run_no_calls" -v`
Expected: FAIL — current `main()` ignores `overwrite_once`, so it runs the single tree-wide command (wrong call count / no markers).

- [ ] **Step 3: Add the overwrite-once branch in `main()`**

In `scripts/album_art.py`, inside `main()`, replace the block that starts at `cmd = build_sacad_cmd(config)` (line ~273) down to the final `return 0` of the apply path (line ~306) with a branch. Insert the overwrite-once handling **before** the existing gap-fill logic, right after `print(summarize_plan(...))` and the `if not album_dirs: return 0` guard:

```python
        if not album_dirs:
            return 0

        if config.overwrite_once:
            return _run_overwrite_once(config, album_dirs)

        # --- existing tree-wide gap-fill path (unchanged) ---
        cmd = build_sacad_cmd(config)
        print(f"sacad command: {' '.join(cmd)}")

        if config.dry_run:
            print(
                f"\nDRY-RUN: nothing downloaded. Pass --apply to fetch {config.cover_filename} "
                f"for the missing album(s)."
            )
            if not sacad_present:
                print(
                    "NOTE: 'sacad_r' is not installed yet — install before --apply: "
                    "pip install sacad"
                )
            return 0

        if not missing and not config.ignore_existing:
            print("\nNothing to do — every album already has a cover.")
            return 0

        print("\nDownloading missing covers with sacad_r…")
        result = subprocess.run(cmd, check=False)  # noqa: S603 — controlled input
        if result.returncode != 0:
            print(
                f"WARNING: sacad_r exited with code {result.returncode} "
                f"(some covers may not have been downloaded).",
                file=sys.stderr,
            )
            return 1

        print(
            "Done. sacad_r finished — see per-album results above "
            "(albums with no cover on any source are left untouched)."
        )
        return 0
```

- [ ] **Step 4: Add the `_run_overwrite_once` helper**

Add this function just above `main()` (side effects live here, so it sits with `main`, not in the pure section):

```python
def _run_overwrite_once(config: RunConfig, album_dirs: list[Path]) -> int:
    """Overwrite each unmarked album's cover once, then mark it done.

    Returns an exit code (0 success, 1 if any per-folder sacad_r failed).
    """
    marked, unmarked = partition_by_marker(album_dirs, config.marker_filename)
    gap = dirs_missing_cover(unmarked, config.cover_filename)
    n_gap = len(gap)
    n_overwrite = len(unmarked) - n_gap
    batch, deferred = select_batch(unmarked, config.limit)

    print(
        summarize_overwrite_plan(
            total=len(album_dirs),
            n_marked=len(marked),
            n_overwrite=n_overwrite,
            n_gap=n_gap,
            n_batch=len(batch),
            n_deferred=len(deferred),
            sample=batch,
            cover_filename=config.cover_filename,
        )
    )

    if config.dry_run:
        print(
            "\nDRY-RUN: nothing downloaded. Pass --apply to overwrite the "
            f"{len(batch)} folder(s) above."
        )
        return 0

    if not batch:
        print("\nNothing to do — every album is already marked done.")
        return 0

    marker_body = f"album_art.py overwrite-once size={config.size} cover={config.cover_filename}\n"
    exit_code = 0
    print(f"\nOverwriting covers for {len(batch)} folder(s) with sacad_r -i…")
    for d in batch:
        cmd = build_overwrite_cmd(d, config.size, config.cover_filename)
        result = subprocess.run(cmd, check=False)  # noqa: S603 — controlled input
        if result.returncode != 0:
            print(
                f"WARNING: sacad_r exited {result.returncode} for {d}",
                file=sys.stderr,
            )
            exit_code = 1
        # Mark done iff a cover now exists (overwritten OR pre-existing art kept).
        # A still-empty gap stays unmarked so future runs retry it.
        if (d / config.cover_filename).exists():
            (d / config.marker_filename).write_text(marker_body)

    print(
        f"Done. Processed {len(batch)} folder(s); {len(deferred)} deferred to a later run."
    )
    return exit_code
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `. .venv/bin/activate && pytest scripts/tests/test_album_art.py -v && ruff check scripts`
Expected: PASS (all tests), ruff clean.

- [ ] **Step 6: Run the full script test gates**

Run: `. .venv/bin/activate && pytest -q scripts/tests && python scripts/test_scripts.py && ruff check scripts`
Expected: all green (matches CI).

- [ ] **Step 7: Manual dry-run smoke against the real library**

Run: `. .venv/bin/activate && python scripts/album_art.py --overwrite-once --music-dir /mnt/drive/music`
Expected: prints total / marked / overwrite / gap / batch(≤300) / deferred counts and a sample; shells out to nothing; exit 0.

- [ ] **Step 8: Commit**

```bash
git add scripts/album_art.py scripts/tests/test_album_art.py
git commit -m "feat(album_art): implement per-folder overwrite-once with marker gating"
```

---

### Task 4: Docs + cron switch

Update the module docstring, `scripts/README.md`, and the crontab entry. No code change.

**Files:**
- Modify: `scripts/album_art.py` (module docstring, lines ~40-54)
- Modify: `scripts/README.md` (album-art section)
- Modify: crontab (via `crontab -e` — manual, documented here)

**Interfaces:** none.

- [ ] **Step 1: Update the module docstring usage block**

In `scripts/album_art.py`, replace the `Usage` examples (lines ~41-53) to add overwrite-once and document the marker:

```python
Overwrite-once
--------------
``--overwrite-once`` overwrites each album's cover ONE time with a fresh
sacad-sourced image (``sacad_r -i`` per folder), then drops a hidden
``.album_art_done`` marker so consecutive runs skip that folder forever. New
albums arrive unmarked and get their one pass automatically. ``--limit N``
(default 300) caps folders per run so the first pass drains over several runs.
A cover is never blanked: sacad leaves existing art in place when no source
has a replacement. Folders that end a run with no cover stay unmarked and are
retried next run (same as plain gap-fill).

Usage
-----
  # Dry-run (default) — prints plan, downloads nothing
  python scripts/album_art.py

  # Fill only MISSING covers (tree-wide, cheap)
  python scripts/album_art.py --apply

  # Overwrite each album's cover once, then never again (the cron mode)
  python scripts/album_art.py --apply --overwrite-once --limit 300
```

- [ ] **Step 2: Update `scripts/README.md`**

Find the `album_art.py` entry and add a bullet describing `--overwrite-once`, `--limit` (default 300), `--marker` (`.album_art_done`), and the drip/never-blank semantics. Match the existing formatting of neighbouring script entries. Run `git log --oneline -- scripts/README.md | head` if unsure of the section style; keep flags and exit codes consistent with the rest of the file.

- [ ] **Step 3: Commit the docs**

```bash
git add scripts/album_art.py scripts/README.md
git commit -m "docs(album_art): document overwrite-once mode + marker semantics"
```

- [ ] **Step 4: Update the crontab**

The current entry (verify with `crontab -l | grep album_art`):

```
45 4 * * 0 /usr/bin/flock -n /tmp/nas-album-art.lock /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/album_art.py --apply >> logs/album_art.log 2>&1"
```

Replace with (run `crontab -e`, edit the one line):

```
45 4 * * 0 /usr/bin/flock -n /tmp/nas-album-art.lock /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/album_art.py --apply --overwrite-once --limit 300 >> logs/album_art.log 2>&1"
```

Verify: `crontab -l | grep album_art` shows the new flags. flock `-n` already prevents overlapping runs, so a long first batch can't collide with the next weekly trigger.

- [ ] **Step 5: Final verification**

Run: `. .venv/bin/activate && pytest -q scripts/tests && ruff check scripts && git status`
Expected: tests green, ruff clean, working tree clean (all committed except the crontab, which lives outside git).

---

## Self-Review

**Spec coverage:**
- Per-folder ongoing overwrite → Task 3 (`_run_overwrite_once` loop + marker). ✓
- Hidden `.album_art_done` marker → Task 1 constant, Task 3 write. ✓
- Never blank (per-folder `sacad_r -i`) → Task 2 `build_overwrite_cmd`, Task 3. ✓
- Mark iff cover exists after attempt → Task 3 step 4; tests `no_source_keeps_art_and_marks` + `unfilled_gap_not_marked`. ✓
- `--limit` default 300, `<=0` no cap → Task 1 + Task 2 `select_batch`; test `limit_bounds_calls`. ✓
- Dry-run reporting → Task 2 `summarize_overwrite_plan`, Task 3 dry-run branch; test `dry_run_no_calls`. ✓
- Backward-compat plain `--apply` unchanged → Task 3 keeps the existing branch; existing tests still assert it. ✓
- Exit codes 0/1/2 → Task 3 test `partial_exit_on_failure`; fatal paths untouched. ✓
- Docs (docstring + README) + cron → Task 4. ✓
- No new env var → respected (Global Constraints). ✓

**Placeholder scan:** none — every code step shows full code; README step references existing file conventions rather than inventing copy (acceptable, formatting-only).

**Type consistency:** `partition_by_marker` returns `(marked, unmarked)`; `select_batch` returns `(batch, deferred)`; `build_overwrite_cmd` shape matches existing `build_sacad_cmd` (`["sacad_r", "-i", dir, size, filename]`); `summarize_overwrite_plan` kwargs match the call site in `_run_overwrite_once`. All consistent across tasks.
