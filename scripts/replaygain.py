#!/usr/bin/env python3
"""Compute and write ReplayGain 2.0 (album-aware, EBU R128) tags to the music library.

Background
----------
Without ReplayGain tags, playback volume varies wildly across tracks and albums.
ReplayGain 2.0 (EBU R128 / ITU-R BS.1770-4) normalises perceived loudness so
every track lands at –18 LUFS, while preserving album dynamics for gapless /
continuous-mix listening. This script delegates all loudness measurement and tag
writing to ``rsgain``, a purpose-built C++ CLI that handles mp3, flac, m4a, opus,
ogg, and more.

What this script does
---------------------
1. Discover every *album directory* under the music root (``--music-dir``) that
   contains at least one audio file.  An "album directory" is any leaf folder
   that holds audio — nested structures (Artist / Album) are handled naturally
   because ``rsgain easy`` visits each immediate parent of audio files.
2. In ``--dry-run`` mode (the **default**) print a plan: how many album dirs and
   audio files were found, a sample of paths, and how many were skipped because
   they already carry tags (when ``--skip-existing`` is given).
3. In ``--apply`` mode run ``rsgain easy <music-dir>`` which walks the tree
   recursively, groups files by containing directory, and writes per-track and
   per-album ReplayGain tags in one pass.  Pass ``--jobs N`` to parallelise the
   loudness scan (forwarded as ``rsgain -m N``), and ``--skip-existing`` to skip
   albums that are already fully tagged (``rsgain -s i``).

Prerequisite
------------
``rsgain`` must be installed and on ``$PATH``.  If it is absent the script exits 2
with an actionable install command::

    sudo apt install rsgain

Safety
------
- **Dry-run is the default.** A bare ``python scripts/replaygain.py`` never
  mutates a single file — you must explicitly pass ``--apply``.
- rsgain only writes loudness tags; it never re-encodes or alters audio data.
- ``--skip-existing`` avoids redundant re-scanning (idempotent re-runs on large
  libraries are expensive — ~1 min per 1 000 tracks).
- ``--jobs`` defaults to 1 (safe on spinning HDDs); increase for SSDs.

Exit codes
----------
  0  success (or dry-run / nothing to do)
  1  partial (rsgain exited non-zero on one or more directories)
  2  fatal (rsgain not found, music directory missing, unexpected error)

Environment
-----------
  SHARE_DIRECTORY   Base share path (default: /mnt/drive).
                    Music root resolves to ``$SHARE_DIRECTORY/music`` unless
                    ``--music-dir`` is given.

Usage
-----
  # Dry-run (default) — prints plan, touches nothing
  python scripts/replaygain.py

  # Show plan for a specific directory
  python scripts/replaygain.py --music-dir /mnt/drive/music

  # Write tags (4 parallel workers, skip already-tagged albums)
  python scripts/replaygain.py --apply --jobs 4 --skip-existing

  # Verify plan then apply
  python scripts/replaygain.py --dry-run
  python scripts/replaygain.py --apply
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
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
DEFAULT_JOBS = 1


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunConfig:
    """Resolved, validated configuration for a single invocation."""

    music_dir: Path
    dry_run: bool
    apply: bool
    jobs: int
    skip_existing: bool


@dataclass
class DiscoveryResult:
    """Summary of the album-directory scan."""

    album_dirs: list[Path] = field(default_factory=list)
    audio_file_count: int = 0


# ---------------------------------------------------------------------------
# Pure / testable functions
# ---------------------------------------------------------------------------


def discover_album_dirs(root: Path, audio_exts: frozenset[str]) -> list[Path]:
    """Return every directory under *root* that contains at least one audio file.

    Directories are returned in sorted order (deterministic, aids testing).  The
    root itself is included if it directly contains audio files.

    ``rsgain easy`` already walks recursively, but we enumerate dirs here so the
    dry-run can report counts and sample paths without shelling out.
    """
    seen: set[Path] = set()
    for entry in sorted(root.rglob("*")):
        if entry.is_file() and entry.suffix.lower() in audio_exts:
            parent = entry.parent
            if parent not in seen:
                seen.add(parent)
    return sorted(seen)


def count_audio_files(dirs: list[Path], audio_exts: frozenset[str]) -> int:
    """Count audio files across *dirs* (non-recursive — dirs are already leaf dirs)."""
    total = 0
    for d in dirs:
        for entry in d.iterdir():
            if entry.is_file() and entry.suffix.lower() in audio_exts:
                total += 1
    return total


def build_rsgain_cmd(config: RunConfig) -> list[str]:
    """Build the ``rsgain easy`` command from a :class:`RunConfig`.

    Returns a list suitable for :func:`subprocess.run`.  ``rsgain easy`` runs in
    recursive mode by default and writes per-track + per-album tags in one pass.
    """
    cmd: list[str] = ["rsgain", "easy"]
    if config.jobs > 1:
        cmd += ["-m", str(config.jobs)]
    if config.skip_existing:
        cmd += ["-s", "i"]
    cmd.append(str(config.music_dir))
    return cmd


def summarize_plan(dirs: list[Path], audio_file_count: int, *, sample_n: int = 5) -> str:
    """Return a human-readable dry-run summary string (no I/O)."""
    if not dirs:
        return "No album directories found containing audio files."
    lines: list[str] = [
        f"Found {len(dirs)} album director{'y' if len(dirs) == 1 else 'ies'} "
        f"containing {audio_file_count} audio file(s).",
    ]
    sample = dirs[:sample_n]
    lines.append(f"Sample paths (first {len(sample)}):")
    lines.extend(f"  {d}" for d in sample)
    if len(dirs) > sample_n:
        lines.append(f"  ... and {len(dirs) - sample_n} more.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    share_dir = os.environ.get("SHARE_DIRECTORY", DEFAULT_SHARE_DIRECTORY)
    default_music_dir = str(Path(share_dir) / "music")

    parser = argparse.ArgumentParser(
        description=(
            "Compute and write ReplayGain 2.0 tags to the music library via rsgain. "
            "Dry-run is the default — pass --apply to write tags."
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
        help="(Default) Report which album directories would be processed; do NOT invoke rsgain.",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually run rsgain and write ReplayGain tags.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=DEFAULT_JOBS,
        metavar="N",
        help=f"Parallel worker threads passed to rsgain -m (default {DEFAULT_JOBS}).",
    )
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        default=False,
        help="Skip albums that already have ReplayGain tags (rsgain -s i). Recommended for re-runs.",
    )
    return parser.parse_args(argv)


def _resolve_config(args: argparse.Namespace) -> RunConfig:
    """Translate parsed args into a :class:`RunConfig`, letting --apply override --dry-run."""
    dry_run = not args.apply
    return RunConfig(
        music_dir=Path(args.music_dir),
        dry_run=dry_run,
        apply=args.apply,
        jobs=max(1, args.jobs),
        skip_existing=args.skip_existing,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: C901  (acceptable complexity)
    """Run the ReplayGain tagging workflow; return an exit code."""
    try:
        args = parse_args(argv)
        config = _resolve_config(args)

        # --- prerequisite check ---
        # rsgain is only needed to WRITE tags (--apply). A --dry-run is pure
        # filesystem discovery, so it previews fine without rsgain installed.
        rsgain_present = shutil.which("rsgain") is not None
        if not config.dry_run and not rsgain_present:
            print(
                "ERROR: 'rsgain' not found on PATH.\n"
                "Install it with:  sudo apt install rsgain\n"
                "Then re-run this script.",
                file=sys.stderr,
            )
            return 2

        # --- music dir check ---
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

        # --- discovery ---
        print(f"Scanning {config.music_dir} for album directories…")
        album_dirs = discover_album_dirs(config.music_dir, AUDIO_EXTENSIONS)
        audio_file_count = count_audio_files(album_dirs, AUDIO_EXTENSIONS)
        print(summarize_plan(album_dirs, audio_file_count))

        if not album_dirs:
            return 0

        cmd = build_rsgain_cmd(config)
        print(f"rsgain command: {' '.join(cmd)}")

        # --- dry-run path ---
        if config.dry_run:
            print(
                "\nDRY-RUN: no tags written.  Pass --apply to write ReplayGain 2.0 tags."
            )
            if not rsgain_present:
                print(
                    "NOTE: 'rsgain' is not installed yet — install it before --apply:"
                    "  sudo apt install rsgain"
                )
            return 0

        # --- apply path ---
        print(f"\nApplying ReplayGain tags with {config.jobs} job(s)…")
        result = subprocess.run(cmd, check=False)  # noqa: S603 — controlled input
        if result.returncode != 0:
            print(
                f"WARNING: rsgain exited with code {result.returncode} "
                f"(some files may not have been tagged).",
                file=sys.stderr,
            )
            return 1

        print("Done. ReplayGain 2.0 tags written successfully.")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
