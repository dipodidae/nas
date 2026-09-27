#!/usr/bin/env python3
"""Fetch the ebooks Bookshelf is missing from Z-Library's catalogue, and import them.

Why a bridge and not an indexer
-------------------------------
Bookshelf (the Readarr revival) only knows torrent and usenet indexers, and
nothing publishes Z-Library as one that works (ADR-0057 has the librarr spike:
its Torznab feed 404s every direct download). So Bookshelf stays the brain --
which authors, which books, which edition, where they live -- and this fills
the one gap it cannot:

  1. Ask Bookshelf for monitored books that are still missing, are not in its
     download queue, and were added more than ``--grace-hours`` ago, so the
     torrent search always gets the first go.
  2. Search Z-Library's JSON API (``z-lib.gd`` -- the domains behind a DiamWall
     browser challenge cannot be scripted) and score each hit: ISBN overlap
     with Bookshelf's editions, title, author, language, format (EPUB first).
  3. Download the winner **by md5**: from a LibGen mirror when it has that md5
     (same file, no daily cap), otherwise from Z-Library itself, which costs one
     of the account's 10 downloads a day. The file is re-hashed on arrival; a
     byte that differs from the catalogue md5 is discarded.
  4. Tell Bookshelf to import exactly that file as exactly that book
     (ManualImport with explicit author/book/edition ids, move mode -- the
     staging dir is on the same filesystem, so it is a rename). Bookshelf
     renames it into the library. Bookshelf's own connector stays silent here
     -- it fires only for downloads it tracked, never for a ManualImport
     (measured) -- so this script publishes the nas-media message itself,
     the same arrangement as process_soulseek_imports.py for Lidarr.

The md5 is the proof of identity, which is why no embedded-title heuristic is
applied: a real edition of Project Hail Mary carries the dc:title "A Novel".

Z-Library downloads are the scarce resource (10 a day), so they are spent only
on a file LibGen does not have. When LibGen has the md5 but no mirror serves
it right now -- they 503 and time out in bursts -- the book is DEFERRED for
``--transient-hours`` instead, as it is when the day's quota is gone; a
deferral never counts toward giving up. A real failure (no acceptable match,
a refused import) waits ``--retry-hours`` and counts, at most ``--max-tries``
times; running out publishes one nas-attention message per book, never a
repeat. State: ``logs/bookshelf-zlib-bridge.json``.

Exit codes
----------
  0  nothing to do, or every attempted book was imported (or --dry-run)
  1  partial: at least one book could not be found, fetched or imported
  2  fatal: Bookshelf unreachable, or a required env var unset

Environment
-----------
  API_KEY_BOOKSHELF, SHARE_DIRECTORY        (required)
  ZLIBRARY_EMAIL, ZLIBRARY_PASSWORD         (optional; without them only LibGen)

Usage
-----
  python scripts/bookshelf_zlib_bridge.py --dry-run          # show the plan
  python scripts/bookshelf_zlib_bridge.py                    # act (cron)
  python scripts/bookshelf_zlib_bridge.py --book-id 12       # one book, now
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Cron does not source .env; load it the way the other *arr scripts do.
if "API_KEY_BOOKSHELF" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO / ".env")
  except ImportError:
    pass

BOOKSHELF = "http://localhost:8787/api/v1"
ZLIB = "https://z-lib.gd"
# libgen.li answers nothing from here; these four served the same md5 on
# 2026-09-27. Tried in order, first hit wins.
LIBGEN_MIRRORS = ("https://libgen.la", "https://libgen.bz", "https://libgen.gl", "https://libgen.vg")
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
STATE_PATH = REPO / "logs" / "bookshelf-zlib-bridge.json"
# ${SHARE_DIRECTORY}/downloads/zlib-bridge on the host is
# /data/downloads/zlib-bridge inside Bookshelf: one filesystem, so the import
# is a rename.
STAGING_SUBDIR = Path("downloads") / "zlib-bridge"
BOOKSHELF_STAGING = "/data/downloads/zlib-bridge"
MAX_BYTES = 200 * 1024 * 1024

# Higher is better.
FORMAT_SCORE = {"epub": 30, "azw3": 18, "mobi": 15, "pdf": 5}
LANGUAGE_OK = {"english", "dutch"}
MIN_SCORE = 60


@dataclass(frozen=True)
class Wanted:
  book_id: int
  author_id: int
  title: str
  author: str
  edition_id: str        # Bookshelf's foreignEditionId of the monitored edition
  isbns: frozenset[str]
  added: float           # epoch seconds


@dataclass(frozen=True)
class Candidate:
  zlib_id: int
  zlib_hash: str
  md5: str
  title: str
  author: str
  extension: str
  language: str
  filesize: int
  isbns: frozenset[str]


@dataclass(frozen=True)
class Result:
  reason: str = ""        # '' means imported
  source: str = ""
  transient: bool = False


@dataclass
class Outcome:
  imported: list[str] = field(default_factory=list)
  failed: list[str] = field(default_factory=list)


# --- pure logic --------------------------------------------------------------


def normalize(text: str) -> str:
  """Lowercase, strip accents, punctuation and a leading article."""
  text = unicodedata.normalize("NFKD", text or "")
  text = "".join(c for c in text if not unicodedata.combining(c)).lower()
  text = re.sub(r"[^a-z0-9 ]+", " ", text)
  text = re.sub(r"\s+", " ", text).strip()
  return re.sub(r"^(the|a|an|de|het|een) ", "", text)


def main_title(title: str) -> str:
  """The title without its subtitle or series suffix -- 'Dune (Dune #1)',
  'Sapiens: A Brief History' -- which catalogues disagree on."""
  return re.split(r"[:(\[]| - ", title or "", maxsplit=1)[0]


def isbns_in(text: str | None) -> frozenset[str]:
  return frozenset(re.findall(r"\b(?:97[89])?\d{9}[\dX]\b", (text or "").replace("-", "")))


def title_similarity(a: str, b: str) -> float:
  na, nb = normalize(main_title(a)), normalize(main_title(b))
  if not na or not nb:
    return 0.0
  return SequenceMatcher(None, na, nb).ratio()


def author_matches(wanted: str, found: str) -> bool:
  """Surname match. Catalogues write 'Weir, Andy', 'Andy Weir', 'A. Weir'."""
  surname = normalize(wanted).split(" ")[-1] if normalize(wanted) else ""
  return bool(surname) and surname in normalize(found).split(" ")


def score(want: Wanted, cand: Candidate) -> int:
  """0-100ish. An ISBN hit is decisive; otherwise title + author must carry it."""
  ext = cand.extension.lower()
  if ext not in FORMAT_SCORE:
    return 0
  lang = (cand.language or "").lower()
  if lang and lang not in LANGUAGE_OK:
    return 0
  isbn_hit = bool(want.isbns & cand.isbns)
  sim = title_similarity(want.title, cand.title)
  if not isbn_hit and (sim < 0.85 or not author_matches(want.author, cand.author)):
    return 0
  total = FORMAT_SCORE[ext] + int(sim * 40)
  total += 25 if isbn_hit else 0
  total += 10 if author_matches(want.author, cand.author) else 0
  total += 5 if lang == "english" else 0
  return total


def pick(want: Wanted, cands: list[Candidate]) -> Candidate | None:
  best = max(cands, key=lambda c: score(want, c), default=None)
  return best if best is not None and score(want, best) >= MIN_SCORE else None


def due(state: dict, book_id: int, now: float, max_tries: int) -> bool:
  """Eligible unless it has used up its tries or its next attempt is later."""
  entry = state.get(str(book_id))
  if not entry:
    return True
  return entry.get("tries", 0) < max_tries and now >= entry.get("next", 0)


def record_failure(entry: dict, result: Result, now: float, retry_s: float,
                   transient_s: float) -> None:
  """A transient failure (LibGen has the file but no mirror served it, or the
  Z-Library quota is spent) retries soon and does not count toward giving up;
  anything else waits `retry_s` and does."""
  entry["reason"], entry["last"] = result.reason, now
  if result.transient:
    entry["next"] = now + transient_s
  else:
    entry["tries"] = entry.get("tries", 0) + 1
    entry["next"] = now + retry_s


def parse_wanted(record: dict, editions: list[dict]) -> Wanted | None:
  """A wanted/missing record carries no editions of its own (measured): the
  monitored edition's id is top-level and the ISBNs come from /edition."""
  monitored = next((e for e in editions if e.get("monitored")), None)
  edition_id = str((monitored or {}).get("foreignEditionId") or record.get("foreignEditionId") or "")
  if not edition_id or not record.get("id") or not record.get("authorId"):
    return None
  isbns = set()
  for e in editions:
    isbns |= isbns_in(e.get("isbn13"))
    isbns |= isbns_in(e.get("asin"))
  return Wanted(
    book_id=record["id"], author_id=record["authorId"], title=record.get("title", ""),
    author=(record.get("author") or {}).get("authorName") or record.get("authorTitle", ""),
    edition_id=edition_id,
    isbns=frozenset(isbns), added=parse_added(record.get("added")),
  )


def parse_added(value: str | None) -> float:
  try:
    return datetime.fromisoformat((value or "").replace("Z", "+00:00")).timestamp()
  except ValueError:
    return 0.0


def parse_candidates(payload: dict) -> list[Candidate]:
  out = []
  for b in payload.get("books") or []:
    if not b.get("md5") or not b.get("id"):
      continue
    out.append(Candidate(
      zlib_id=int(b["id"]), zlib_hash=str(b.get("hash") or ""), md5=b["md5"].lower(),
      title=b.get("title") or "", author=b.get("author") or "",
      extension=(b.get("extension") or "").lower(), language=b.get("language") or "",
      filesize=int(b.get("filesize") or 0), isbns=isbns_in(b.get("identifier")),
    ))
  return out


# --- I/O -----------------------------------------------------------------------


def _opener(jar: http.cookiejar.CookieJar | None = None) -> urllib.request.OpenerDirector:
  handlers: list = [urllib.request.HTTPCookieProcessor(jar)] if jar is not None else []
  return urllib.request.build_opener(*handlers)


def _http(method: str, url: str, *, headers: dict[str, str] | None = None,
          json_body: object | None = None, form: dict | None = None, timeout: float = 60,
          opener: urllib.request.OpenerDirector | None = None):
  data, hdrs = None, {"User-Agent": UA, "Accept": "application/json", **(headers or {})}
  if json_body is not None:
    data, hdrs["Content-Type"] = json.dumps(json_body).encode(), "application/json"
  elif form is not None:
    data = urllib.parse.urlencode(form).encode()
    hdrs["Content-Type"] = "application/x-www-form-urlencoded"
  req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
  with (opener or _opener()).open(req, timeout=timeout) as resp:
    body = resp.read()
  return json.loads(body) if body else None


class Bookshelf:
  def __init__(self, key: str, base: str = BOOKSHELF) -> None:
    self.key, self.base = key, base

  def call(self, method: str, path: str, body: object | None = None, timeout: float = 60):
    return _http(method, self.base + path, headers={"X-Api-Key": self.key},
                 json_body=body, timeout=timeout)

  def missing(self) -> list[dict]:
    out, page = [], 1
    while True:
      data = self.call("GET", f"/wanted/missing?page={page}&pageSize=100&monitored=true"
                              "&includeAuthor=true&sortKey=books.added&sortDirection=ascending")
      out += data.get("records") or []
      if page * 100 >= int(data.get("totalRecords") or 0):
        return out
      page += 1

  def editions(self, book_id: int) -> list[dict]:
    return self.call("GET", f"/edition?bookId={book_id}") or []

  def queued_book_ids(self) -> set[int]:
    data = self.call("GET", "/queue?pageSize=1000&includeUnknownAuthorItems=false")
    return {r["bookId"] for r in data.get("records") or [] if r.get("bookId")}

  def import_file(self, want: Wanted, path: str) -> bool:
    """ManualImport exactly this file as exactly this book, then confirm it."""
    folder = path.rsplit("/", 1)[0]
    items = self.call("GET", "/manualimport?folder=" + urllib.parse.quote(folder)
                      + "&filterExistingFiles=false&replaceExistingFiles=false", timeout=120)
    item = next((i for i in items or [] if i.get("path") == path), None)
    if item is None:
      raise RuntimeError(f"Bookshelf does not see {path}")
    cmd = self.call("POST", "/command", {
      "name": "ManualImport", "importMode": "move", "replaceExistingFiles": False,
      "files": [{
        "path": path, "authorId": want.author_id, "bookId": want.book_id,
        "foreignEditionId": want.edition_id, "quality": item.get("quality"),
        "indexerFlags": 0, "downloadId": "", "disableReleaseSwitching": True,
      }],
    })
    for _ in range(60):
      time.sleep(2)
      if self.call("GET", f"/command/{cmd['id']}").get("status") in ("completed", "failed", "aborted"):
        break
    book = self.call("GET", f"/book/{want.book_id}")
    return int((book.get("statistics") or {}).get("bookFileCount") or 0) > 0


class ZLibrary:
  """Search is anonymous; a download needs the account, logged in lazily so a
  run that finds everything on LibGen never touches the login."""

  def __init__(self, email: str, password: str) -> None:
    self.email, self.password = email, password
    self.jar = http.cookiejar.CookieJar()
    self.opener = _opener(self.jar)
    self.logged_in = False

  def search(self, query: str) -> list[Candidate]:
    payload = _http("POST", f"{ZLIB}/eapi/book/search", form={"message": query, "limit": 25},
                    timeout=30, opener=self.opener)
    return parse_candidates(payload or {})

  def _login(self) -> None:
    data = _http("POST", f"{ZLIB}/eapi/user/login", timeout=30, opener=self.opener,
                 form={"email": self.email, "password": self.password})
    user = (data or {}).get("user") or {}
    if not (data or {}).get("success") or not user.get("remix_userkey"):
      raise RuntimeError(f"Z-Library login refused: {(data or {}).get('error', 'no user in reply')}")
    domain = urllib.parse.urlsplit(ZLIB).hostname or ""
    for name, value in (("remix_userid", str(user["id"])), ("remix_userkey", user["remix_userkey"])):
      self.jar.set_cookie(http.cookiejar.Cookie(
        0, name, value, None, False, domain, False, False, "/", True, True, None, False,
        None, None, {}))
    self.logged_in = True

  def downloads_left(self) -> int:
    if not self.email or not self.password:
      return 0
    if not self.logged_in:
      self._login()
    user = (_http("GET", f"{ZLIB}/eapi/user/profile", timeout=30, opener=self.opener)
            or {}).get("user") or {}
    return int(user.get("downloads_limit") or 0) - int(user.get("downloads_today") or 0)

  def download_link(self, cand: Candidate) -> str:
    if not self.email or not self.password:
      raise RuntimeError("no Z-Library account configured (ZLIBRARY_EMAIL/PASSWORD)")
    if not self.logged_in:
      self._login()
    data = _http("GET", f"{ZLIB}/eapi/book/{cand.zlib_id}/{cand.zlib_hash}/file", timeout=30,
                 opener=self.opener)
    link = ((data or {}).get("file") or {}).get("downloadLink") or ""
    if not link:
      raise RuntimeError(f"Z-Library gave no download link: {(data or {}).get('error', 'quota?')}")
    return link


def libgen_links(md5: str):
  """Yield a get.php URL from each mirror that has this exact md5, lazily --
  a mirror can hand out a key and then 503 the file (libgen.la, measured), so
  the caller moves on to the next one rather than to Z-Library's quota."""
  for mirror in LIBGEN_MIRRORS:
    try:
      req = urllib.request.Request(f"{mirror}/ads.php?md5={md5}", headers={"User-Agent": UA})
      with urllib.request.urlopen(req, timeout=20) as resp:
        page = resp.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError):
      continue
    m = re.search(r"get\.php\?md5=" + md5 + r"&(?:amp;)?key=\w+", page, re.I)
    if m:
      yield f"{mirror}/{m.group(0).replace('&amp;', '&')}"


def download(url: str, dest: Path, md5: str) -> None:
  """Stream to dest, hashing on the way; a size or md5 mismatch deletes it."""
  dest.parent.mkdir(parents=True, exist_ok=True)
  digest, size = hashlib.md5(), 0
  tmp = dest.with_suffix(dest.suffix + ".part")
  req = urllib.request.Request(url, headers={"User-Agent": UA})
  try:
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
      while chunk := resp.read(1 << 16):
        size += len(chunk)
        if size > MAX_BYTES:
          raise RuntimeError(f"refusing a file over {MAX_BYTES} bytes")
        digest.update(chunk)
        fh.write(chunk)
    if digest.hexdigest() != md5:
      raise RuntimeError(f"md5 mismatch: got {digest.hexdigest()}, wanted {md5} ({size} bytes)")
    tmp.replace(dest)
  finally:
    tmp.unlink(missing_ok=True)


def load_state(path: Path) -> dict:
  try:
    return json.loads(path.read_text())
  except (OSError, ValueError):
    return {}


def save_state(path: Path, state: dict) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_suffix(".tmp")
  tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
  tmp.replace(path)


def safe_name(text: str) -> str:
  return re.sub(r"[^\w .,'()-]+", "", text).strip()[:120] or "book"


def attempt(want: Wanted, bs: Bookshelf, zl: ZLibrary, staging: Path) -> Result:
  """One book, end to end."""
  cands = zl.search(f"{main_title(want.title)} {want.author}")
  best = pick(want, cands)
  if best is None:
    return Result(f"no acceptable Z-Library match among {len(cands)} hits")
  name = f"{safe_name(want.author)} - {safe_name(want.title)}.{best.extension}"
  dest = staging / best.md5 / name
  source, errors, on_libgen = "", [], False
  for link in libgen_links(best.md5):
    on_libgen = True
    try:
      download(link, dest, best.md5)
      source = "LibGen"
      break
    except (OSError, urllib.error.URLError, RuntimeError) as exc:
      host = urllib.parse.urlsplit(link).hostname
      errors.append(f"LibGen {host}: {exc}")
      print(f"    LibGen {host} failed: {exc}")
  if not source and on_libgen:
    # LibGen HAS this exact file and only failed to serve it just now. Do not
    # spend one of ten daily Z-Library downloads on a flaky mirror.
    return Result("; ".join(errors), transient=True)
  if not source:
    try:
      if zl.downloads_left() <= 0:
        return Result("not on LibGen, and today's Z-Library downloads are spent",
                      transient=True)
      download(zl.download_link(best), dest, best.md5)
      source = "Z-Library"
    except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
      return Result(f"not on LibGen; Z-Library: {exc}")
  container_path = f"{BOOKSHELF_STAGING}/{best.md5}/{name}"
  if not bs.import_file(want, container_path):
    return Result(f"Bookshelf did not import {container_path}", source)
  # The md5-named folder is empty once Bookshelf has moved the file out.
  with contextlib.suppress(OSError):
    dest.parent.rmdir()
  return Result(source=source)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
  ap.add_argument("--dry-run", action="store_true", help="search and score, fetch nothing")
  ap.add_argument("--max-books", type=int, default=5, help="books attempted per run (default 5)")
  ap.add_argument("--grace-hours", type=float, default=6.0,
                  help="leave a newly added book to the torrent search this long (default 6)")
  ap.add_argument("--retry-hours", type=float, default=24.0,
                  help="wait after a real failure (default 24)")
  ap.add_argument("--transient-hours", type=float, default=1.0,
                  help="wait after a flaky mirror or a spent quota (default 1)")
  ap.add_argument("--max-tries", type=int, default=5)
  ap.add_argument("--book-id", type=int, action="append",
                  help="only these Bookshelf book ids; ignores grace and retry state")
  return ap.parse_args(argv)


def select(records: list[dict], queued: set[int], state: dict, args: argparse.Namespace,
           now: float) -> list[dict]:
  """Pure: which missing records are eligible this run, before editions are read."""
  out = []
  for rec in records:
    book_id = rec.get("id")
    if book_id in queued:
      continue
    if args.book_id:
      if book_id in args.book_id:
        out.append(rec)
      continue
    if not due(state, book_id, now, args.max_tries):
      continue
    if now - parse_added(rec.get("added")) < args.grace_hours * 3600:
      continue
    out.append(rec)
  return out


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  env = os.environ
  missing_env = [k for k in ("API_KEY_BOOKSHELF", "SHARE_DIRECTORY") if not env.get(k)]
  if missing_env:
    print(f"FATAL: unset: {missing_env}", file=sys.stderr)
    return 2
  staging = Path(env["SHARE_DIRECTORY"]) / STAGING_SUBDIR
  bs = Bookshelf(env["API_KEY_BOOKSHELF"])
  zl = ZLibrary(env.get("ZLIBRARY_EMAIL", ""), env.get("ZLIBRARY_PASSWORD", ""))

  try:
    records = bs.missing()
    queued = bs.queued_book_ids()
  except (OSError, urllib.error.URLError, ValueError) as exc:
    print(f"FATAL: Bookshelf unreachable: {exc}", file=sys.stderr)
    return 2

  now = time.time()
  state = load_state(STATE_PATH)
  todo: list[Wanted] = []
  for rec in select(records, queued, state, args, now):
    try:
      want = parse_wanted(rec, bs.editions(rec["id"]))
    except (OSError, urllib.error.URLError, ValueError) as exc:
      print(f"  ? book {rec.get('id')}: could not read its editions: {exc}")
      continue
    if want is not None:
      todo.append(want)
    if len(todo) >= args.max_books:
      break
  print(f"{len(records)} missing, {len(queued)} queued in Bookshelf, {len(todo)} to attempt")

  out = Outcome()
  for want in todo:
    label = f"{want.author} — {want.title} (book {want.book_id})"
    if args.dry_run:
      try:
        best = pick(want, zl.search(f"{main_title(want.title)} {want.author}"))
      except (OSError, urllib.error.URLError, ValueError) as exc:
        print(f"  ? {label}: search failed: {exc}")
        continue
      where = ("LibGen" if best and next(libgen_links(best.md5), None) else "Z-Library") if best else ""
      print(f"  {'+' if best else '-'} {label}: "
            + (f"{best.extension} {best.language} {best.filesize}B md5={best.md5} "
               f"score={score(want, best)} via {where}" if best else "no acceptable match"))
      continue
    try:
      result = attempt(want, bs, zl, staging)
    except (OSError, urllib.error.URLError, ValueError, RuntimeError) as exc:
      result = Result(f"{type(exc).__name__}: {exc}", transient=isinstance(exc, OSError))
    if result.reason:
      entry = state.setdefault(str(want.book_id), {"tries": 0, "title": label})
      record_failure(entry, result, now, args.retry_hours * 3600, args.transient_hours * 3600)
      out.failed.append(f"{label}: {result.reason}")
      print(f"  {'DEFERRED' if result.transient else 'FAILED'} {label}: {result.reason}")
      if entry.get("tries", 0) >= args.max_tries and not entry.get("alerted"):
        _alert_exhausted(label, result.reason)
        entry["alerted"] = True
    else:
      state.pop(str(want.book_id), None)
      out.imported.append(label)
      print(f"  imported {label} via {result.source}")
      _announce(want, result.source)
  if not args.dry_run:
    save_state(STATE_PATH, state)
  print(f"imported {len(out.imported)}, failed {len(out.failed)}")
  return 1 if out.failed else 0


def _notify(*args: object, **kwargs: object) -> None:
  try:
    sys.path.insert(0, str(REPO))
    from scripts.notify import notify  # noqa: PLC0415 - only needed on these paths

    notify(*args, **kwargs)
  except Exception as exc:  # noqa: BLE001 - a notifier must never fail the run
    print(f"  (could not notify: {exc})", file=sys.stderr)


def _announce(want: Wanted, source: str) -> None:
  """nas-media, shaped like arr_notify.sh's book message. Best-effort."""
  _notify("media", f"📚 {want.author} — {want.title}", f"via {source}", tags=("books",),
          click=os.environ.get("JELLYFIN_PUBLISHED_URL") or None)


def _alert_exhausted(label: str, reason: str) -> None:
  """One nas-attention message per book that ran out of tries. Best-effort."""
  _notify("attention", f"📚 Not on Z-Library: {label}",
          f"Gave up after the retry budget. Last reason: {reason}. "
          "Grab it by hand in Bookshelf, or unmonitor it.",
          tags=("books",), dedup_key=f"zlib-bridge-{label}")


if __name__ == "__main__":
  sys.exit(main())
