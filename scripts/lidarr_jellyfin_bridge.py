#!/usr/bin/env python3
"""Make Lidarr's imports actually reach Jellyfin, by fixing the path Lidarr sends.

Why this exists
---------------
Lidarr's built-in "Emby / Jellyfin" connection with *Update Library* enabled
does fire on import: verified 2026-09-01 with Jellyfin request logging on, a
`POST http://jellyfin:8096/mediabrowser/Library/Media/Updated` arrives and
returns 204. It is nonetheless a **silent no-op**, and the 204 is what hides it.

The reason is a path-namespace mismatch. Lidarr's media root is `/music`;
Jellyfin's Music library lives at `/data/movies/music` (the `${SHARE_DIRECTORY}`
mount). `Library/Media/Updated` hands each reported path to Jellyfin's
LibraryMonitor, which resolves it to the library that contains it — and
`/music/...` is under no Jellyfin library, so it is dropped without a word.
Proven by A/B against the live server:

    POST {"Updates":[{"Path":"/music/Bathory/1988 - Blood Fire Death"}]}
      -> 204, no LibraryMonitor line, Jellyfin keeps the stale metadata
    POST {"Updates":[{"Path":"/data/movies/music/Bathory/1988 - Blood Fire Death"}]}
      -> 204, 'LibraryMonitor: "Blood Fire Death" ... will be refreshed', updated

Sonarr and Radarr have `mapFrom`/`mapTo` fields on their MediaBrowser
connection for exactly this, and they are now set. **Lidarr's does not expose
those fields at all** (confirmed against `/api/v1/notification` — its field list
is host/port/useSsl/urlBase/apiKey/notify/updateLibrary and nothing else), so
there is no in-Lidarr fix. This script is the missing mapping, applied outside
Lidarr: poll Lidarr's history for file-level events, translate the paths, and
report them to Jellyfin itself.

Targeted on purpose: it reports the *album folder* that changed, so Jellyfin
refreshes that folder rather than re-walking the whole Music library. A full
Music scan is the box's largest memory event (~1.56GB anon-RSS) and is not
something to run every five minutes — see docs/jellyfin-playback-audit.md.

Media roots
-----------
Lidarr's root folder moved `/music` -> `/data/music` on 2026-09-02 (the ADR-0003
retry, done offline by `lidarr_repath_db.py`). This script kept translating
`/music` alone, so for a day every import was dropped by `translate()` and seven
Kraftwerk albums never reached Jellyfin. Hence `DEFAULT_MAP_FROM` is a *list*:
history written on either side of a repath still maps. Longest root wins, so
adding a broad `/data` cannot swallow `/data/music`.

That miss was silent for a second reason worth keeping fixed: a dropped folder
printed a WARNING and still returned 0, and `cron_job.py` treats 0 and 1 alike
(`--ok-codes` defaults to `0,1`). A folder under no known root is therefore now
**fatal**, and the cursor is left where it is so the album is retried rather
than skipped past forever.

The cursor
----------
The cursor is a **history id high-water mark**, not a timestamp, and the state
file is written atomically. Both were data-loss paths, measured 2026-09-04:

* **Timestamps tie.** 600 live history records held only 125 distinct dates,
  and one single second was shared by 22 records. The old `date <= cursor`
  test skipped every record sharing the cursor's second, including ones that
  had not been written yet when the cursor was taken. `id` is monotonic and
  unique (0 inversions over 600 records), so `id > high_water_id` is exact.
* **A partial write was indistinguishable from no state at all.** The old
  `write_text` was not atomic, and a truncated, empty, or unparseable file
  fell through to a silent 30-minute lookback that then *saved* itself --
  permanently discarding everything between the real cursor and now-30min.
  Absent, empty, corrupt, and future-schema states are now each a distinct
  **exit 2**; none of them guesses.
* **Running out of pages used to skip the gap and advance anyway.** See
  `HistoryExhausted` below.

Bootstrapping a new state file is therefore deliberate: pass `--since-min N`
(or `--bootstrap`) once. Cron never passes either, so a lost state file alerts
instead of quietly re-basing.

Exit codes
----------
  0  nothing to do, or every path reported successfully
  2  fatal: missing API key, Lidarr or Jellyfin unreachable, Jellyfin rejected
     the batch, a folder sat under no known media root, the cursor state file
     could not be trusted, or history ran past MAX_HISTORY_PAGES. The cursor is
     not advanced in any of these cases, so the next run retries.

There is deliberately no exit 1: the report is a single all-or-nothing batch,
so "partial" cannot arise.

Environment
-----------
  API_KEY_LIDARR       (required)
  API_KEY_JELLYFIN     (required)
  LIDARR_HOST          (default: http://localhost:8686)
  JELLYFIN_HOST        (default: http://localhost:8096)

Usage
-----
  python scripts/lidarr_jellyfin_bridge.py --state logs/lidarr_jellyfin_bridge.json
  python scripts/lidarr_jellyfin_bridge.py --dry-run --since-min 120
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

if "API_KEY_LIDARR" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv()
  except ImportError:
    pass


DEFAULT_LIDARR_HOST = "http://localhost:8686"
DEFAULT_JELLYFIN_HOST = "http://localhost:8096"
DEFAULT_STATE = Path(__file__).resolve().parent.parent / "logs" / "lidarr_jellyfin_bridge.json"
# Both sides of the ADR-0003 repath. Order does not matter; longest wins.
DEFAULT_MAP_FROM = ("/data/music", "/music")
DEFAULT_MAP_TO = "/data/movies/music"
# Lidarr history events that mean "a file under /music changed on disk".
# trackFileDeleted is included so a removed album stops haunting Jellyfin.
FILE_EVENTS = {
  "trackFileImported",
  "trackFileRenamed",
  "trackFileRetagged",
  "trackFileDeleted",
}
PATH_FIELDS = ("importedPath", "path", "sourcePath")
# This box does hundreds of Lidarr imports a day and one album alone produces
# ~25 history records, so a single page is NOT reliably enough between runs
# during a backlog drain — fetch_history pages back until it passes the cursor.
# MAX_HISTORY_PAGES bounds the work if the cursor is very old (e.g. the state
# file was lost); anything older than that is left to the weekly library scan.
HISTORY_PAGE_SIZE = 200
MAX_HISTORY_PAGES = 10
DEFAULT_FIRST_RUN_MINUTES = 30
# Bumped only when the on-disk shape changes. A file claiming a HIGHER version
# than this was written by a newer build and is refused rather than misread as
# a stale cursor.
STATE_SCHEMA_VERSION = 1


class StateError(RuntimeError):
  """The cursor state file cannot be trusted.

  Every path that raises this used to fall through to a lookback, which then
  saved itself over the real cursor. Guessing here is how imports go missing,
  so the only safe response is to stop and alert.
  """


class HistoryExhausted(RuntimeError):
  """More new history than MAX_HISTORY_PAGES covers.

  The old code printed a WARNING to stderr, dispatched the newest 2000 records,
  advanced the cursor to the newest of them, and returned 0 -- so `cron_job.py`
  saw success and everything older was skipped forever. Demonstrated 2026-09-04
  with a cursor of 2026-07-01: 2000 records fetched, two months silently
  dropped, exit 0. It is now fatal with the cursor held.
  """


@dataclasses.dataclass(frozen=True)
class Cursor:
  """How much of Lidarr's history has been processed.

  `high_water_id` is the authority. `date` is carried for humans and for the
  one-shot migration from the pre-2026-09-04 date-only state file, where it is
  the only thing available.
  """

  high_water_id: int | None
  date: str

  def is_new(self, record: dict) -> bool:
    """True if `record` has not been processed yet."""
    if self.high_water_id is not None:
      record_id = record.get("id")
      return isinstance(record_id, int) and record_id > self.high_water_id
    return str(record.get("date", "")) > self.date

  def __str__(self) -> str:
    """Id when there is one, date during the v0 migration when there is not."""
    return f"id={self.high_water_id}" if self.high_water_id is not None else f"date={self.date}"

  def advanced_by(self, record: dict) -> Cursor:
    """This cursor moved past `record` (whatever its event type)."""
    record_id = record.get("id")
    high = self.high_water_id
    if isinstance(record_id, int):
      high = record_id if high is None else max(high, record_id)
    date = str(record.get("date", ""))
    return Cursor(high, max(self.date, date) if date else self.date)


def _get_json(url: str, headers: dict[str, str], timeout: int = 60):
  req = urllib.request.Request(url, headers=headers)
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - localhost
      return json.loads(resp.read().decode("utf-8", "replace"))
  except (OSError, json.JSONDecodeError, urllib.error.HTTPError):
    return None


def fetch_history(host: str, api_key: str, cursor: Cursor) -> list[dict] | None:
  """History records newer than `cursor`, newest first.

  Pages backwards until it reaches the cursor rather than trusting one page:
  during an import burst a single 200-record page covers well under five
  minutes, and a missed record is a silently unrefreshed album.

  Sorted by `id`, not `date`. Date ordering is ambiguous here -- 600 live
  records held 125 distinct dates -- so a page boundary inside a shared second
  cannot be resolved by timestamp. Ids are unique and monotonic.

  Returns None if Lidarr is unreachable. Raises `HistoryExhausted` rather than
  returning a partial set, because a partial set used to be dispatched and then
  committed as if complete.
  """
  collected: list[dict] = []
  for page in range(1, MAX_HISTORY_PAGES + 1):
    url = (
      f"{host.rstrip('/')}/api/v1/history"
      f"?page={page}&pageSize={HISTORY_PAGE_SIZE}&sortKey=id&sortDirection=descending"
    )
    body = _get_json(url, {"X-Api-Key": api_key})
    if not isinstance(body, dict):
      # A mid-run failure used to return what had been collected so far, which
      # is the same silent truncation as running out of pages. Fail the run.
      return None
    records = body.get("records")
    if not isinstance(records, list):
      return None
    if not records:
      return collected  # genuinely reached the end of Lidarr's history
    collected.extend(records)
    if any(not cursor.is_new(record) for record in records):
      return collected
  raise HistoryExhausted(
    f"more than {MAX_HISTORY_PAGES * HISTORY_PAGE_SIZE} history records are newer "
    f"than the cursor (id={cursor.high_water_id}, date={cursor.date}). Dispatching "
    "only the newest of them would advance the cursor past the rest and lose them."
  )


def changed_folders(records: list[dict], cursor: Cursor) -> tuple[list[str], Cursor]:
  """Album folders touched after `cursor`, plus the cursor that now covers them.

  Returns Lidarr-side paths, de-duplicated and ordered oldest-first so a
  Jellyfin refresh sees them in the order they actually happened. The cursor
  advances over *every* new record, including non-file events, so a run that
  reports nothing still makes progress.
  """
  folders: list[str] = []
  seen: set[str] = set()

  # v0 migration. A date-only cursor also covers every record at or before that
  # date, so seed the high-water mark from their ids before processing. Without
  # this the cursor would persist as null and keep using date comparison, which
  # is the tie-prone test this change exists to remove.
  if cursor.high_water_id is None:
    covered = [
      record["id"]
      for record in records
      if not cursor.is_new(record) and isinstance(record.get("id"), int)
    ]
    if covered:
      cursor = Cursor(max(covered), cursor.date)

  # Oldest first. Sorting by id rather than reversing the input means the
  # caller does not have to hand them over pre-sorted.
  for record in sorted(records, key=lambda r: r.get("id") or 0):
    if not cursor.is_new(record):
      continue
    cursor = cursor.advanced_by(record)
    if record.get("eventType") not in FILE_EVENTS:
      continue
    data = record.get("data") or {}
    for field in PATH_FIELDS:
      value = data.get(field)
      if not value:
        continue
      folder = str(Path(str(value)).parent)
      if folder not in seen:
        seen.add(folder)
        folders.append(folder)
  return folders, cursor


def _roots(map_from: str | Sequence[str]) -> list[str]:
  """Media roots, longest first, so `/data/music` beats a broader `/data`."""
  values = [map_from] if isinstance(map_from, str) else list(map_from)
  return sorted((r.rstrip("/") for r in values), key=len, reverse=True)


def _matching_root(folder: str, roots: Sequence[str]) -> str | None:
  for root in roots:
    if folder == root or folder.startswith(root + "/"):
      return root
  return None


def translate(folders: list[str], map_from: str | Sequence[str], map_to: str) -> list[str]:
  """Rewrite Lidarr's media root to Jellyfin's. Paths outside every root drop.

  Dropping rather than passing through is deliberate: an untranslated path is
  exactly the silent no-op this script exists to fix, so it should be visible
  as a missing entry rather than a 204 that means nothing. `untranslated()` is
  what makes it visible.
  """
  roots = _roots(map_from)
  target = map_to.rstrip("/")
  out: list[str] = []
  for folder in folders:
    root = _matching_root(folder, roots)
    if root is not None:
      out.append(target + folder[len(root) :])
  return out


def untranslated(folders: list[str], map_from: str | Sequence[str]) -> list[str]:
  """The folders `translate()` had to drop — i.e. the ones Jellyfin never hears."""
  roots = _roots(map_from)
  return [f for f in folders if _matching_root(f, roots) is None]


def report_to_jellyfin(host: str, api_key: str, paths: list[str]) -> bool:
  """POST one Library/Media/Updated batch. True if Jellyfin accepted it."""
  payload = {"Updates": [{"Path": p, "UpdateType": "Modified"} for p in paths]}
  req = urllib.request.Request(
    f"{host.rstrip('/')}/Library/Media/Updated",
    data=json.dumps(payload).encode("utf-8"),
    method="POST",
    headers={
      "Content-Type": "application/json",
      "Authorization": f'MediaBrowser Token="{api_key}"',
    },
  )
  try:
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 - localhost
      return resp.status in (200, 202, 204)
  except (OSError, urllib.error.HTTPError):
    return False


def _bootstrap(first_run_minutes: int) -> Cursor:
  """A deliberately-requested lookback. Never reached by accident."""
  start = datetime.now(UTC) - timedelta(minutes=first_run_minutes)
  return Cursor(None, start.strftime("%Y-%m-%dT%H:%M:%SZ"))


def load_state(state_path: Path | None, first_run_minutes: int, *, bootstrap: bool) -> Cursor:
  """The cursor, or `StateError` explaining why it cannot be trusted.

  Every branch that once fell through to a lookback now raises. The four
  states below were verified 2026-09-04 to be byte-identical in the old code --
  all four printed `nothing to report` and exited 0.
  """
  if state_path is None:
    return _bootstrap(first_run_minutes)

  if not state_path.exists():
    if bootstrap:
      return _bootstrap(first_run_minutes)
    raise StateError(
      f"state file {state_path} does not exist. Refusing to invent a cursor: a "
      "lookback would skip every import older than it and then save itself over "
      "the gap. Bootstrap deliberately with --since-min N (or --bootstrap)."
    )

  try:
    raw = state_path.read_text()
  except OSError as exc:
    raise StateError(f"state file {state_path} could not be read: {exc}") from exc

  if not raw.strip():
    raise StateError(
      f"state file {state_path} is empty -- the signature of an interrupted "
      "write. It is not a fresh install; the cursor it held is unknown."
    )

  try:
    data = json.loads(raw)
  except json.JSONDecodeError as exc:
    raise StateError(
      f"state file {state_path} is not valid JSON ({exc}). A truncated file is "
      "what a non-atomic write leaves behind on a crash; the cursor is unknown."
    ) from exc

  if not isinstance(data, dict):
    raise StateError(f"state file {state_path} holds {type(data).__name__}, expected an object")

  version = data.get("schema_version")

  if version is None:
    # Pre-2026-09-04 format: {"cursor": "<iso8601>"}. Migrate on the next save.
    value = data.get("cursor")
    if not isinstance(value, str) or not value:
      raise StateError(
        f"state file {state_path} has no schema_version and no usable 'cursor' "
        "string, so it matches no format this script has ever written."
      )
    return Cursor(None, value)

  if not isinstance(version, int) or version > STATE_SCHEMA_VERSION:
    raise StateError(
      f"state file {state_path} declares schema_version {version!r}, newer than "
      f"the {STATE_SCHEMA_VERSION} this build understands. It was written by a "
      "later version; reading it as a stale cursor would re-dispatch history."
    )

  high_water_id = data.get("high_water_id")
  date = data.get("cursor_date")
  if not isinstance(date, str) or not date:
    raise StateError(f"state file {state_path} has no usable 'cursor_date'")
  if high_water_id is not None and not isinstance(high_water_id, int):
    raise StateError(
      f"state file {state_path} has high_water_id {high_water_id!r}, expected an int or null"
    )
  return Cursor(high_water_id, date)


def save_state(state_path: Path | None, cursor: Cursor) -> None:
  """Write the cursor atomically: temp file, fsync, then `os.replace`.

  `os.replace` is atomic on POSIX, so a reader sees either the old file or the
  new one and never the half-written file that produced `StateError` above.
  """
  if state_path is None:
    return
  payload = {
    "schema_version": STATE_SCHEMA_VERSION,
    "high_water_id": cursor.high_water_id,
    "cursor_date": cursor.date,
  }
  tmp = state_path.with_name(state_path.name + ".tmp")
  try:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with tmp.open("w", encoding="utf-8") as handle:
      json.dump(payload, handle)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(tmp, state_path)
  except OSError as exc:
    print(f"WARNING: could not write state file {state_path}: {exc}", file=sys.stderr)
    tmp.unlink(missing_ok=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description="Report Lidarr's file changes to Jellyfin with the paths translated.",
  )
  parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Cursor state file.")
  parser.add_argument(
    "--map-from",
    action="append",
    help=(
      "Lidarr's media root; repeatable. "
      f"Default: {' '.join(DEFAULT_MAP_FROM)} (both sides of the ADR-0003 repath)."
    ),
  )
  parser.add_argument("--map-to", default=DEFAULT_MAP_TO, help="Jellyfin's path for the same files.")
  parser.add_argument(
    "--since-min",
    type=int,
    default=None,
    help=(
      "Bootstrap a MISSING state file with this lookback, in minutes "
      f"(default {DEFAULT_FIRST_RUN_MINUTES} when --bootstrap is given). Passing "
      "this is what makes creating a cursor deliberate; without it an absent "
      "state file is fatal, so cron cannot silently re-base."
    ),
  )
  parser.add_argument(
    "--bootstrap",
    action="store_true",
    help="Permit creating a state file that does not exist. Implied by --since-min.",
  )
  parser.add_argument("--dry-run", action="store_true", help="Print paths, tell Jellyfin nothing.")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  lidarr_key = os.getenv("API_KEY_LIDARR")
  jellyfin_key = os.getenv("API_KEY_JELLYFIN")
  if not lidarr_key or not jellyfin_key:
    print("ERROR: API_KEY_LIDARR and API_KEY_JELLYFIN must both be set", file=sys.stderr)
    return 2

  try:
    cursor = load_state(
      args.state,
      args.since_min if args.since_min is not None else DEFAULT_FIRST_RUN_MINUTES,
      bootstrap=args.bootstrap or args.since_min is not None,
    )
  except StateError as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    return 2

  try:
    records = fetch_history(os.getenv("LIDARR_HOST", DEFAULT_LIDARR_HOST), lidarr_key, cursor)
  except HistoryExhausted as exc:
    print(f"ERROR: {exc}", file=sys.stderr)
    print(
      "  Cursor held. Raise MAX_HISTORY_PAGES, or bootstrap deliberately once the "
      "backlog is understood -- do not let it advance past unprocessed records.",
      file=sys.stderr,
    )
    return 2
  if records is None:
    print("ERROR: could not read Lidarr history", file=sys.stderr)
    return 2

  folders, new_cursor = changed_folders(records, cursor)
  roots = args.map_from or list(DEFAULT_MAP_FROM)
  paths = translate(folders, roots, args.map_to)
  unmapped = untranslated(folders, roots)

  if unmapped:
    # Fatal, not a warning: this is how the Kraftwerk imports were lost.
    print(
      f"ERROR: {len(unmapped)} folder(s) under none of {roots} — Lidarr's root "
      "folder has probably moved (ADR-0003). Cursor held so they are retried:",
      file=sys.stderr,
    )
    for folder in unmapped[:10]:
      print(f"  unmapped: {folder}", file=sys.stderr)

  if not paths:
    if unmapped:
      return 2
    print(f"nothing to report (cursor {cursor} -> {new_cursor})")
    if not args.dry_run:
      save_state(args.state, new_cursor)
    return 0

  for path in paths:
    print(f"changed: {path}")
  if args.dry_run:
    print(f"DRY-RUN would report {len(paths)} folder(s) to Jellyfin")
    return 2 if unmapped else 0

  if not report_to_jellyfin(os.getenv("JELLYFIN_HOST", DEFAULT_JELLYFIN_HOST), jellyfin_key, paths):
    print("ERROR: Jellyfin rejected the update; cursor not advanced", file=sys.stderr)
    return 2

  if unmapped:
    print(f"reported {len(paths)} folder(s) to Jellyfin; cursor held for the unmapped one(s)")
    return 2

  save_state(args.state, new_cursor)
  print(f"reported {len(paths)} folder(s) to Jellyfin (cursor {cursor} -> {new_cursor})")
  return 0


if __name__ == "__main__":
  sys.exit(main())
