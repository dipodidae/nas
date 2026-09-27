#!/usr/bin/env python3
"""Write metadata.opf + cover.jpg beside every book Bookshelf holds, from Bookshelf's metadata.

Why sidecars
------------
Jellyfin titles a book from whatever the file carries: an epub's own OPF, or
for a PDF the file name. Those came in as "The Art of the Novel ( PDFDrive )",
"Blindness a novel", a Saramago PDF titled "José Saramago". Bookshelf already
knows the truth -- title, author, original year, series and position,
description, ISBN, publisher, genres, cover -- so this writes it next to each
book, where Jellyfin's Bookshelf plugin reads it (`metadata.opf`, the Calibre
convention; Audiobookshelf reads the same file).

Never INTO the book files: library files are hardlinks to what qBittorrent is
still seeding (ADR-0002), so rewriting a file's tags corrupts the torrent.
Sidecars are separate files and touch nothing.

What it does, per Bookshelf instance (ebooks and audiobooks):

* every book with files gets `metadata.opf` and `cover.jpg` in its folder
  (`Author/Title/`, ADR-0059), written only when the content differs, so
  Jellyfin's change monitor does not churn;
* a book folder that holds ONLY these two sidecars -- because Bookshelf moved or
  upgraded the book away, and its "delete empty folders" cannot see past our
  files -- is removed. Only those two exact names, only at Author/Title depth.

Exit codes
----------
  0  everything written (or already current)
  1  partial: some books could not be written
  2  fatal: Bookshelf unreachable, or a required env var unset

Usage
-----
  python scripts/books_sidecars.py            # write (cron)
  python scripts/books_sidecars.py --dry-run  # report what would change
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parents[1]

if "API_KEY_BOOKSHELF" not in os.environ:
  try:
    from dotenv import load_dotenv  # type: ignore

    load_dotenv(REPO / ".env")
  except ImportError:
    pass

SIDECARS = ("metadata.opf", "cover.jpg")
MAX_GENRES = 6


@dataclass(frozen=True)
class Instance:
  label: str
  base: str
  key_env: str
  root: str   # container path Bookshelf reports; the host path is this with
              # /data swapped for SHARE_DIRECTORY


INSTANCES = (
  Instance("ebooks", "http://localhost:8787", "API_KEY_BOOKSHELF", "/data/books/ebooks"),
  Instance("audiobooks", "http://localhost:8788", "API_KEY_BOOKSHELF_AUDIO", "/data/books/audiobooks"),
)


@dataclass(frozen=True)
class BookMeta:
  title: str
  author: str
  author_sort: str
  date: str = ""         # full ISO date: the plugin DateTime.TryParse()s it, and a bare year fails
  description: str = ""
  publisher: str = ""
  language: str = ""
  isbn: str = ""
  series: str = ""
  series_index: str = ""
  genres: tuple[str, ...] = ()


@dataclass
class Report:
  written: list[str] = field(default_factory=list)
  reaped: list[str] = field(default_factory=list)
  failed: list[str] = field(default_factory=list)


# --- pure --------------------------------------------------------------------


def parse_series(series_title: str) -> tuple[str, str]:
  """'Culture #1' -> ('Culture', '1'); 'Earthsea Cycle #1-3' -> ('Earthsea Cycle', '1')."""
  m = re.match(r"^(.*?)\s*#\s*([0-9]+(?:\.[0-9]+)?)", series_title or "")
  if not m:
    return (series_title or "").strip(), ""
  return m.group(1).strip(), m.group(2)


def strip_html(text: str) -> str:
  text = re.sub(r"<br\s*/?>|</p>", "\n", text or "", flags=re.I)
  text = re.sub(r"<[^>]+>", "", text)
  return re.sub(r"\n{3,}", "\n\n", text).strip()


def title_sort(title: str) -> str:
  """'The Crying of Lot 49' -> 'Crying of Lot 49, The' (English and Dutch articles)."""
  m = re.match(r"^(The|A|An|De|Het|Een)\s+(.+)$", title or "")
  return f"{m.group(2)}, {m.group(1)}" if m else (title or "")


def series_map(series_list: list[dict]) -> dict[int, tuple[str, str]]:
  """bookId -> (series, position) from Bookshelf's /series, which is more complete
  than a book's own seriesTitle (Excession has none; /series places it Culture #5)."""
  out: dict[int, tuple[str, str]] = {}
  for series in series_list or []:
    for link in series.get("links") or []:
      pos = str(link.get("position") or "").strip()
      out.setdefault(link.get("bookId"), (series.get("title", ""), pos if re.fullmatch(r"\d+", pos) else ""))
  return out


def build_meta(book: dict, edition: dict | None, author: dict,
               series_info: tuple[str, str] | None = None) -> BookMeta:
  edition = edition or {}
  series, index = series_info or parse_series(book.get("seriesTitle", ""))
  return BookMeta(
    title=book.get("title") or edition.get("title") or "",
    author=author.get("authorName", ""),
    author_sort=author.get("authorNameLastFirst") or author.get("authorName", ""),
    date=(book.get("releaseDate") or "")[:10],
    description=strip_html(edition.get("overview") or book.get("overview") or ""),
    publisher=edition.get("publisher") or "",
    language=edition.get("language") or "",
    isbn=edition.get("isbn13") or "",
    series=series,
    series_index=index,
    genres=tuple(g for g in (book.get("genres") or [])
                 if g.lower() not in ("audiobook", "audiobooks", "fiction"))[:MAX_GENRES],
  )


def build_opf(m: BookMeta) -> str:
  """A Calibre-style OPF 2.0 package: what Jellyfin's Bookshelf plugin and
  Audiobookshelf both read."""
  e = escape
  lines = [
    '<?xml version="1.0" encoding="utf-8"?>',
    '<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uuid_id" version="2.0">',
    '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:opf="http://www.idpf.org/2007/opf">',
    f"    <dc:title>{e(m.title)}</dc:title>",
    f'    <meta name="calibre:title_sort" content="{e(title_sort(m.title))}"/>',
    f'    <dc:creator opf:role="aut" opf:file-as="{e(m.author_sort)}">{e(m.author)}</dc:creator>',
  ]
  if m.date:
    lines.append(f"    <dc:date>{e(m.date)}</dc:date>")
  if m.description:
    lines.append(f"    <dc:description>{e(m.description)}</dc:description>")
  if m.publisher:
    lines.append(f"    <dc:publisher>{e(m.publisher)}</dc:publisher>")
  if m.language:
    lines.append(f"    <dc:language>{e(m.language)}</dc:language>")
  if m.isbn:
    lines.append(f'    <dc:identifier opf:scheme="ISBN">{e(m.isbn)}</dc:identifier>')
  lines += [f"    <dc:subject>{e(g)}</dc:subject>" for g in m.genres]
  if m.series:
    lines.append(f'    <meta name="calibre:series" content="{e(m.series)}"/>')
    if m.series_index:
      lines.append(f'    <meta name="calibre:series_index" content="{e(m.series_index)}"/>')
  lines += [
    '    <meta name="cover" content="cover"/>',
    "  </metadata>",
    "  <manifest>",
    '    <item id="cover" href="cover.jpg" media-type="image/jpeg"/>',
    "  </manifest>",
    "</package>",
    "",
  ]
  return "\n".join(lines)


def sidecar_only(names: set[str]) -> bool:
  """True for a folder holding our sidecars and nothing else."""
  return bool(names) and names <= set(SIDECARS)


# --- I/O -----------------------------------------------------------------------


def _get(inst: Instance, path: str, key: str, raw: bool = False):
  req = urllib.request.Request(inst.base + path, headers={"X-Api-Key": key})
  with urllib.request.urlopen(req, timeout=120) as resp:
    body = resp.read()
    if raw:
      return body, resp.headers.get("Content-Type", "")
  return json.loads(body or b"null")


def write_if_changed(path: Path, data: bytes, dry_run: bool) -> bool:
  try:
    if path.read_bytes() == data:
      return False
  except OSError:
    pass
  if not dry_run:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
  return True


def converge(inst: Instance, key: str, share: Path, dry_run: bool, rep: Report) -> None:
  host_root = share / Path(inst.root).relative_to("/data")
  authors = {a["id"]: a for a in _get(inst, "/api/v1/author", key)}
  books = {b["id"]: b for b in _get(inst, "/api/v1/book", key)}
  live_folders: set[Path] = set()
  for author_id, author in authors.items():
    in_series = series_map(_get(inst, f"/api/v1/series?authorId={author_id}", key))
    folders: dict[int, Path] = {}
    for f in _get(inst, f"/api/v1/bookfile?authorId={author_id}", key):
      host = share / Path(f["path"]).relative_to("/data")
      folders.setdefault(f["bookId"], host.parent)
    for book_id, folder in folders.items():
      live_folders.add(folder)
      book = books.get(book_id)
      if book is None:
        continue
      label = f"{inst.label}: {author['authorName']} — {book['title']}"
      try:
        editions = _get(inst, f"/api/v1/edition?bookId={book_id}", key) or []
        edition = next((e for e in editions if e.get("monitored")), editions[0] if editions else None)
        opf = build_opf(build_meta(book, edition, author, in_series.get(book_id))).encode()
        changed = write_if_changed(folder / "metadata.opf", opf, dry_run)
        cover, ctype = _get(inst, f"/api/v1/mediacover/book/{book_id}/cover.jpg", key, raw=True)
        if ctype.startswith("image/"):
          changed |= write_if_changed(folder / "cover.jpg", cover, dry_run)
        if changed:
          rep.written.append(label)
      except (OSError, urllib.error.URLError, ValueError) as exc:
        rep.failed.append(f"{label}: {exc}")

  # Reap folders left holding nothing but our sidecars. Author/Title depth only.
  if host_root.is_dir():
    for folder in host_root.glob("*/*"):
      if not folder.is_dir() or folder in live_folders:
        continue
      names = {p.name for p in folder.iterdir()}
      if sidecar_only(names):
        if not dry_run:
          for name in names:
            (folder / name).unlink()
          folder.rmdir()
          with contextlib.suppress(OSError):
            folder.parent.rmdir()  # the author folder, if that was its last book
        rep.reaped.append(str(folder.relative_to(host_root)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  ap.add_argument("--dry-run", action="store_true")
  return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  share = os.environ.get("SHARE_DIRECTORY")
  keys = {i.key_env: os.environ.get(i.key_env) for i in INSTANCES}
  if not share or not all(keys.values()):
    print("FATAL: SHARE_DIRECTORY and both API_KEY_BOOKSHELF* must be set", file=sys.stderr)
    return 2
  rep = Report()
  for inst in INSTANCES:
    try:
      converge(inst, keys[inst.key_env], Path(share), args.dry_run, rep)
    except (OSError, urllib.error.URLError, ValueError) as exc:
      print(f"FATAL: {inst.label} Bookshelf unreachable: {exc}", file=sys.stderr)
      return 2
  verb = "would write" if args.dry_run else "wrote"
  print(f"{verb} sidecars for {len(rep.written)} book(s), reaped {len(rep.reaped)} "
        f"folder(s), {len(rep.failed)} failure(s)")
  for line in rep.reaped:
    print(f"  reaped {line}")
  for line in rep.failed:
    print(f"  FAILED {line}", file=sys.stderr)
  return 1 if rep.failed else 0


if __name__ == "__main__":
  sys.exit(main())
