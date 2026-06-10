#!/usr/bin/env python3
"""Write/lock album.nfo sidecars so Jellyfin shows correct original release years.

Background
----------
Jellyfin's built-in MusicBrainz scraper frequently stores incorrect or
reissue years for albums.  The playlist-generator service maintains a
``album_release_dates`` table in its Postgres database with the *original*
release year (and optional month/day) resolved from MusicBrainz release groups.

The reliable way to make Jellyfin respect these dates is an ``album.nfo``
sidecar in each album folder: Jellyfin's Nfo reader runs first and
``<lockdata>true</lockdata>`` prevents the online provider from overriding the
values on the next metadata refresh.  (Setting LockData via the Jellyfin API
is unreliable across container restarts — NFO is the durable path.)

What this script does
---------------------
1. Query the playlist-generator Postgres database (via ``docker exec``) for
   every album that has a resolved ``original_year``, together with one
   representative track path per album.
2. Translate each track path from its container-side prefix (``/music``) to
   the host-side path (``$SHARE_DIRECTORY/music``).  Collapse disc sub-folders
   (``Disc 01/``, ``CD2/``, …) so ``album.nfo`` is written in the album root.
3. Dry-run (default): report how many NFOs would be created or updated and
   show a sample.
4. ``--apply``: write/update each ``album.nfo`` using
   ``xml.etree.ElementTree``.  Existing NFOs are parsed and merged — all
   non-date elements are preserved, any ``musicbrainz*`` id elements are
   stripped (they would cause Jellyfin to re-fetch the wrong date), and
   ``<year>``, ``<premiered>``, ``<releasedate>``, ``<lockdata>`` are set.
5. Optionally (``--refresh``) POST a Jellyfin library refresh after writing.
   Best-effort: uses ``docker exec`` to reach Jellyfin inside the compose
   network and never fails the script if it errors.

Idempotency
-----------
An album is skipped (counted as "current") when its existing ``album.nfo``
already contains the correct ``<year>`` **and** ``<lockdata>true</lockdata>``.

Multi-disc albums
-----------------
If the track's immediate parent folder matches the disc sub-folder pattern
(case-insensitive: ``disc N``, ``cd N``), ``album.nfo`` is written one level
up — the album root — because that is where Jellyfin looks for it.

NFO format
----------
Written with an XML declaration ``<?xml version="1.0" encoding="utf-8"
standalone="yes"?>`` and a root ``<album>`` element.  When updating an
existing file, all non-date, non-musicbrainz child elements are preserved.

Exit codes
----------
  0  success (or dry-run / nothing to do)
  1  partial (some album folders missing or NFO writes failed)
  2  fatal (docker not found, no DB rows returned, music directory missing,
           unexpected top-level error)

Environment
-----------
  SHARE_DIRECTORY        Base share path (default: /mnt/drive).
                         Music root resolves to ``$SHARE_DIRECTORY/music``
                         unless ``--music-dir`` is given.
  API_KEY_JELLYFIN       Used by ``--refresh`` to POST to Jellyfin's
                         ``/Library/Refresh`` endpoint.
  PLAYLIST_GENERATOR_DB_CONTAINER
                         Docker container name for the playlist DB
                         (default: playlist-generator-db).

Usage
-----
  # Dry-run (default) — prints plan, touches nothing
  python scripts/jellyfin_nfo_dates.py

  # Apply NFO writes
  python scripts/jellyfin_nfo_dates.py --apply

  # Apply + trigger Jellyfin library refresh
  python scripts/jellyfin_nfo_dates.py --apply --refresh

  # Preview against a local music tree
  python scripts/jellyfin_nfo_dates.py --music-dir /mnt/drive/music
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

if "SHARE_DIRECTORY" not in os.environ:
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except ImportError:
        pass

DEFAULT_SHARE_DIRECTORY = "/mnt/drive"
DEFAULT_DB_CONTAINER = "playlist-generator-db"
CONTAINER_MUSIC_PREFIX = "/music"

_DISC_RE = re.compile(r"^(disc|cd)\s*\d+$", re.IGNORECASE)

# SQL fetches one representative track path per resolved album.
_DB_SQL = (
    "SELECT a.id, a.title,"
    " ard.original_year, ard.original_month, ard.original_day,"
    " (SELECT tf.path FROM track_albums ta"
    "  JOIN track_files tf ON tf.track_id=ta.track_id"
    "  WHERE ta.album_id=a.id AND tf.path IS NOT NULL LIMIT 1) AS track_path"
    " FROM albums a"
    " JOIN album_release_dates ard ON ard.album_id=a.id"
    " WHERE ard.original_year IS NOT NULL;"
)

_DATE_TAGS = frozenset({"year", "premiered", "releasedate", "lockdata"})
_XML_DECL = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlbumRecord:
    """One row from the playlist-generator DB."""

    album_id: str
    title: str
    year: int
    month: int | None
    day: int | None
    track_path: str | None


@dataclass
class NfoResult:
    """Outcome tallies for a run."""

    current: int = 0
    would_write: int = 0
    written: int = 0
    failed: int = 0
    missing_folder: int = 0
    no_track_path: int = 0


# ---------------------------------------------------------------------------
# Pure / testable functions
# ---------------------------------------------------------------------------


def album_folder(track_path: str, container_prefix: str, host_prefix: str) -> Path:
    """Resolve the host-side album folder from a container track path.

    Swaps *container_prefix* → *host_prefix* then collapses disc sub-folders
    (matching ``^(disc|cd)\\s*\\d+$``, case-insensitive) so that ``album.nfo``
    is always written in the album root rather than a disc sub-folder.

    >>> album_folder("/music/Burzum/Filosofem/01.flac", "/music", "/mnt/drive/music")
    PosixPath('/mnt/drive/music/Burzum/Filosofem')

    >>> album_folder("/music/Artist/Album/Disc 1/01.flac", "/music", "/mnt/drive/music")
    PosixPath('/mnt/drive/music/Artist/Album')
    """
    # Strip trailing slash from prefixes for clean replacement.
    cpfx = container_prefix.rstrip("/")
    hpfx = host_prefix.rstrip("/")

    if track_path.startswith(cpfx + "/"):
        relative = track_path[len(cpfx) + 1:]
        host_path = Path(hpfx) / relative
    else:
        # Not under the expected prefix — use path as-is (best effort).
        host_path = Path(track_path)

    parent = host_path.parent
    if _DISC_RE.match(parent.name):
        return parent.parent
    return parent


def iso_date(year: int, month: int | None, day: int | None) -> str:
    """Format a partial date as ISO-8601 ``YYYY-MM-DD``, defaulting month/day to 01.

    >>> iso_date(1990, None, None)
    '1990-01-01'
    >>> iso_date(1983, 5, 25)
    '1983-05-25'
    >>> iso_date(2001, 3, None)
    '2001-03-01'
    """
    m = month if month is not None else 1
    d = day if day is not None else 1
    return f"{year:04d}-{m:02d}-{d:02d}"


def build_album_nfo(
    existing_xml: str | None,
    title: str,
    year: int,
    month: int | None,
    day: int | None,
) -> str:
    """Build (or update) an ``album.nfo`` XML string.

    If *existing_xml* is ``None``, a minimal ``<album>`` element is created.
    Otherwise the existing XML is parsed and:
    - ``<year>``, ``<premiered>``, ``<releasedate>``, ``<lockdata>`` are
      set/overwritten.
    - Any child element whose tag starts with ``musicbrainz`` is removed so
      Jellyfin cannot re-fetch a wrong date via a stale MB release id.
    - All other existing child elements are preserved.
    - A ``<title>`` is added if none exists.

    The returned string includes the XML declaration line.
    """
    date_str = iso_date(year, month, day)

    if existing_xml:
        try:
            root = ET.fromstring(existing_xml)
        except ET.ParseError:
            root = ET.Element("album")
    else:
        root = ET.Element("album")

    # Remove musicbrainz* elements and date-related elements (we'll re-add them).
    to_remove = [
        el for el in list(root)
        if el.tag.lower().startswith("musicbrainz") or el.tag.lower() in _DATE_TAGS
    ]
    for el in to_remove:
        root.remove(el)

    # Ensure <title> exists.
    if root.find("title") is None:
        title_el = ET.SubElement(root, "title")
        title_el.text = title

    # Append date fields in a consistent order.
    year_el = ET.SubElement(root, "year")
    year_el.text = str(year)

    premiered_el = ET.SubElement(root, "premiered")
    premiered_el.text = date_str

    releasedate_el = ET.SubElement(root, "releasedate")
    releasedate_el.text = date_str

    lock_el = ET.SubElement(root, "lockdata")
    lock_el.text = "true"

    # Serialise.
    body = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return f"{_XML_DECL}\n{body}"


def nfo_is_current(existing_xml: str, year: int) -> bool:
    """Return True if *existing_xml* already has the correct year and lockdata=true.

    Parsing errors are treated as "not current" so the NFO is rewritten.
    """
    try:
        root = ET.fromstring(existing_xml)
    except ET.ParseError:
        return False

    year_el = root.find("year")
    lock_el = root.find("lockdata")

    if year_el is None or year_el.text is None:
        return False
    try:
        nfo_year = int(year_el.text.strip())
    except ValueError:
        return False

    if nfo_year != year:
        return False

    return not (lock_el is None or (lock_el.text or "").strip().lower() != "true")


# ---------------------------------------------------------------------------
# DB / system helpers (side-effecting; not directly unit-tested)
# ---------------------------------------------------------------------------


def _run_docker_exec(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run a ``docker exec`` command and return the completed process."""
    return subprocess.run(  # noqa: S603
        ["docker", "exec", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def fetch_album_records(db_container: str) -> list[AlbumRecord]:
    """Query the playlist-generator DB and return a list of AlbumRecord objects.

    Raises RuntimeError on failure (docker not found, psql error, empty result).
    """
    if not shutil.which("docker"):
        raise RuntimeError("docker executable not found on PATH")

    result = _run_docker_exec([
        db_container,
        "psql", "-U", "playlist", "-d", "playlist_generator",
        "-t", "-A", "-F|",
        "-c", _DB_SQL,
    ])

    if result.returncode != 0:
        raise RuntimeError(
            f"psql exited {result.returncode}: {result.stderr.strip()}"
        )

    records: list[AlbumRecord] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        album_id, title, raw_year, raw_month, raw_day, track_path = parts[:6]
        try:
            year = int(raw_year)
        except ValueError:
            continue
        month: int | None = None
        day: int | None = None
        with contextlib.suppress(ValueError):
            month = int(raw_month) if raw_month.strip() else None
        with contextlib.suppress(ValueError):
            day = int(raw_day) if raw_day.strip() else None
        records.append(AlbumRecord(
            album_id=album_id,
            title=title,
            year=year,
            month=month,
            day=day,
            track_path=track_path.strip() if track_path.strip() else None,
        ))

    if not records:
        raise RuntimeError("No album_release_dates rows returned from DB (table empty?)")

    return records


def write_nfo(path: Path, content: str) -> None:
    """Write *content* to *path*, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def trigger_jellyfin_refresh(api_key: str) -> None:
    """POST /Library/Refresh to Jellyfin via docker exec (best-effort)."""
    cmd = [
        "playlist-generator",
        "curl", "-s", "-X", "POST",
        f"http://jellyfin:8096/Library/Refresh?api_key={api_key}",
    ]
    try:
        result = _run_docker_exec(cmd, timeout=15)
        if result.returncode == 0:
            print("  refresh: Jellyfin /Library/Refresh triggered")
        else:
            print(
                f"  refresh: non-zero exit from docker exec "
                f"(ignored): {result.stderr.strip()}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort
        print(f"  refresh: skipped (error: {exc})", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write/lock album.nfo sidecars so Jellyfin shows the correct "
            "original release year from the playlist-generator DB."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write NFO files. Default is dry-run (no writes).",
    )
    parser.add_argument(
        "--music-dir",
        type=Path,
        default=None,
        help=(
            "Override host-side music root "
            "(default: $SHARE_DIRECTORY/music or /mnt/drive/music)."
        ),
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help=(
            "After applying, POST a Jellyfin library refresh. "
            "Best-effort — never fails the script."
        ),
    )
    parser.add_argument(
        "--db-container",
        default=None,
        help=(
            f"Docker container name for the playlist DB "
            f"(default: {DEFAULT_DB_CONTAINER})."
        ),
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:  # noqa: C901
    args = parse_args(argv)

    share_dir = os.environ.get("SHARE_DIRECTORY", DEFAULT_SHARE_DIRECTORY)
    music_dir: Path = args.music_dir or Path(share_dir) / "music"
    db_container = args.db_container or os.environ.get(
        "PLAYLIST_GENERATOR_DB_CONTAINER", DEFAULT_DB_CONTAINER
    )
    jellyfin_api_key = os.environ.get("API_KEY_JELLYFIN", "")

    if not music_dir.is_dir():
        print(f"ERROR: music directory not found: {music_dir}", file=sys.stderr)
        return 2

    # ------------------------------------------------------------------
    # Fetch records from the playlist-generator DB.
    # ------------------------------------------------------------------
    try:
        records = fetch_album_records(db_container)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"DB: {len(records)} albums with resolved release dates")

    host_music = str(music_dir)
    tally = NfoResult()
    sample_changes: list[str] = []
    SAMPLE_LIMIT = 10

    for rec in records:
        if rec.track_path is None:
            tally.no_track_path += 1
            continue

        try:
            folder = album_folder(rec.track_path, CONTAINER_MUSIC_PREFIX, host_music)
        except Exception as exc:  # noqa: BLE001
            print(
                f"WARNING: cannot resolve folder for album {rec.album_id!r}: {exc}",
                file=sys.stderr,
            )
            tally.failed += 1
            continue

        if not folder.is_dir():
            tally.missing_folder += 1
            continue

        nfo_path = folder / "album.nfo"
        existing_xml: str | None = None
        if nfo_path.exists():
            try:
                existing_xml = nfo_path.read_text(encoding="utf-8")
            except OSError:
                existing_xml = None

        if existing_xml and nfo_is_current(existing_xml, rec.year):
            tally.current += 1
            continue

        tally.would_write += 1
        if len(sample_changes) < SAMPLE_LIMIT:
            action = "update" if existing_xml else "create"
            sample_changes.append(
                f"  {action}: {nfo_path}  year={rec.year}"
            )

        if args.apply:
            try:
                content = build_album_nfo(existing_xml, rec.title, rec.year, rec.month, rec.day)
                write_nfo(nfo_path, content)
                tally.written += 1
            except OSError as exc:
                print(f"WARNING: failed to write {nfo_path}: {exc}", file=sys.stderr)
                tally.failed += 1

    # ------------------------------------------------------------------
    # Report
    # ------------------------------------------------------------------
    dry_marker = " (DRY RUN — use --apply to write)" if not args.apply else ""
    print(
        f"result: {tally.current} current, {tally.would_write} to write"
        + (f", {tally.missing_folder} missing folder(s)" if tally.missing_folder else "")
        + (f", {tally.no_track_path} no track path" if tally.no_track_path else "")
        + (f", {tally.failed} failed" if tally.failed else "")
        + dry_marker
    )
    if args.apply:
        print(f"wrote {tally.written}/{tally.would_write} NFO file(s)")

    if not args.apply and sample_changes:
        print("sample (up to 10):")
        for line in sample_changes:
            print(line)

    # ------------------------------------------------------------------
    # Optional Jellyfin refresh
    # ------------------------------------------------------------------
    if args.apply and args.refresh:
        if not jellyfin_api_key:
            print(
                "WARNING: --refresh requested but API_KEY_JELLYFIN not set; skipping",
                file=sys.stderr,
            )
        else:
            trigger_jellyfin_refresh(jellyfin_api_key)

    if tally.failed or tally.missing_folder:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
