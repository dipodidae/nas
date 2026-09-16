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
  2  fatal (sacad_r not found, music directory missing, unexpected error, or a
     gap-fill batch of 20+ folders in which NOT ONE gained a cover -- the
     measured hit rate is ~80%, so 0% is an outage and cron_job.py must alert)

Environment
-----------
  SHARE_DIRECTORY   Base share path (default: /mnt/drive). Music root resolves
                    to ``$SHARE_DIRECTORY/music`` unless ``--music-dir`` given.

Overwrite-once
--------------
``--overwrite-once`` overwrites each album's cover ONE time with a fresh
sacad-sourced image (``sacad_r -i`` per folder), then drops a hidden
``.album_art_done`` marker so consecutive runs skip that folder forever. New
albums arrive unmarked and get their one pass automatically. ``--limit N``
caps folders per run so the first pass drains over several runs. A cover is
never blanked, and never replaced by a smaller one: the previous file is kept
and put back if the new one is a downgrade.

Two passes, and a memory of misses
----------------------------------
sacad's ``-t`` defaults to **25%**, so a search at ``--size 1000`` silently
DISCARDS every cover below 750px and reports "unable to find cover" — the same
message as an album that genuinely exists on no source. Any folder the primary
pass leaves empty therefore gets a second, relaxed pass at
``--fallback-size``/``--fallback-tolerance`` (500/90). Measured 2026-09-16 on
20 albums that had failed every previous weekly run: 16 of them (80%) had art
after the relaxed pass.

A folder that survives both passes with no art records ``.album_art_none``
holding an attempt count, and is skipped until its cooldown expires (7 days
after the first miss, then 30, then 90). Without that file an unfindable album
re-enters the batch on EVERY run, at the head of a sorted list, and starves the
queue behind it — which is exactly what had been happening: three consecutive
runs each processed 300 folders and marked only 82/116/145 done, while the
backlog grew 1018 → 1296 → 1573.

``--upgrade-below PX`` re-asks for art in folders already marked done whose
cover is narrower than PX *and* whose recorded attempt never asked for
anything that large. The marker records both what was asked for and what was
achieved, so a 400px cover that is 400px because no source has better is
skipped from then on instead of being re-fetched every week.

Usage
-----
  # Dry-run (default) — prints plan, downloads nothing
  python scripts/album_art.py

  # Plan for a specific directory
  python scripts/album_art.py --music-dir /mnt/drive/music

  # Fill only MISSING covers (tree-wide, cheap)
  python scripts/album_art.py --apply

  # Force re-download for ALL albums, every run (overwrite existing covers)
  python scripts/album_art.py --apply --ignore-existing

  # The cron mode: fill gaps, spend each album's one overwrite, and re-ask
  # for anything that settled below 600px
  python scripts/album_art.py --apply --overwrite-once --limit 800 \
      --upgrade-below 600

  # What is left, and why it is left (writes nothing)
  python scripts/album_art.py --overwrite-once --upgrade-below 600
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
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
DEFAULT_MISS_FILENAME = ".album_art_none"
DEFAULT_LIMIT = 800

# Second-chance pass. sacad's -t defaults to 25%, so a run at --size 1000
# DISCARDS every cover below 750px and reports "unable to find cover" -- which
# reads identically to the album genuinely not existing on any source. Measured
# 2026-09-16 on 20 randomly-sampled albums that had failed every weekly run:
# re-running them at 500/90 found art for 16 of them (80%). The fallback only
# ever runs for a folder the primary pass left with no cover at all.
DEFAULT_FALLBACK_SIZE = 500
DEFAULT_FALLBACK_TOLERANCE = 90

# Escalating cooldown before a folder that no source could satisfy is retried,
# indexed by attempt count. A brand-new release often gains art within weeks;
# a 1994 noise demo will not, so it drops to a quarterly retry instead of
# occupying a slot in every single run (see _run_overwrite_once).
MISS_COOLDOWN_DAYS: tuple[int, ...] = (7, 30, 90)


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
    miss_filename: str = DEFAULT_MISS_FILENAME
    fallback_size: int = DEFAULT_FALLBACK_SIZE
    fallback_tolerance: int = DEFAULT_FALLBACK_TOLERANCE
    upgrade_below: int = 0


@dataclass(frozen=True)
class Candidate:
    """One album folder selected for a sacad pass, and why it was selected.

    ``kind`` is one of ``gap`` (no cover at all), ``overwrite`` (has art, has
    never had its one overwrite pass) or ``upgrade`` (marked done, but the
    cover it settled on is below --upgrade-below and the recorded attempt
    never asked for anything that big).
    """

    path: Path
    kind: str
    width: int | None = None


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


def select_batch(items: list, limit: int) -> tuple[list, list]:
    """Return (batch, deferred): the first *limit* items to process this run.

    ``limit <= 0`` disables the cap (process everything now).
    """
    if limit <= 0:
        return list(items), []
    return items[:limit], items[limit:]


def build_overwrite_cmd(
    target_dir: Path,
    size: int,
    cover_filename: str,
    *,
    tolerance: int | None = None,
) -> list[str]:
    """Build a per-folder ``sacad_r -i`` command that force-refreshes one album.

    ``-i`` ignores any existing cover and re-downloads; sacad leaves the
    existing file in place when no source has art, so a cover is never blanked.
    ``tolerance`` maps to ``-t``; sacad's own default is 25 (percent).
    """
    cmd = ["sacad_r", "-i"]
    if tolerance is not None:
        cmd += ["-t", str(tolerance)]
    return [*cmd, str(target_dir), str(size), cover_filename]


# ---------------------------------------------------------------------------
# Sidecar state
#
# Two sidecars, with deliberately different meanings:
#   .album_art_done  -- this folder HAS art and has spent its overwrite pass.
#                       Body records what was asked for and what was achieved,
#                       so an --upgrade-below sweep can tell "400px is all any
#                       source has" from "400px is all we ever asked for".
#   .album_art_none  -- this folder has NO art and every source has been asked.
#                       Body records the attempt count so the retry cooldown
#                       can escalate. Without this file, a permanently
#                       unfindable album re-enters the batch on EVERY run, at
#                       the head of a sorted list, and starves the queue behind
#                       it -- which is what had been happening: the three runs
#                       logged before 2026-09-16 each processed 300 folders and
#                       only marked 82/116/145 of them done, while the unmarked
#                       backlog grew 1018 -> 1296 -> 1573.
# ---------------------------------------------------------------------------


def read_sidecar(path: Path) -> dict:
    """Return a sidecar's parsed body, or ``{}`` when it does not exist.

    A pre-2026-09-16 sidecar holds one line of plain text, not JSON; it parses
    as ``{"v": 1}`` — present, but with nothing recorded about what was asked
    for. That "unknown" is what makes a legacy folder eligible for one
    --upgrade-below re-sweep, after which it carries real numbers.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        return {"v": 1}
    return body if isinstance(body, dict) else {"v": 1}


def write_sidecar(path: Path, payload: dict) -> None:
    """Write a sidecar atomically (temp file -> ``os.replace``).

    AGENTS.md rule: a truncated state file must never be readable as a healthy
    empty one. Here a half-written .album_art_none would parse as v1 and be
    treated as "attempted once", silently resetting the cooldown.
    """
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def cooldown_days(attempts: int) -> int:
    """Days to wait before retrying a folder no source could satisfy."""
    if attempts < 1:
        return 0
    idx = min(attempts, len(MISS_COOLDOWN_DAYS)) - 1
    return MISS_COOLDOWN_DAYS[idx]


def miss_is_cooling(payload: dict, now: datetime) -> bool:
    """True if this folder's last failed search is still inside its cooldown.

    An unparseable or absent timestamp returns False (retry now) rather than
    True: a guard that fails CLOSED here would silently retire an album from
    the queue forever, which is the exact failure this sidecar exists to fix.
    """
    if not payload:
        return False
    stamp = payload.get("ts")
    if not isinstance(stamp, str):
        return False
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    attempts = payload.get("attempts")
    days = cooldown_days(attempts if isinstance(attempts, int) else 1)
    return (now - last).days < days


def cover_width(path: Path) -> int | None:
    """Return a cover's pixel width, or None if it cannot be read.

    Pillow only parses the header here, but this is still one open() per
    album, so callers cache the answer in the sidecar rather than re-reading
    17k images every week.
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        return None
    try:
        with Image.open(path) as img:
            return int(img.size[0])
    except Exception:  # noqa: BLE001 — a corrupt cover is data, not a crash
        return None


def needs_upgrade(marker: dict, width: int | None, upgrade_below: int) -> bool:
    """True if a marked folder's cover is small enough to be worth re-asking.

    Two conditions, and the second is what stops this looping forever: the
    cover must be below the threshold AND the recorded attempt must never have
    asked for anything that large. A folder whose cover is 400px because 400px
    is all Discogs has records ``target: 1000`` and is skipped from then on.
    """
    if upgrade_below <= 0 or width is None or width >= upgrade_below:
        return False
    target = marker.get("target")
    if not isinstance(target, int):
        return True  # legacy marker: never recorded what it asked for
    return target < upgrade_below


def classify_dirs(
    dirs: list[Path],
    *,
    cover_filename: str,
    marker_filename: str,
    miss_filename: str,
    upgrade_below: int,
    now: datetime,
    width_of=cover_width,
    cache_widths: bool = False,
) -> tuple[list[Candidate], dict[str, int]]:
    """Split album dirs into work to do this run and work to skip.

    Returns ``(candidates, counts)``. ``counts`` reports every bucket,
    including the skipped ones, so a run that does nothing says *why*.

    ``cache_widths`` writes a width measured from the image back into the
    folder's marker. Without it an --upgrade-below run re-opens every one of
    the ~15k already-settled covers on every run, forever, to re-derive an
    answer that cannot change. Off during --dry-run, which writes nothing.
    """
    candidates: list[Candidate] = []
    counts = {
        "total": len(dirs),
        "done": 0,
        "gap": 0,
        "overwrite": 0,
        "upgrade": 0,
        "cooling": 0,
    }
    for d in dirs:
        cover = d / cover_filename
        if not cover.exists():
            miss = read_sidecar(d / miss_filename)
            if miss_is_cooling(miss, now):
                counts["cooling"] += 1
                continue
            counts["gap"] += 1
            candidates.append(Candidate(d, "gap"))
            continue
        marker_path = d / marker_filename
        if not marker_path.exists():
            counts["overwrite"] += 1
            candidates.append(Candidate(d, "overwrite"))
            continue
        if upgrade_below > 0:
            marker = read_sidecar(marker_path)
            width = marker.get("w")
            if not isinstance(width, int):
                width = width_of(cover)
                if cache_widths and width is not None:
                    cached = dict(marker)
                    cached["w"] = width
                    # A read-only album folder is not a reason to stop.
                    with contextlib.suppress(OSError):
                        write_sidecar(marker_path, cached)
            if needs_upgrade(marker, width, upgrade_below):
                counts["upgrade"] += 1
                candidates.append(Candidate(d, "upgrade", width))
                continue
        counts["done"] += 1
    return candidates, counts


def summarize_overwrite_plan(
    *,
    counts: dict[str, int],
    n_batch: int,
    n_deferred: int,
    sample: list[Candidate],
    cover_filename: str,
    upgrade_below: int = 0,
    sample_n: int = 5,
) -> str:
    """Human-readable plan summary for --overwrite-once (no I/O).

    Every bucket is printed, including the two skipped ones, so a quiet run
    states which reason made it quiet.
    """
    total = counts.get("total", 0)
    lines: list[str] = [
        f"Found {total} album director{'y' if total == 1 else 'ies'}.",
        f"  {counts.get('done', 0)} settled (have art, overwrite spent).",
        f"  {counts.get('cooling', 0)} searched, no source has art — in retry cooldown.",
        f"  {counts.get('gap', 0)} missing {cover_filename} (gap fill).",
        f"  {counts.get('overwrite', 0)} with art, overwrite pass not yet spent.",
    ]
    if upgrade_below > 0:
        lines.append(
            f"  {counts.get('upgrade', 0)} with art below {upgrade_below}px "
            "never asked for bigger (upgrade)."
        )
    lines.append(
        f"  {n_batch} will be processed this run; {n_deferred} deferred to a later run."
    )
    if sample:
        shown = sample[:sample_n]
        lines.append(f"Sample to process (first {len(shown)}):")
        lines.extend(f"  [{c.kind}] {c.path}" for c in shown)
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
    parser.add_argument(
        "--miss-marker",
        default=DEFAULT_MISS_FILENAME,
        help=(
            f"Sidecar filename recording 'searched, no source has art' "
            f"(default {DEFAULT_MISS_FILENAME}). Carries the attempt count "
            "that drives the escalating retry cooldown."
        ),
    )
    parser.add_argument(
        "--fallback-size",
        type=int,
        default=DEFAULT_FALLBACK_SIZE,
        metavar="PX",
        help=(
            f"Retry size for folders the primary pass left empty "
            f"(default {DEFAULT_FALLBACK_SIZE}; 0 disables the second pass)."
        ),
    )
    parser.add_argument(
        "--fallback-tolerance",
        type=int,
        default=DEFAULT_FALLBACK_TOLERANCE,
        metavar="PCT",
        help=(
            f"sacad -t for the fallback pass (default {DEFAULT_FALLBACK_TOLERANCE}). "
            "sacad's own default of 25 is what makes --size 1000 discard every "
            "cover under 750px."
        ),
    )
    parser.add_argument(
        "--upgrade-below",
        type=int,
        default=0,
        metavar="PX",
        help=(
            "Re-ask for art in folders already marked done whose cover is "
            "narrower than PX and that were never asked for anything that "
            "large. 0 (default) disables. A cover is never replaced by a "
            "smaller one."
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
        miss_filename=args.miss_marker,
        fallback_size=args.fallback_size,
        fallback_tolerance=args.fallback_tolerance,
        upgrade_below=args.upgrade_below,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _fetch_cover(config: RunConfig, target: Path) -> tuple[bool, int, int]:
    """Run the primary sacad pass, then the relaxed one if nothing landed.

    Returns ``(sacad_failed, asked_for, width)``. ``asked_for`` is the largest
    size any pass requested — recorded in the marker so a later
    --upgrade-below sweep knows whether this folder was ever asked for
    something bigger. ``width`` is 0 when no cover exists afterwards.
    """
    cover = target / config.cover_filename
    failed = False
    asked = config.size

    result = subprocess.run(  # noqa: S603 — controlled input
        build_overwrite_cmd(target, config.size, config.cover_filename),
        check=False,
    )
    failed = result.returncode != 0

    if not cover.exists() and config.fallback_size > 0:
        # The primary pass found nothing. Before recording a miss, ask again
        # without the implicit 750px floor that --size 1000 imposes via
        # sacad's default -t 25.
        result = subprocess.run(  # noqa: S603 — controlled input
            build_overwrite_cmd(
                target,
                config.fallback_size,
                config.cover_filename,
                tolerance=config.fallback_tolerance,
            ),
            check=False,
        )
        failed = failed or result.returncode != 0

    if not cover.exists():
        return failed, asked, 0
    return failed, asked, cover_width(cover) or 0


def _process_candidate(config: RunConfig, cand: Candidate, stamp: str) -> tuple[bool, bool]:
    """Fetch art for one album folder, never leaving it worse than it started.

    Returns ``(sacad_failed, has_cover_now)``.
    """
    cover = cand.path / config.cover_filename
    backup: Path | None = None
    before = 0

    if cover.exists():
        # sacad -i overwrites in place and its relaxed fallback pass can land
        # a SMALLER image than the one already here. Keep a copy and put it
        # back if the result is a downgrade -- an upgrade sweep that makes art
        # worse is worse than no sweep.
        before = cover_width(cover) or 0
        backup = cover.with_name(f"{cover.name}.prev")
        try:
            shutil.copy2(cover, backup)
        except OSError:
            backup = None

    failed, asked, after = _fetch_cover(config, cand.path)

    if backup is not None:
        if after and after >= before:
            backup.unlink(missing_ok=True)
        else:
            try:
                os.replace(backup, cover)
                after = before
            except OSError:
                backup.unlink(missing_ok=True)

    miss_path = cand.path / config.miss_filename
    marker_path = cand.path / config.marker_filename

    if cover.exists():
        write_sidecar(
            marker_path,
            {
                "v": 2,
                "ts": stamp,
                "target": asked,
                "w": after or None,
                "cover": config.cover_filename,
                "kind": cand.kind,
            },
        )
        miss_path.unlink(missing_ok=True)
        return failed, True

    prev = read_sidecar(miss_path)
    attempts = prev.get("attempts")
    write_sidecar(
        miss_path,
        {
            "v": 2,
            "ts": stamp,
            "attempts": (attempts if isinstance(attempts, int) else 0) + 1,
            "target": asked,
            "fallback": config.fallback_size,
        },
    )
    return failed, False


def _run_overwrite_once(config: RunConfig, album_dirs: list[Path]) -> int:
    """Fill gaps, spend each album's one overwrite, and re-ask for small art.

    Returns an exit code: 0 success, 1 if any per-folder sacad_r failed, 2 if
    a batch with real gap-fill work in it produced no art at all (that is a
    source or network outage, not a library that happens to be obscure).
    """
    now = datetime.now(UTC)
    stamp = now.isoformat(timespec="seconds")
    candidates, counts = classify_dirs(
        album_dirs,
        cover_filename=config.cover_filename,
        marker_filename=config.marker_filename,
        miss_filename=config.miss_filename,
        upgrade_below=config.upgrade_below,
        now=now,
        cache_widths=not config.dry_run,
    )
    batch, deferred = select_batch(candidates, config.limit)

    print(
        summarize_overwrite_plan(
            counts=counts,
            n_batch=len(batch),
            n_deferred=len(deferred),
            sample=batch,
            cover_filename=config.cover_filename,
            upgrade_below=config.upgrade_below,
        )
    )

    if config.dry_run:
        print(
            "\nDRY-RUN: nothing downloaded. Pass --apply to process the "
            f"{len(batch)} folder(s) above."
        )
        return 0

    if not batch:
        print("\nNothing to do — every album is settled.")
        return 0

    exit_code = 0
    gained = 0
    n_gap = sum(1 for c in batch if c.kind == "gap")
    gap_gained = 0
    print(f"\nProcessing {len(batch)} folder(s) with sacad_r…")
    for cand in batch:
        failed, has_cover = _process_candidate(config, cand, stamp)
        if failed:
            print(
                f"WARNING: sacad_r exited non-zero for {cand.path}",
                file=sys.stderr,
            )
            exit_code = 1
        if has_cover:
            gained += 1
            if cand.kind == "gap":
                gap_gained += 1

    print(
        f"Done. Processed {len(batch)} folder(s), {gained} now have "
        f"{config.cover_filename} ({gap_gained}/{n_gap} of the gap-fills); "
        f"{len(deferred)} deferred to a later run."
    )

    # A gap-fill batch of any size that lands nothing is not an obscure
    # library, it is a broken one: the measured hit rate for gap folders is
    # ~80%. Exit 2 so cron_job.py alerts -- exit 1 is inside its ok-codes.
    if n_gap >= 20 and gap_gained == 0:
        print(
            f"FATAL: {n_gap} gap-fill folders were searched and NONE gained a "
            "cover. Every cover source failing at once is a network or source "
            "outage, not a miss.",
            file=sys.stderr,
        )
        return 2
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
