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
import urllib.error
import urllib.parse
import urllib.request

JELLYFIN = "http://localhost:8096"
BOOKSHELF = "http://localhost:8787/api/v1"
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


def main() -> int:
  jf_key, bs_key = os.environ.get("API_KEY_JELLYFIN"), os.environ.get("API_KEY_BOOKSHELF")
  if not jf_key or not bs_key:
    print("FATAL: API_KEY_JELLYFIN and API_KEY_BOOKSHELF must be set", file=sys.stderr)
    return 2
  jf = {"Authorization": f'MediaBrowser Token="{jf_key}"'}
  try:
    findings = library_findings(_get(f"{JELLYFIN}/Library/VirtualFolders", jf))
    findings += plugin_findings(_get(f"{JELLYFIN}/Plugins", jf))
  except (OSError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: Jellyfin unreachable: {exc}", file=sys.stderr)
    return 2
  try:
    hits = _get(f"{BOOKSHELF}/author/lookup?term={urllib.parse.quote(PROBE_AUTHOR)}",
                {"X-Api-Key": bs_key}, timeout=90) or []
    if not any(PROBE_AUTHOR.lower() in (h.get("authorName") or "").lower() for h in hits):
      findings.append(f"Bookshelf's metadata source returned no {PROBE_AUTHOR!r} "
                      f"({len(hits)} hits) -- author search and refresh are dead")
  except urllib.error.HTTPError as exc:
    findings.append(f"Bookshelf author lookup failed: HTTP {exc.code} -- metadata source down?")
  except (OSError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: Bookshelf unreachable: {exc}", file=sys.stderr)
    return 2

  for f in findings:
    print(f"    !!! {f}")
  if not findings:
    print("    ok: Books + Audiobooks libraries, Bookshelf plugin, metadata lookup")
  return 1 if findings else 0


if __name__ == "__main__":
  sys.exit(main())
