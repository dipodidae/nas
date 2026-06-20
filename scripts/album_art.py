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

        print(
            "Done. sacad_r finished — see per-album results above "
            "(albums with no cover on any source are left untouched)."
        )
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
