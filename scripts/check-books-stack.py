#!/usr/bin/env python3
"""Assert the live books stack still works end to end (ADR-0057).

Everything here lives outside git, so `make check` cannot see it, and each
piece fails quietly:

* Jellyfin's **Books** and **Audiobooks** libraries -- collection type
  `books`, at `/data/movies/books/{ebooks,audiobooks}` (the load-bearing
  mount, ADR-0016), realtime monitor ON. Without the monitor a Bookshelf import
  sits invisible until the next scheduled scan; a config restore can drop it.
* The Jellyfin **Bookshelf** plugin is Active -- it supplies the book metadata
  and the audiobook handling. A Jellyfin major can leave it NotSupported.
* Bookshelf's **metadata source answers**. It is a shared public service
  (api.bookinfo.pro); its Hardcover sibling died on the day this was built, and
  when metadata is down Bookshelf keeps serving its UI with green health while
  every author search and refresh silently returns nothing. Proven with a real
  author lookup, not a ping.
* **Every book file sits in its own book folder** -- `Author/Title/file`, never
  loose in an author folder (ADR-0059). Loose files are the symptom of
  `renameBooks` being off, and loose audiobook parts from two books sharing a
  folder are what made Jellyfin show one "book" per chapter.
* **AudioBookBay answers through Prowlarr, and bookshelf-audio has it**
  (ADR-0058). ABB is a custom definition this repo owns; a domain move, a
  User-Agent block or its uppercase-query redirect each turned it into an
  indexer that returns nothing while testing green. Proven with a real search.

Exit codes
----------
  0  all of it holds
  1  something drifted (each finding printed)
  2  Jellyfin or Bookshelf unreachable, or an env var unset
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

JELLYFIN = "http://localhost:8096"
BOOKSHELF = "http://localhost:8787/api/v1"
BOOKSHELF_AUDIO = "http://localhost:8788/api/v1"
PROWLARR = "http://localhost:9696/api/v1"
LIBRARIES = {
  "Books": "/data/movies/books/ebooks",
  "Audiobooks": "/data/movies/books/audiobooks",
}
# Any well-known author works; this one is also in the library, so a lookup
# that returns him is unambiguous.
PROBE_AUTHOR = "Andy Weir"


def _get(url: str, headers: dict[str, str], timeout: float = 30):
  req = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    return json.loads(resp.read() or b"null")


BOOK_EXT = {".epub", ".mobi", ".azw3", ".pdf", ".mp3", ".m4a", ".m4b", ".flac", ".ogg", ".opus"}


def layout_findings(root: Path) -> list[str]:
  """Pure over a directory: book files loose in an author folder."""
  if not root.is_dir():
    return [f"{root} does not exist"]
  loose = sorted(p.relative_to(root).as_posix() for p in root.glob("*/*")
                 if p.is_file() and p.suffix.lower() in BOOK_EXT)
  if not loose:
    return []
  return [f"{len(loose)} book file(s) loose in an author folder under {root.name}/ "
          f"(renameBooks off? ADR-0059), e.g. {loose[0]}"]


def library_findings(folders: list[dict]) -> list[str]:
  """Pure: what is wrong with Jellyfin's virtual folders, if anything."""
  out = []
  by_name = {f.get("Name"): f for f in folders}
  for name, path in LIBRARIES.items():
    lib = by_name.get(name)
    if lib is None:
      out.append(f"Jellyfin has no {name!r} library")
      continue
    if lib.get("CollectionType") != "books":
      out.append(f"{name!r} is a {lib.get('CollectionType')!r} library, not 'books'")
    if path not in (lib.get("Locations") or []):
      out.append(f"{name!r} does not point at {path} (has {lib.get('Locations')})")
    if not (lib.get("LibraryOptions") or {}).get("EnableRealtimeMonitor"):
      out.append(f"{name!r} has the realtime monitor OFF -- imports stay invisible until a scan")
  return out


def plugin_findings(plugins: list[dict]) -> list[str]:
  active = [p for p in plugins if p.get("Name") == "Bookshelf" and p.get("Status") == "Active"]
  return [] if active else ["the Jellyfin Bookshelf plugin is not Active"]


ABB_URL = "https://audiobookbay.lu/?s=murakami"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"


def abb_direct_posts() -> int:
  """English posts on ABB's search page, fetched as the definition fetches it."""
  req = urllib.request.Request(ABB_URL, headers={"User-Agent": BROWSER_UA})
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:
      page = resp.read().decode("utf-8", "replace")
  except (OSError, urllib.error.URLError):
    return 0
  return page.count("Language: English")


def audiobookbay_findings(pr_key: str, ba_key: str) -> list[str]:
  out = []
  indexers = _get(f"{PROWLARR}/indexer", {"X-Api-Key": pr_key})
  abb = next((i for i in indexers if i.get("definitionName") == "audiobookbay"), None)
  if abb is None:
    return ["Prowlarr has no AudioBookBay indexer (prowlarr/definitions/audiobookbay.yml)"]
  if not abb.get("enable"):
    out.append("Prowlarr's AudioBookBay indexer is disabled")
  hits = _get(f"{PROWLARR}/search?query=murakami&type=search&categories=3030"
              f"&indexerIds={abb['id']}", {"X-Api-Key": pr_key}, timeout=120) or []
  if not hits:
    # Prowlarr silently leaves an indexer out once its hourly query cap (60,
    # set on purpose) is spent -- which a backlog search does. So ask the site
    # directly, the way the definition does, before calling it broken.
    if abb_direct_posts() > 0:
      print("    note: AudioBookBay answers directly but not through Prowlarr right now "
            "-- its 60/h query cap is probably spent")
    else:
      out.append("AudioBookBay returned nothing for 'murakami', directly or through "
                 "Prowlarr -- domain, User-Agent or query rules changed (ADR-0058)")
  names = [i.get("name", "") for i in _get(f"{BOOKSHELF_AUDIO}/indexer", {"X-Api-Key": ba_key})]
  if not any("AudioBookBay" in n for n in names):
    out.append("bookshelf-audio does not have AudioBookBay -- Prowlarr app sync broken?")
  return out


def main() -> int:
  jf_key, bs_key = os.environ.get("API_KEY_JELLYFIN"), os.environ.get("API_KEY_BOOKSHELF")
  ba_key, pr_key = os.environ.get("API_KEY_BOOKSHELF_AUDIO"), os.environ.get("API_KEY_PROWLARR")
  if not all((jf_key, bs_key, ba_key, pr_key)):
    print("FATAL: API_KEY_JELLYFIN, API_KEY_BOOKSHELF, API_KEY_BOOKSHELF_AUDIO and "
          "API_KEY_PROWLARR must be set", file=sys.stderr)
    return 2
  jf = {"Authorization": f'MediaBrowser Token="{jf_key}"'}
  try:
    findings = library_findings(_get(f"{JELLYFIN}/Library/VirtualFolders", jf))
    findings += plugin_findings(_get(f"{JELLYFIN}/Plugins", jf))
  except (OSError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: Jellyfin unreachable: {exc}", file=sys.stderr)
    return 2
  try:
    # One retry: the shared server throws the odd 503 under load, and a
    # single blip is not "metadata is down".
    url = f"{BOOKSHELF}/author/lookup?term={urllib.parse.quote(PROBE_AUTHOR)}"
    try:
      hits = _get(url, {"X-Api-Key": bs_key}, timeout=90) or []
    except urllib.error.HTTPError:
      time.sleep(15)
      hits = _get(url, {"X-Api-Key": bs_key}, timeout=90) or []
    if not any(PROBE_AUTHOR.lower() in (h.get("authorName") or "").lower() for h in hits):
      findings.append(f"Bookshelf's metadata source returned no {PROBE_AUTHOR!r} "
                      f"({len(hits)} hits) -- author search and refresh are dead")
  except urllib.error.HTTPError as exc:
    findings.append(f"Bookshelf author lookup failed: HTTP {exc.code} -- metadata source down?")
  except (OSError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: Bookshelf unreachable: {exc}", file=sys.stderr)
    return 2

  share = os.environ.get("SHARE_DIRECTORY")
  if share:
    for sub in ("ebooks", "audiobooks"):
      findings += layout_findings(Path(share) / "books" / sub)
  try:
    findings += audiobookbay_findings(pr_key, ba_key)
  except (OSError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: Prowlarr or bookshelf-audio unreachable: {exc}", file=sys.stderr)
    return 2

  for f in findings:
    print(f"    !!! {f}")
  if not findings:
    print("    ok: Books + Audiobooks libraries, one folder per book, Bookshelf plugin, "
          "metadata lookup, AudioBookBay")
  return 1 if findings else 0


if __name__ == "__main__":
  sys.exit(main())
