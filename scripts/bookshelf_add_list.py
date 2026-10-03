#!/usr/bin/env python3
"""Bulk-add a reading list to a Bookshelf instance (Readarr v1 API).

Takes a plain list of "Title" or "Title | Author" lines (or a JSON array of
`{"title": ..., "author": ...}` objects) and, for each entry:

1. looks the book up on the shared metadata service (``book/lookup``),
2. looks its author up too (``author/lookup``, using the given author name
   when the caller supplied one -- the metadata service's own guess from the
   book result is frequently wrong for anthologies/compilations),
3. skips it if a book with that exact title already exists,
4. otherwise adds the book (which silently also adds its author, unmonitored
   and with no other books pulled in), and
5. force-monitors the one book, because adding an unmonitored author resets
   the just-added book back to unmonitored too -- a real behaviour of this
   Bookshelf version, not a bug in this script (verified 2026-09-28).

Finally triggers one ``BookSearch`` command for everything actually added.

The shared metadata service (``api.bookinfo.pro``, see ADR-0057) is flaky --
expect 503s and timeouts in bursts, not a sign anything here is broken. This
script retries each lookup a few times with backoff and keeps going past a
failed title rather than aborting the whole list; run it again later to
mop up anything still missing (it is idempotent -- already-added titles are
skipped, not duplicated).

A wrong top match is the one failure mode this script cannot detect for you:
skim the "added" lines against your list once it finishes, and drop any
mismatch with the removal command it will have printed for you.

Exit codes
----------
  0  every title was added (or already present)
  1  one or more titles could not be added (fatal 503s exhausted, no match, etc.)
  2  fatal: a required env var is unset, or the instance is unreachable

Usage
-----
  # plain text list, one book per line, "Title | Author" (author optional)
  python scripts/bookshelf_add_list.py --instance ebook --file list.txt

  # or audiobooks
  python scripts/bookshelf_add_list.py --instance audio --file list.txt

  # preview without writing anything
  python scripts/bookshelf_add_list.py --instance ebook --file list.txt --dry-run
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

RETRIES = 4
RETRY_SLEEP_S = 8
BETWEEN_BOOKS_SLEEP_S = 6
LOOKUP_TIMEOUT_S = 90
MONITOR_SETTLE_S = 5
MONITOR_ASSERT_RETRIES = 4

INSTANCES = {
  "ebook": {
    "base": "http://localhost:8787/api/v1",
    "key_env": "API_KEY_BOOKSHELF",
    "root": "/data/books/ebooks",
    "quality_profile": "eBook",
    "metadata_profile": "Standard",
    "is_ebook": True,
  },
  "audio": {
    "base": "http://localhost:8788/api/v1",
    "key_env": "API_KEY_BOOKSHELF_AUDIO",
    "root": "/data/books/audiobooks",
    "quality_profile": "Spoken",
    "metadata_profile": "Standard",
    "is_ebook": False,
  },
}


@dataclass(frozen=True)
class Entry:
  title: str
  author: str | None


@dataclass
class Report:
  added: list[str]
  skipped: list[str]
  failed: list[str]


def parse_list_file(path: str) -> list[Entry]:
  with open(path, encoding="utf-8") as fh:
    text = fh.read()
  stripped = text.strip()
  if stripped.startswith("["):
    data = json.loads(stripped)
    return [Entry(d["title"], d.get("author")) for d in data]
  entries = []
  for raw_line in text.splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
      continue
    if "|" in line:
      title, author = line.split("|", 1)
      entries.append(Entry(title.strip(), author.strip() or None))
    else:
      entries.append(Entry(line, None))
  return entries


def _request(method: str, base: str, key: str, path: str, payload: object | None = None,
             timeout: int = 60):
  data = json.dumps(payload).encode() if payload is not None else None
  req = urllib.request.Request(base + path, data=data, method=method, headers={
    "X-Api-Key": key, "Content-Type": "application/json", "Accept": "application/json",
  })
  last_exc: Exception | None = None
  for _ in range(RETRIES):
    try:
      with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
      return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
      if exc.code != 503:
        raise
      last_exc = exc
    except (TimeoutError, urllib.error.URLError) as exc:
      last_exc = exc
    time.sleep(RETRY_SLEEP_S)
  raise last_exc  # type: ignore[misc]


def already_have(base: str, key: str, title: str) -> dict | None:
  """Match on the exact title, or the existing book's title starting with
  ours (a subtitle variant, e.g. library already holds 'Blood Meridian, or,
  the Evening Redness in the West' for a wanted 'Blood Meridian') -- without
  this, the exact-match check misses real matches and a study-guide
  imposter gets added alongside the real book already on the shelf."""
  target = title.strip().lower()
  for b in _request("GET", base, key, "/book"):
    existing = b["title"].strip().lower()
    if existing == target or existing.startswith((target + ",", target + ":")):
      return b
  return None


# Cheap junk Goodreads returns for anything remotely canonical: student
# study-guide mills republish "Summary and Analysis of <Book>" as its own
# searchable title. Reproduced 2026-09-28: "Blood Meridian" matched one of
# these ahead of McCarthy's actual novel. Skip these results unless the
# wanted title itself is one (nobody adds a study guide by that name here).
_JUNK_MARKERS = ("summary", "study guide", "book review", "analysis of",
                 "summary and analysis", "summary & analysis")


def pick_book(results: list[dict], wanted_title: str) -> dict | None:
  wanted_has_junk_marker = any(m in wanted_title.lower() for m in _JUNK_MARKERS)
  if wanted_has_junk_marker:
    return results[0] if results else None
  for candidate in results:
    if not any(m in candidate["title"].lower() for m in _JUNK_MARKERS):
      return candidate
  return results[0] if results else None


def build_edition(book: dict, is_ebook: bool) -> dict:
  return {
    "foreignEditionId": book["foreignEditionId"],
    "titleSlug": book["titleSlug"],
    "isbn13": None, "asin": None,
    "title": book["title"],
    "language": None, "overview": "", "format": "",
    "isEbook": is_ebook,
    "disambiguation": "", "publisher": "",
    "pageCount": book.get("pageCount", 0),
    "releaseDate": book.get("releaseDate"),
    "images": book.get("images", []),
    "links": [],
    "ratings": book.get("ratings"),
    "monitored": True, "manualAdd": True, "grabbed": False,
  }


def add_entry(inst: dict, quality_profile_id: int, metadata_profile_id: int,
              entry: Entry) -> tuple[str, int | None]:
  base, key = inst["base"], os.environ[inst["key_env"]]

  # Author-qualify the search term whenever we have one: an unqualified
  # title search is what matched Machen's "The White People" to "Stuff White
  # People Like" and would have matched Lewis's "The Monk" to an unrelated
  # book too (both reproduced 2026-09-28). Adding the author name to the
  # query, not just using it to pick the author record afterward, is what
  # actually fixes it -- book/lookup has no separate author filter.
  book_term = f"{entry.title} {entry.author}" if entry.author else entry.title
  results = _request("GET", base, key, f"/book/lookup?term={urllib.parse.quote(book_term)}",
                      timeout=LOOKUP_TIMEOUT_S)
  if not results:
    return (f"NOT FOUND (book lookup): {book_term}", None)
  book = pick_book(results, entry.title)

  existing = already_have(base, key, book["title"])
  if existing is not None:
    return (f"already present: {book['title']} (id {existing['id']})", None)

  author_term = entry.author or book["authorTitle"].split(".")[0]
  authors = _request("GET", base, key, f"/author/lookup?term={urllib.parse.quote(author_term)}",
                      timeout=LOOKUP_TIMEOUT_S)
  if not authors:
    return (f"NOT FOUND (author lookup): {author_term} for {entry.title}", None)
  author = dict(authors[0])
  author["qualityProfileId"] = quality_profile_id
  author["metadataProfileId"] = metadata_profile_id
  author["rootFolderPath"] = inst["root"]
  author["monitored"] = False
  author["monitorNewItems"] = "none"
  author["addOptions"] = {"monitor": "none", "searchForMissingBooks": False, "monitored": False}

  body = dict(book)
  body["author"] = author
  body["monitored"] = True
  body["addOptions"] = {"searchForNewBook": True}
  body["editions"] = [build_edition(book, inst["is_ebook"])]

  try:
    result = _request("POST", base, key, "/book", body)
  except urllib.error.HTTPError as exc:
    return (f"FAILED to add {entry.title}: HTTP {exc.code} {exc.read()[:300]!r}", None)

  # Adding a fresh, unmonitored author resets the new book to unmonitored --
  # asynchronously, a beat *after* the 201 response, which itself still
  # claims monitored: true. A PUT /book/monitor issued immediately gets
  # overwritten by that settle a few seconds later (measured 2026-09-28:
  # true at t=0, false by t=3s). Wait it out, then assert and verify the
  # assertion actually stuck before trusting it.
  book_id = result["id"]
  time.sleep(MONITOR_SETTLE_S)
  monitored = False
  for _ in range(MONITOR_ASSERT_RETRIES):
    with contextlib.suppress(urllib.error.HTTPError):
      _request("PUT", base, key, "/book/monitor", {"bookIds": [book_id], "monitored": True})
    check = _request("GET", base, key, f"/book/{book_id}")
    monitored = check["monitored"]
    if monitored:
      break
    time.sleep(MONITOR_SETTLE_S)
  warn = "" if monitored else " -- WARNING: still unmonitored after retries, check manually"
  return (f"added: {result['title']} (id {result['id']}){warn} -- verify this matches "
          f"'{entry.title}'{' by ' + entry.author if entry.author else ''}; if not, "
          f"remove with: curl -X DELETE {base}/book/{result['id']}", result["id"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("--instance", choices=("ebook", "audio"), required=True)
  ap.add_argument("--file", required=True, help="plain-text or JSON list file")
  ap.add_argument("--dry-run", action="store_true", help="look up and report only, add nothing")
  return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  inst = INSTANCES[args.instance]
  if not os.environ.get(inst["key_env"]):
    print(f"ERROR: {inst['key_env']} is unset", file=sys.stderr)
    return 2

  entries = parse_list_file(args.file)
  if not entries:
    print(f"ERROR: no entries found in {args.file}", file=sys.stderr)
    return 2

  key = os.environ[inst["key_env"]]
  try:
    profiles = _request("GET", inst["base"], key, "/qualityprofile")
    meta_profiles = _request("GET", inst["base"], key, "/metadataprofile")
  except (OSError, urllib.error.URLError) as exc:
    print(f"FATAL: cannot reach {args.instance} instance: {exc}", file=sys.stderr)
    return 2
  quality_profile_id = next(p["id"] for p in profiles if p["name"] == inst["quality_profile"])
  metadata_profile_id = next(p["id"] for p in meta_profiles if p["name"] == inst["metadata_profile"])

  rep = Report(added=[], skipped=[], failed=[])
  added_ids: list[int] = []
  for entry in entries:
    if args.dry_run:
      print(f"DRY RUN: would look up '{entry.title}'"
            f"{' by ' + entry.author if entry.author else ''}")
      continue
    try:
      msg, book_id = add_entry(inst, quality_profile_id, metadata_profile_id, entry)
    except Exception as exc:  # noqa: BLE001 -- keep going past one bad title
      msg, book_id = f"ERROR on {entry.title}: {exc!r}", None
    print(msg, flush=True)
    if book_id is not None:
      rep.added.append(msg)
      added_ids.append(book_id)
    elif msg.startswith("already present"):
      rep.skipped.append(msg)
    else:
      rep.failed.append(msg)
    time.sleep(BETWEEN_BOOKS_SLEEP_S)

  if args.dry_run:
    return 0

  if added_ids:
    print(f"\nTriggering BookSearch for {len(added_ids)} newly added book(s)...")
    _request("POST", inst["base"], key, "/command", {"name": "BookSearch", "bookIds": added_ids})

  print(f"\n{len(rep.added)} added, {len(rep.skipped)} already present, "
        f"{len(rep.failed)} failed")
  for line in rep.failed:
    print(f"  FAILED: {line}", file=sys.stderr)
  return 1 if rep.failed else 0


if __name__ == "__main__":
  sys.exit(main())
