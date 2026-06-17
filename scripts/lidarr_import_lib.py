#!/usr/bin/env python3
"""Shared, pure import-acceptance policy for Lidarr manual-import.

Both ``process_soulseek_imports.py`` (orphan-folder importer) and
``lidarr_queue_unstick.py`` (importFailed queue drainer) decide whether a
Soulseek grab that Lidarr refused to auto-import is nonetheless *good enough*
to import via the manual-import API (with release switching enabled). This
module is the single source of truth for that decision so the two agree.

Everything here is pure — it operates on the dicts Lidarr's
``GET /api/v1/manualimport`` returns (``file_info`` / ``entry``) or on plain
rejection-reason strings, and performs no I/O. That keeps the policy unit
testable in isolation (see ``scripts/tests/test_lidarr_import_lib.py``) and
matches the repo's "pure logic separate from side effects" contract.
"""

from __future__ import annotations

import re
from typing import Any

# Match-quality percentage inside a "not close enough: X % vs 80 %" reason.
NOT_CLOSE_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")

# Rejections that importing what's already on disk can never satisfy.
ALWAYS_BLOCKERS: tuple[str, ...] = (
    "not an upgrade",
    "couldn't find similar",
    "destination already exists",
)

# Default confidence floor for "album match is not close enough".
DEFAULT_ACCEPT_MIN_MATCH = 70.0


def classify_reasons(
    reasons: list[str],
    *,
    accept_min_match: float = DEFAULT_ACCEPT_MIN_MATCH,
    accept_missing_tracks: bool = True,
    block_fewer_tracks: bool = False,
) -> tuple[bool, list[str]]:
    """Decide whether a set of Lidarr rejection reasons is salvageable.

    Returns ``(acceptable, blockers)`` where ``acceptable`` is True when no
    blocking reason is present and ``blockers`` lists the reasons that forced
    rejection.

    Accepted (never block):
      - "album release not requested"  (edition mismatch — fixed by release switch)
      - "has unmatched tracks"          (extra files are harmless)
      - "not close enough: X %"         when ``X >= accept_min_match``
      - "has missing tracks"            only when ``accept_missing_tracks``

    Blocked:
      - "not close enough: X %"         when ``X < accept_min_match``
      - "has missing tracks"            when not ``accept_missing_tracks``
      - "has fewer tracks than existing" when ``block_fewer_tracks``
      - any of ``ALWAYS_BLOCKERS``      (not-upgrade / no-similar / dest-exists)

    Unknown reasons are treated as non-blocking (conservative toward importing
    what we already paid to download); the track-file-delta check downstream is
    the real backstop against a no-op import clearing a row.
    """
    blockers: list[str] = []
    for reason in reasons:
        lower = reason.lower()

        if "not close enough" in lower:
            match = NOT_CLOSE_PCT_RE.search(reason)
            actual_pct = float(match.group(1)) if match else 0.0
            if actual_pct < accept_min_match:
                blockers.append(reason)
            continue

        if "missing tracks" in lower:
            if not accept_missing_tracks:
                blockers.append(reason)
            continue

        if "unmatched tracks" in lower:
            continue

        if "album release not requested" in lower:
            continue

        if block_fewer_tracks and "fewer tracks than existing" in lower:
            blockers.append(reason)
            continue

        if any(b in lower for b in ALWAYS_BLOCKERS):
            blockers.append(reason)

    return (not blockers, blockers)


def build_import_item(file_info: dict[str, Any]) -> dict[str, Any] | None:
    """Turn a ``/manualimport`` scan entry into a ``ManualImport`` payload item.

    Returns ``None`` when the entry lacks the artist/album/track ids needed to
    import. ``disableReleaseSwitching: False`` is the whole point — it lets
    Lidarr re-point the monitored release to the edition on disk.
    """
    artist = file_info.get("artist") or {}
    album = file_info.get("album") or {}
    tracks = file_info.get("tracks") or []
    if not artist.get("id") or not album.get("id") or not tracks:
        return None
    track_ids = [t["id"] for t in tracks if t.get("id")]
    if not track_ids:
        return None
    return {
        "path": file_info["path"],
        "artistId": artist["id"],
        "albumId": album["id"],
        "albumReleaseId": file_info.get("albumReleaseId", 0),
        "trackIds": track_ids,
        "quality": file_info.get("quality", {}),
        "replaceExistingFiles": False,
        "disableReleaseSwitching": False,
    }


def release_track_count(file_info: dict[str, Any]) -> int:
    """Track count of the release this file matched, from ``album.releases``.

    Returns 0 when the matched release can't be resolved (callers treat 0 as
    "unknown" and do not let it block an import).
    """
    album = file_info.get("album") or {}
    release_id = file_info.get("albumReleaseId")
    for rel in album.get("releases") or []:
        if rel.get("id") == release_id:
            return int(rel.get("trackCount") or 0)
    return 0


def stub_coverage(
    imported_by_release: dict[int, int],
    tracks_by_release: dict[int, int],
) -> tuple[int, int, float]:
    """Coverage of the dominant matched release: ``(imported, total, fraction)``.

    The "dominant" release is the one the most importable files mapped to. An
    unknown release size (0) yields a fraction of 1.0 so it never blocks — we
    only skip when we can prove the import would be a small fraction of a
    known-larger release (the incomplete-download stub case).
    """
    if not imported_by_release:
        return (0, 0, 0.0)
    dominant = max(imported_by_release, key=lambda r: imported_by_release[r])
    imported = imported_by_release[dominant]
    total = tracks_by_release.get(dominant, 0)
    fraction = imported / total if total > 0 else 1.0
    return imported, total, fraction


def select_importable_items(
    entries: list[dict[str, Any]],
    *,
    accept_min_match: float = DEFAULT_ACCEPT_MIN_MATCH,
    accept_missing_tracks: bool = False,
    block_fewer_tracks: bool = True,
    min_track_fraction: float = 0.5,
) -> tuple[list[dict[str, Any]], str | None]:
    """Select the importable items from a ``/manualimport`` scan.

    Returns ``(items, stub_skip_reason)``. ``items`` is the list of
    ``ManualImport`` payload items whose rejections pass ``classify_reasons``.
    ``stub_skip_reason`` is non-None (and ``items`` empty) when the acceptable
    files would only cover a small fraction of a known-larger release — an
    incomplete download from a dead peer that should be re-grabbed, not
    imported.
    """
    items: list[dict[str, Any]] = []
    imported_by_release: dict[int, int] = {}
    tracks_by_release: dict[int, int] = {}

    for entry in entries:
        if entry.get("additionalFile"):
            continue
        reasons = [r.get("reason", "") for r in entry.get("rejections", [])]
        acceptable, _blockers = classify_reasons(
            reasons,
            accept_min_match=accept_min_match,
            accept_missing_tracks=accept_missing_tracks,
            block_fewer_tracks=block_fewer_tracks,
        )
        if not acceptable:
            continue
        item = build_import_item(entry)
        if not item:
            continue
        items.append(item)
        release_id = item["albumReleaseId"]
        imported_by_release[release_id] = imported_by_release.get(release_id, 0) + 1
        tracks_by_release[release_id] = release_track_count(entry)

    if items and min_track_fraction > 0:
        imported, total, fraction = stub_coverage(imported_by_release, tracks_by_release)
        if total > 0 and fraction < min_track_fraction:
            return [], (
                f"stub: would import only {imported}/{total} tracks of release "
                f"({fraction:.0%} < {min_track_fraction:.0%} min)"
            )

    return items, None
