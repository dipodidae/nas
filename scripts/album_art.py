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
DEFAULT_MARKER_FILENAME = ".album_art_done"
DEFAULT_LIMIT = 300


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
    overwrite_once: bool
    limit: int
    marker_filename: str


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
        overwrite_once=args.overwrite_once,
        limit=args.limit,
        marker_filename=args.marker,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


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

    marker_body = (
        f"album_art.py overwrite-once size={config.size} "
        f"cover={config.cover_filename}\n"
    )
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
        f"Done. Processed {len(batch)} folder(s); "
        f"{len(deferred)} deferred to a later run."
    )
    return exit_code


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

        if not album_dirs:
            print(summarize_plan(album_dirs, [], config.cover_filename))
            return 0

        if config.overwrite_once:
            return _run_overwrite_once(config, album_dirs)

        missing = dirs_missing_cover(album_dirs, config.cover_filename)
        print(summarize_plan(album_dirs, missing, config.cover_filename))

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
