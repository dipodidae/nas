#!/usr/bin/env python3
"""Process stuck Soulseek downloads and import them into Lidarr.

Many albums downloaded manually from Soulseek get stuck in the downloads
folder because:
  - QueueCleaner was enabled after downloads were already present
  - Folder names don't match Lidarr's expected naming scheme
  - Lidarr's auto-matching threshold (80%) rejects imperfect matches

This script uses Lidarr's Manual Import API to:
  1. Scan each download folder for audio fingerprinting and matching
  2. Evaluate Lidarr's match quality and confidence
  3. Import matched albums using copy mode (originals are never touched)
  4. Produce a detailed report of all actions

Usage:
    # Dry run (default) - show what would be imported
    python scripts/process_soulseek_imports.py

    # Actually execute imports
    python scripts/process_soulseek_imports.py --execute

    # Write report to file
    python scripts/process_soulseek_imports.py --report report.txt

    # Only process the first N folders (for testing)
    python scripts/process_soulseek_imports.py --limit 5

    # Process a specific folder
    python scripts/process_soulseek_imports.py --folder "Erase"

Exit codes:
    0 - Success (all processable items imported or dry-run complete)
    1 - Partial success (some items skipped or failed)
    2 - Fatal error (API unreachable, bad configuration, etc.)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUDIO_EXTENSIONS: set[str] = {
    ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".aac", ".wma", ".wav",
    ".ape", ".wv", ".alac", ".aiff", ".aif",
}

# Folders in the downloads directory that are not music
SKIP_FOLDERS: set[str] = {
    "prowlarr", "radarr", "tv-sonarr", "incomplete", "books",
    "1G1R - Redump - Nintendo - GameCube", "NES", "SNES",
    "SNES-Homebrew", "SNES-Romhacks", "SNES-extracted", "GBC", "GB",
    "Digital Media 01",
}

# Lidarr manual import API timeout (seconds) — fingerprinting is slow
MANUAL_IMPORT_TIMEOUT: int = 120

# Delay between API calls to avoid overloading Lidarr
API_DELAY: float = 1.0

# Default import mode — copy preserves originals
IMPORT_MODE: str = "copy"

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FolderResult:
    """Result of processing a single download folder."""

    folder: str
    status: str  # imported, skipped, failed, error
    reason: str = ""
    artist: str = ""
    album: str = ""
    tracks_imported: int = 0
    tracks_total: int = 0
    rejections: list[str] = field(default_factory=list)


@dataclass
class ImportSummary:
    """Aggregate summary of the import run."""

    total_folders: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    errors: int = 0
    results: list[FolderResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Lidarr API client
# ---------------------------------------------------------------------------

class LidarrClient:
    """Lightweight Lidarr API client."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"X-Api-Key": api_key})

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1{path}"

    def ping(self) -> bool:
        """Check if Lidarr is reachable."""
        try:
            resp = self.session.get(
                f"{self.base_url}/ping", timeout=10,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def get_system_status(self) -> dict[str, Any]:
        resp = self.session.get(self._url("/system/status"), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_queue(self, page_size: int = 400) -> list[dict[str, Any]]:
        """Get all queue items."""
        resp = self.session.get(
            self._url("/queue"),
            params={"pageSize": page_size},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("records", [])

    def get_manual_import(
        self,
        folder: str,
        filter_existing: bool = True,
    ) -> list[dict[str, Any]]:
        """Ask Lidarr to scan a folder and return match suggestions.

        This is slow (30-120s) because Lidarr fingerprints audio files.
        """
        resp = self.session.get(
            self._url("/manualimport"),
            params={
                "folder": folder,
                "filterExistingFiles": str(filter_existing).lower(),
            },
            timeout=MANUAL_IMPORT_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()

    def post_manual_import(
        self,
        items: list[dict[str, Any]],
        import_mode: str = IMPORT_MODE,
    ) -> Any:
        """Submit manual import items to Lidarr for processing."""
        payload = {
            "name": "ManualImport",
            "importMode": import_mode,
            "files": items,
        }
        resp = self.session.post(
            self._url("/command"),
            json=payload,
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()

    def get_command_status(self, command_id: int) -> dict[str, Any]:
        """Check status of a command."""
        resp = self.session.get(
            self._url(f"/command/{command_id}"),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def wait_for_command(
        self,
        command_id: int,
        timeout: int = 300,
        poll_interval: int = 5,
    ) -> dict[str, Any]:
        """Poll until a command completes or times out."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_command_status(command_id)
            if status.get("status") in ("completed", "failed", "aborted"):
                return status
            time.sleep(poll_interval)
        return {"status": "timeout", "result": "unknown"}

    def get_artists(self) -> list[dict[str, Any]]:
        """Get all artists (id, name, path only for lightweight use)."""
        resp = self.session.get(self._url("/artist"), timeout=60)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _has_audio_files(host_path: Path) -> bool:
    """Check if a directory contains at least one audio file."""
    for item in host_path.rglob("*"):
        if item.is_file() and item.suffix.lower() in AUDIO_EXTENSIONS:
            return True
    return False


def _build_import_item(file_info: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a manual-import GET response item into a POST payload item.

    Returns None if essential data is missing.
    """
    artist = file_info.get("artist")
    album = file_info.get("album")
    tracks = file_info.get("tracks", [])

    if not artist or not artist.get("id"):
        return None
    if not album or not album.get("id"):
        return None
    if not tracks:
        return None

    return {
        "path": file_info["path"],
        "artistId": artist["id"],
        "albumId": album["id"],
        "albumReleaseId": file_info.get("albumReleaseId", 0),
        "trackIds": [t["id"] for t in tracks if t.get("id")],
        "quality": file_info.get("quality", {}),
        "replaceExistingFiles": False,
        "disableReleaseSwitching": False,
    }


def _evaluate_rejections(
    file_info: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Evaluate whether rejections are acceptable for import.

    Returns (should_import, list_of_rejection_reasons).

    We accept:
      - "Has missing tracks" (partial album is still useful)
      - "Has unmatched tracks" (extra tracks are ok)

    We reject:
      - "Album match is not close enough" (wrong album)
      - "Not an upgrade" (already have it)
      - "Couldn't find similar album" (no match at all)
    """
    rejections = file_info.get("rejections", [])
    reasons = []
    dominated_by_blockers = False

    for rej in rejections:
        reason = rej.get("reason", "")
        reasons.append(reason)

        # Permanent rejections that indicate wrong match or no value
        if any(
            phrase in reason.lower()
            for phrase in [
                "not close enough",
                "not an upgrade",
                "couldn't find similar",
                "destination already exists",
            ]
        ):
            dominated_by_blockers = True

    return (not dominated_by_blockers, reasons)


def _get_queue_paths(client: LidarrClient) -> set[str]:
    """Get the set of output paths currently tracked in the Lidarr queue."""
    queue = client.get_queue()
    paths = set()
    for record in queue:
        output_path = record.get("outputPath", "")
        if output_path:
            paths.add(os.path.basename(output_path))
    return paths


def scan_downloads_dir(host_downloads: Path) -> list[str]:
    """Return sorted list of music folder names in the downloads directory."""
    folders = []
    for item in sorted(host_downloads.iterdir()):
        if not item.is_dir():
            continue
        if item.name in SKIP_FOLDERS:
            continue
        if _has_audio_files(item):
            folders.append(item.name)
    return folders


def process_folder(
    client: LidarrClient,
    container_downloads: str,
    folder_name: str,
    *,
    execute: bool = False,
    log: logging.Logger,
) -> FolderResult:
    """Process a single download folder through Lidarr's manual import.

    1. GET /manualimport to scan and fingerprint
    2. Evaluate match quality
    3. If good enough and execute=True, POST to import
    """
    container_path = f"{container_downloads}/{folder_name}"
    log.info("Scanning: %s", folder_name)

    # Step 1: ask Lidarr to scan and match
    try:
        items = client.get_manual_import(container_path)
    except requests.Timeout:
        log.warning("  Timeout scanning %s (>%ds)", folder_name, MANUAL_IMPORT_TIMEOUT)
        return FolderResult(
            folder=folder_name,
            status="error",
            reason=f"API timeout after {MANUAL_IMPORT_TIMEOUT}s",
        )
    except requests.RequestException as exc:
        log.warning("  API error scanning %s: %s", folder_name, exc)
        return FolderResult(
            folder=folder_name,
            status="error",
            reason=str(exc),
        )

    if not items:
        log.info("  No audio files detected by Lidarr")
        return FolderResult(
            folder=folder_name,
            status="skipped",
            reason="No audio files detected by Lidarr",
        )

    # Step 2: evaluate each file's match quality
    importable_items: list[dict[str, Any]] = []
    all_rejections: list[str] = []
    total_files = len(items)
    artist_name = ""
    album_title = ""

    for file_info in items:
        if file_info.get("additionalFile"):
            continue

        should_import, reasons = _evaluate_rejections(file_info)
        all_rejections.extend(reasons)

        if not should_import:
            continue

        import_item = _build_import_item(file_info)
        if import_item:
            importable_items.append(import_item)
            # Track artist/album name from first good item
            if not artist_name:
                artist_name = file_info.get("artist", {}).get("artistName", "?")
                album_title = file_info.get("album", {}).get("title", "?")

    if not importable_items:
        unique_reasons = sorted(set(all_rejections))[:5]
        reason = "; ".join(unique_reasons) if unique_reasons else "No confident matches"
        log.info("  SKIP: %s", reason)
        return FolderResult(
            folder=folder_name,
            status="skipped",
            reason=reason,
            artist=artist_name,
            album=album_title,
            tracks_total=total_files,
            rejections=unique_reasons,
        )

    log.info(
        "  Matched: %s - %s (%d/%d tracks)",
        artist_name,
        album_title,
        len(importable_items),
        total_files,
    )

    # Step 3: import (or report in dry-run)
    if not execute:
        log.info("  DRY-RUN: would import %d tracks via copy", len(importable_items))
        return FolderResult(
            folder=folder_name,
            status="imported",
            reason="dry-run",
            artist=artist_name,
            album=album_title,
            tracks_imported=len(importable_items),
            tracks_total=total_files,
            rejections=sorted(set(all_rejections)),
        )

    try:
        result = client.post_manual_import(importable_items)
        command_id = result.get("id")
        if command_id:
            log.info("  Import command submitted (id=%d), waiting...", command_id)
            final = client.wait_for_command(command_id, timeout=300)
            final_status = final.get("status", "unknown")
            final_result = final.get("result", "unknown")
            log.info("  Import result: %s / %s", final_status, final_result)

            if final_result == "successful":
                return FolderResult(
                    folder=folder_name,
                    status="imported",
                    artist=artist_name,
                    album=album_title,
                    tracks_imported=len(importable_items),
                    tracks_total=total_files,
                )
            return FolderResult(
                folder=folder_name,
                status="failed",
                reason=f"Command {final_status}/{final_result}",
                artist=artist_name,
                album=album_title,
                tracks_imported=0,
                tracks_total=total_files,
            )

        log.warning("  No command ID returned")
        return FolderResult(
            folder=folder_name,
            status="failed",
            reason="No command ID returned from import",
            artist=artist_name,
            album=album_title,
            tracks_total=total_files,
        )
    except requests.RequestException as exc:
        log.error("  Import API error: %s", exc)
        return FolderResult(
            folder=folder_name,
            status="error",
            reason=str(exc),
            artist=artist_name,
            album=album_title,
            tracks_total=total_files,
        )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_summary(summary: ImportSummary, *, log: logging.Logger) -> None:
    """Print a human-readable summary."""
    log.info("")
    log.info("=" * 70)
    log.info("IMPORT SUMMARY")
    log.info("=" * 70)
    log.info("Total folders processed: %d", summary.total_folders)
    log.info("  Imported:  %d", summary.imported)
    log.info("  Skipped:   %d", summary.skipped)
    log.info("  Failed:    %d", summary.failed)
    log.info("  Errors:    %d", summary.errors)
    log.info("")

    if summary.imported > 0:
        log.info("--- IMPORTED ---")
        for r in summary.results:
            if r.status == "imported":
                extra = " (dry-run)" if r.reason == "dry-run" else ""
                log.info(
                    "  [OK%s] %s -> %s - %s (%d/%d tracks)",
                    extra,
                    r.folder,
                    r.artist,
                    r.album,
                    r.tracks_imported,
                    r.tracks_total,
                )
        log.info("")

    if summary.skipped > 0:
        log.info("--- SKIPPED ---")
        for r in summary.results:
            if r.status == "skipped":
                artist_info = f" ({r.artist} - {r.album})" if r.artist else ""
                log.info("  [SKIP] %s%s", r.folder, artist_info)
                log.info("         Reason: %s", r.reason)
        log.info("")

    if summary.failed > 0:
        log.info("--- FAILED ---")
        for r in summary.results:
            if r.status == "failed":
                log.info("  [FAIL] %s -> %s - %s", r.folder, r.artist, r.album)
                log.info("         Reason: %s", r.reason)
        log.info("")

    if summary.errors > 0:
        log.info("--- ERRORS ---")
        for r in summary.results:
            if r.status == "error":
                log.info("  [ERR]  %s", r.folder)
                log.info("         Reason: %s", r.reason)
        log.info("")


def write_report(summary: ImportSummary, report_path: Path) -> None:
    """Write a machine-readable report (tab-separated)."""
    with report_path.open("w", encoding="utf-8") as fh:
        fh.write("status\tfolder\tartist\talbum\ttracks_imported\ttracks_total\treason\n")
        for r in summary.results:
            fh.write(
                f"{r.status}\t{r.folder}\t{r.artist}\t{r.album}\t"
                f"{r.tracks_imported}\t{r.tracks_total}\t{r.reason}\n",
            )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process stuck Soulseek downloads into Lidarr",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually import (default is dry-run)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Write tab-separated report to this file",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N folders",
    )
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Only process a specific folder name",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Extra debug logging",
    )
    parser.add_argument(
        "--lidarr-url",
        type=str,
        default=None,
        help="Lidarr base URL (default: http://127.0.0.1:8686)",
    )
    parser.add_argument(
        "--downloads-dir",
        type=Path,
        default=None,
        help="Host path to downloads directory",
    )
    parser.add_argument(
        "--container-downloads",
        type=str,
        default="/downloads",
        help="Container path to downloads directory (default: /downloads)",
    )
    parser.add_argument(
        "--skip-queue-tracked",
        action="store_true",
        default=False,
        help="Skip folders already tracked in Lidarr's queue",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )
    log = logging.getLogger("soulseek-import")

    # Load environment
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    # Resolve configuration
    api_key = os.environ.get("API_KEY_LIDARR", "")
    if not api_key:
        log.error("API_KEY_LIDARR not set in environment or .env")
        return 2

    lidarr_url = args.lidarr_url or os.environ.get(
        "LIDARR_URL", "http://127.0.0.1:8686",
    )
    share_dir = os.environ.get("SHARE_DIRECTORY", "/mnt/drive-next")
    host_downloads = args.downloads_dir or Path(share_dir) / "Downloads"

    if not host_downloads.is_dir():
        log.error("Downloads directory not found: %s", host_downloads)
        return 2

    # Initialize client and verify connectivity
    client = LidarrClient(lidarr_url, api_key)
    if not client.ping():
        log.error("Cannot reach Lidarr at %s", lidarr_url)
        return 2

    status = client.get_system_status()
    log.info("Connected to Lidarr %s", status.get("version", "unknown"))

    mode_label = "EXECUTE" if args.execute else "DRY-RUN"
    log.info("Mode: %s | Import mode: %s", mode_label, IMPORT_MODE)
    log.info("Downloads dir (host): %s", host_downloads)
    log.info("Downloads dir (container): %s", args.container_downloads)
    log.info("")

    # Scan download folders
    if args.folder:
        folder_path = host_downloads / args.folder
        if not folder_path.is_dir():
            log.error("Folder not found: %s", folder_path)
            return 2
        folders = [args.folder]
    else:
        log.info("Scanning downloads directory for music folders...")
        folders = scan_downloads_dir(host_downloads)
        log.info("Found %d music folders", len(folders))

    # Optionally filter out queue-tracked folders
    if args.skip_queue_tracked:
        log.info("Checking Lidarr queue for already-tracked folders...")
        queue_paths = _get_queue_paths(client)
        original_count = len(folders)
        folders = [f for f in folders if f not in queue_paths]
        log.info(
            "Filtered %d queue-tracked folders, %d remaining",
            original_count - len(folders),
            len(folders),
        )

    # Apply limit
    if args.limit:
        folders = folders[: args.limit]
        log.info("Limited to first %d folders", args.limit)

    log.info("")
    log.info("Processing %d folders...", len(folders))
    log.info("-" * 70)

    # Process each folder
    summary = ImportSummary(total_folders=len(folders))

    for i, folder_name in enumerate(folders, 1):
        log.info("[%d/%d] %s", i, len(folders), folder_name)

        result = process_folder(
            client,
            args.container_downloads,
            folder_name,
            execute=args.execute,
            log=log,
        )

        summary.results.append(result)
        if result.status == "imported":
            summary.imported += 1
        elif result.status == "skipped":
            summary.skipped += 1
        elif result.status == "failed":
            summary.failed += 1
        elif result.status == "error":
            summary.errors += 1

        # Be nice to the API
        if i < len(folders):
            time.sleep(API_DELAY)

    # Print summary
    print_summary(summary, log=log)

    # Write report file
    if args.report:
        write_report(summary, args.report)
        log.info("Report written to %s", args.report)

    # Determine exit code
    if summary.errors > 0 or summary.failed > 0:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
