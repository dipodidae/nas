# Album-art Backfill Cron Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A weekly cron that downloads missing `folder.jpg` album covers across the music library using sacad's `sacad_r` CLI, via a host-side wrapper that mirrors `scripts/replaygain.py`.

**Architecture:** `scripts/album_art.py` is a thin, dry-run-default wrapper around `sacad_r`. It resolves the music root from `$SHARE_DIRECTORY/music`, walks the tree to report a plan (album dirs found / already have cover / gaps), and in `--apply` mode shells out to `sacad_r <music-dir> <size> <cover_pattern>` (which natively skips folders that already contain the cover file). A weekly flock-guarded crontab entry runs it with `--apply`.

**Tech Stack:** Python 3.14 (host `.venv`), `sacad==2.8.3` (provides `sacad_r`), pytest, ruff, cron/flock.

## Global Constraints

- Exit codes: `0` success/dry-run/nothing-to-do, `1` partial (sacad_r non-zero), `2` fatal (sacad missing, music dir missing, unexpected) — copied from the repo script contract in `AGENTS.md`.
- Dry-run is the DEFAULT; `--apply` is required to mutate anything.
- Never hard-code paths: music root = `$SHARE_DIRECTORY/music` (default `/mnt/drive`), overridable via `--music-dir`.
- Cover filename convention is `folder.jpg` (matches 6625 existing covers).
- Side effects (subprocess, filesystem writes) centralized in `main()`; pure logic elsewhere for testability.
- Match `scripts/replaygain.py` style (4-space indent, `from __future__ import annotations`, dataclass `RunConfig`).
- CI gates: `ruff check scripts` and `pytest -q scripts/tests` must pass.

---

### Task 1: `scripts/album_art.py` wrapper + unit tests

**Files:**
- Create: `scripts/album_art.py`
- Create: `scripts/tests/test_album_art.py`
- Modify: `scripts/requirements.txt` (add `sacad==2.8.3`)

**Interfaces:**
- Produces:
  - `AUDIO_EXTENSIONS: frozenset[str]`
  - `DEFAULT_SIZE: int = 1000`, `DEFAULT_COVER_FILENAME: str = "folder.jpg"`, `DEFAULT_SHARE_DIRECTORY: str = "/mnt/drive"`
  - `RunConfig(music_dir: Path, dry_run: bool, apply: bool, size: int, cover_filename: str, ignore_existing: bool)` (frozen dataclass)
  - `discover_album_dirs(root: Path, audio_exts: frozenset[str]) -> list[Path]`
  - `dirs_missing_cover(dirs: list[Path], cover_filename: str) -> list[Path]`
  - `build_sacad_cmd(config: RunConfig) -> list[str]` → `["sacad_r", str(music_dir), str(size), cover_filename]` plus `"-i"` when `ignore_existing`
  - `summarize_plan(dirs: list[Path], missing: list[Path], cover_filename: str, *, sample_n: int = 5) -> str`
  - `parse_args(argv) -> argparse.Namespace`, `_resolve_config(args) -> RunConfig`, `main(argv=None) -> int`

- [ ] **Step 1: Add the dependency**

In `scripts/requirements.txt` add a line (keep alphabetical-ish with the rest):

```
sacad==2.8.3
```

- [ ] **Step 2: Write `scripts/album_art.py`**

```python
#!/usr/bin/env python3
"""Download missing external album covers (folder.jpg) for the music library.

Background
----------
Jellyfin shows an album's art from an external image file in the album folder
(``folder.jpg`` by convention here). Lidarr writes one for most albums, but a
few hundred have none. This script delegates cover discovery + download to
``sacad`` (Smart Automatic Cover Art Downloader) via its recursive ``sacad_r``
CLI, which queries Deezer/Discogs/iTunes/Last.fm and writes one image per album
folder. ``sacad_r`` natively skips folders that already contain the target
cover file, so re-runs only do work for the gaps.

What this script does
---------------------
1. Discover every *album directory* under ``--music-dir`` (any directory that
   directly contains audio files).
2. In ``--dry-run`` mode (the **default**) print a plan: album dirs found, how
   many already have the cover file, how many are missing, and a sample of the
   missing paths. Shells out to nothing.
3. In ``--apply`` mode run ``sacad_r <music-dir> <size> <cover_filename>`` which
   walks the tree and downloads a cover into each album dir that lacks one.

Prerequisite
------------
``sacad`` must be installed in the active environment (provides ``sacad_r`` on
PATH). Install with ``pip install sacad`` (pinned in scripts/requirements.txt;
``pnpm py:deps`` refreshes the venv). If absent, --apply exits 2.

Exit codes
----------
  0  success (or dry-run / nothing to do)
  1  partial (sacad_r exited non-zero)
  2  fatal (sacad_r not found, music directory missing, unexpected error)

Environment
-----------
  SHARE_DIRECTORY   Base share path (default: /mnt/drive). Music root resolves
                    to ``$SHARE_DIRECTORY/music`` unless ``--music-dir`` given.

Usage
-----
  # Dry-run (default) — prints plan, downloads nothing
  python scripts/album_art.py

  # Plan for a specific directory
  python scripts/album_art.py --music-dir /mnt/drive/music

  # Download missing covers at 1000px
  python scripts/album_art.py --apply

  # Force re-download for ALL albums (overwrite existing covers)
  python scripts/album_art.py --apply --ignore-existing
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

if "SHARE_DIRECTORY" not in os.environ:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except ImportError:
        pass

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {".mp3", ".flac", ".m4a", ".opus", ".ogg", ".oga", ".aac", ".wav", ".wv"}
)
DEFAULT_SHARE_DIRECTORY = "/mnt/drive"
DEFAULT_SIZE = 1000
DEFAULT_COVER_FILENAME = "folder.jpg"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """Resolved, validated configuration for a single invocation."""

    music_dir: Path
    dry_run: bool
    apply: bool
    size: int
    cover_filename: str
    ignore_existing: bool


# ---------------------------------------------------------------------------
# Pure / testable functions
# ---------------------------------------------------------------------------


def discover_album_dirs(root: Path, audio_exts: frozenset[str]) -> list[Path]:
    """Return every directory under *root* that directly contains an audio file.

    Sorted for determinism. The root itself is included if it directly holds
    audio. ``sacad_r`` walks recursively itself; we enumerate here only so the
    dry-run can report counts without shelling out.
    """
    seen: set[Path] = set()
    for entry in sorted(root.rglob("*")):
        if entry.is_file() and entry.suffix.lower() in audio_exts:
            seen.add(entry.parent)
    return sorted(seen)


def dirs_missing_cover(dirs: list[Path], cover_filename: str) -> list[Path]:
    """Return the subset of *dirs* that do not contain *cover_filename*."""
    return [d for d in dirs if not (d / cover_filename).exists()]


def build_sacad_cmd(config: RunConfig) -> list[str]:
    """Build the ``sacad_r`` command from a :class:`RunConfig`.

    Signature is ``sacad_r lib_dir size cover_pattern``. ``-i`` forces
    re-download even when a cover already exists; omitted by default so existing
    covers are preserved and only gaps are filled.
    """
    cmd: list[str] = ["sacad_r"]
    if config.ignore_existing:
        cmd.append("-i")
    cmd += [str(config.music_dir), str(config.size), config.cover_filename]
    return cmd


def summarize_plan(
    dirs: list[Path],
    missing: list[Path],
    cover_filename: str,
    *,
    sample_n: int = 5,
) -> str:
    """Return a human-readable dry-run summary string (no I/O)."""
    if not dirs:
        return "No album directories found containing audio files."
    have = len(dirs) - len(missing)
    lines: list[str] = [
        f"Found {len(dirs)} album director{'y' if len(dirs) == 1 else 'ies'}.",
        f"  {have} already have {cover_filename}.",
        f"  {len(missing)} missing {cover_filename}.",
    ]
    if missing:
        sample = missing[:sample_n]
        lines.append(f"Sample missing (first {len(sample)}):")
        lines.extend(f"  {d}" for d in sample)
        if len(missing) > sample_n:
            lines.append(f"  ... and {len(missing) - sample_n} more.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    share_dir = os.environ.get("SHARE_DIRECTORY", DEFAULT_SHARE_DIRECTORY)
    default_music_dir = str(Path(share_dir) / "music")

    parser = argparse.ArgumentParser(
        description=(
            "Download missing album covers for the music library via sacad_r. "
            "Dry-run is the default — pass --apply to download."
        )
    )
    parser.add_argument(
        "--music-dir",
        default=default_music_dir,
        help=f"Root of the music library (default: {default_music_dir}).",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="(Default) Report which album dirs are missing covers; download nothing.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually run sacad_r and download missing covers.",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=DEFAULT_SIZE,
        metavar="PX",
        help=f"Target cover size in pixels (default {DEFAULT_SIZE}).",
    )
    parser.add_argument(
        "--filename",
        default=DEFAULT_COVER_FILENAME,
        help=f"Cover filename to write into each album folder (default {DEFAULT_COVER_FILENAME}).",
    )
    parser.add_argument(
        "--ignore-existing",
        action="store_true",
        default=False,
        help="Force re-download for ALL albums, overwriting existing covers (sacad_r -i).",
    )
    return parser.parse_args(argv)


def _resolve_config(args: argparse.Namespace) -> RunConfig:
    """Translate parsed args into a :class:`RunConfig`; --apply overrides --dry-run."""
    return RunConfig(
        music_dir=Path(args.music_dir),
        dry_run=not args.apply,
        apply=args.apply,
        size=args.size,
        cover_filename=args.filename,
        ignore_existing=args.ignore_existing,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the album-art backfill workflow; return an exit code."""
    try:
        args = parse_args(argv)
        config = _resolve_config(args)

        # sacad_r is only needed to DOWNLOAD (--apply). A --dry-run is pure
        # filesystem discovery, so it previews fine without sacad installed.
        sacad_present = shutil.which("sacad_r") is not None
        if not config.dry_run and not sacad_present:
            print(
                "ERROR: 'sacad_r' not found on PATH.\n"
                "Install it with:  pip install sacad   (or: pnpm py:deps)\n"
                "Then re-run this script.",
                file=sys.stderr,
            )
            return 2

        if not config.music_dir.exists():
            print(
                f"ERROR: music directory does not exist: {config.music_dir}\n"
                f"Set SHARE_DIRECTORY in .env or pass --music-dir.",
                file=sys.stderr,
            )
            return 2
        if not config.music_dir.is_dir():
            print(
                f"ERROR: music path is not a directory: {config.music_dir}",
                file=sys.stderr,
            )
            return 2

        print(f"Scanning {config.music_dir} for album directories…")
        album_dirs = discover_album_dirs(config.music_dir, AUDIO_EXTENSIONS)
        missing = dirs_missing_cover(album_dirs, config.cover_filename)
        print(summarize_plan(album_dirs, missing, config.cover_filename))

        if not album_dirs:
            return 0

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

        print("Done. Album covers downloaded.")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Write `scripts/tests/test_album_art.py`**

```python
"""Tests for scripts/album_art.py — pure-logic unit tests + mocked subprocess."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock


def _load_module():
    root = Path(__file__).resolve().parents[2]
    scripts_dir = root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    script_path = scripts_dir / "album_art.py"
    spec = importlib.util.spec_from_file_location("album_art", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module  # type: ignore[attr-defined]
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


aa = _load_module()
AUDIO_EXTS = aa.AUDIO_EXTENSIONS


# --- discover_album_dirs ---


def test_discover_finds_album(tmp_path):
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    (album / "t.flac").touch()
    assert aa.discover_album_dirs(tmp_path, AUDIO_EXTS) == [album]


def test_discover_ignores_non_audio(tmp_path):
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True)
    (d / "folder.jpg").touch()
    (d / "info.txt").touch()
    assert aa.discover_album_dirs(tmp_path, AUDIO_EXTS) == []


# --- dirs_missing_cover ---


def test_dirs_missing_cover_splits(tmp_path):
    have = tmp_path / "Have"
    have.mkdir()
    (have / "folder.jpg").touch()
    miss = tmp_path / "Miss"
    miss.mkdir()
    result = aa.dirs_missing_cover([have, miss], "folder.jpg")
    assert result == [miss]


def test_dirs_missing_cover_all_present(tmp_path):
    d = tmp_path / "A"
    d.mkdir()
    (d / "folder.jpg").touch()
    assert aa.dirs_missing_cover([d], "folder.jpg") == []


def test_dirs_missing_cover_respects_custom_name(tmp_path):
    d = tmp_path / "A"
    d.mkdir()
    (d / "folder.jpg").touch()
    # cover name is cover.jpg -> folder.jpg does not satisfy it
    assert aa.dirs_missing_cover([d], "cover.jpg") == [d]


# --- build_sacad_cmd ---


def test_build_cmd_defaults(tmp_path):
    cfg = aa.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        size=1000,
        cover_filename="folder.jpg",
        ignore_existing=False,
    )
    assert aa.build_sacad_cmd(cfg) == ["sacad_r", str(tmp_path), "1000", "folder.jpg"]


def test_build_cmd_ignore_existing(tmp_path):
    cfg = aa.RunConfig(
        music_dir=tmp_path,
        dry_run=False,
        apply=True,
        size=600,
        cover_filename="cover.jpg",
        ignore_existing=True,
    )
    cmd = aa.build_sacad_cmd(cfg)
    assert cmd == ["sacad_r", "-i", str(tmp_path), "600", "cover.jpg"]


# --- summarize_plan ---


def test_summarize_empty():
    assert "No album" in aa.summarize_plan([], [], "folder.jpg")


def test_summarize_counts(tmp_path):
    dirs = [tmp_path / f"A{i}" for i in range(5)]
    missing = dirs[:2]
    out = aa.summarize_plan(dirs, missing, "folder.jpg")
    assert "5 album" in out
    assert "3 already have folder.jpg" in out
    assert "2 missing folder.jpg" in out


def test_summarize_truncates(tmp_path):
    dirs = [tmp_path / f"A{i:02d}" for i in range(20)]
    out = aa.summarize_plan(dirs, dirs, "folder.jpg", sample_n=5)
    assert "more" in out
    assert str(dirs[0]) in out
    assert str(dirs[10]) not in out


# --- parse_args / _resolve_config ---


def test_dry_run_is_default(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path)]))
    assert cfg.dry_run is True and cfg.apply is False


def test_apply_disables_dry_run(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path), "--apply"]))
    assert cfg.apply is True and cfg.dry_run is False


def test_size_and_filename(tmp_path):
    cfg = aa._resolve_config(
        aa.parse_args(["--music-dir", str(tmp_path), "--size", "600", "--filename", "cover.jpg"])
    )
    assert cfg.size == 600 and cfg.cover_filename == "cover.jpg"


def test_resolve_config_defaults(tmp_path):
    cfg = aa._resolve_config(aa.parse_args(["--music-dir", str(tmp_path)]))
    assert cfg.size == aa.DEFAULT_SIZE
    assert cfg.cover_filename == aa.DEFAULT_COVER_FILENAME
    assert cfg.ignore_existing is False


# --- main: exit codes + side effects (sacad_r mocked) ---


def _album(tmp_path, name, with_cover=False):
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "t.flac").touch()
    if with_cover:
        (d / "folder.jpg").touch()
    return d


def test_main_dry_run_never_calls_sacad(tmp_path):
    _album(tmp_path, "Miss")
    with mock.patch.object(aa.subprocess, "run") as run:
        rc = aa.main(["--music-dir", str(tmp_path)])
    assert rc == 0
    run.assert_not_called()


def test_main_apply_missing_sacad_exits_2(tmp_path):
    _album(tmp_path, "Miss")
    with mock.patch.object(aa.shutil, "which", return_value=None):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 2


def test_main_apply_invokes_sacad_and_maps_success(tmp_path):
    _album(tmp_path, "Miss")
    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=0)) as run,
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 0
    run.assert_called_once()
    assert run.call_args.args[0][0] == "sacad_r"


def test_main_apply_maps_nonzero_to_partial(tmp_path):
    _album(tmp_path, "Miss")
    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run", return_value=mock.Mock(returncode=3)),
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 1


def test_main_apply_nothing_missing_skips_sacad(tmp_path):
    _album(tmp_path, "Have", with_cover=True)
    with (
        mock.patch.object(aa.shutil, "which", return_value="/usr/bin/sacad_r"),
        mock.patch.object(aa.subprocess, "run") as run,
    ):
        rc = aa.main(["--music-dir", str(tmp_path), "--apply"])
    assert rc == 0
    run.assert_not_called()


def test_main_missing_music_dir_exits_2(tmp_path):
    rc = aa.main(["--music-dir", str(tmp_path / "nope"), "--apply"])
    assert rc == 2
```

- [ ] **Step 4: Run the tests, expect PASS**

Run: `. .venv/bin/activate && pytest -q scripts/tests/test_album_art.py`
Expected: all tests pass.

- [ ] **Step 5: Lint**

Run: `. .venv/bin/activate && ruff check scripts/album_art.py scripts/tests/test_album_art.py`
Expected: no errors (fix any, e.g. import order, line length).

- [ ] **Step 6: Smoke-test the real dry-run against the live library**

Run: `. .venv/bin/activate && python scripts/album_art.py 2>&1 | tail -20`
Expected: prints a plan — ~7200 album dirs, ~6625 with folder.jpg, a few hundred missing, plus the `sacad command:` line and the DRY-RUN notice. Downloads nothing.

- [ ] **Step 7: Commit**

```bash
git add scripts/album_art.py scripts/tests/test_album_art.py scripts/requirements.txt
git commit -m "feat(scripts): album_art.py — sacad cover backfill wrapper (dry-run default)"
```

---

### Task 2: Docs + crontab entry

**Files:**
- Modify: `scripts/README.md` (add an `album_art.py` section)
- Modify: `AGENTS.md` (mention in the scripts list; note no new env var)
- Install: one new user crontab line (not a repo file)

- [ ] **Step 1: Document in `scripts/README.md`**

Add a section near `replaygain.py` describing purpose, the dry-run default, flags (`--music-dir`, `--apply`, `--size`, `--filename`, `--ignore-existing`), exit codes (0/1/2), and the weekly cron. Use the existing entries' formatting.

- [ ] **Step 2: Mention in `AGENTS.md`**

Add `album_art.py` to the scripts inventory with a one-line description; note it reuses `SHARE_DIRECTORY` and needs no new `.env` key.

- [ ] **Step 3: Commit docs**

```bash
git add scripts/README.md AGENTS.md
git commit -m "docs: document album_art.py + weekly cover-backfill cron"
```

- [ ] **Step 4: Install the crontab entry**

Append (preserving existing entries via `crontab -l`):

```cron
# Sunday 04:45 — backfill missing folder.jpg album covers (sacad, incremental:
# skips albums that already have one). After 04:30 post-update verifier, clear
# of the heavy hourly Tubifarry/slskd hygiene jobs.
45 4 * * 0 /usr/bin/flock -n /tmp/nas-album-art.lock /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/album_art.py --apply >> logs/album_art.log 2>&1"
```

- [ ] **Step 5: Verify cron installed**

Run: `crontab -l | grep album_art`
Expected: the new line is present.

---

## Self-Review

- **Spec coverage:** external folder.jpg (Task 1 default), .venv install (Task 1 Step 1), wrapper mirroring replaygain (Task 1), 1000px default (`DEFAULT_SIZE`), weekly cron (Task 2 Step 4), tests (Task 1 Step 3), README + AGENTS (Task 2). All covered.
- **Placeholders:** none — all code is complete.
- **Type consistency:** `RunConfig` fields and function names used in tests match the definitions in `album_art.py`. `build_sacad_cmd` argv order matches the verified `sacad_r lib_dir size cover_pattern` signature.
