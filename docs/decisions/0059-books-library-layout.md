# ADR-0059 — One folder per book, metadata beside it, one file per audiobook

**Date:** 2026-09-28
**Status:** accepted

## The problem

The books libraries were a pile. Every file sat loose in its author folder under
whatever name its release had: `The Art of the Novel ( PDFDrive ).pdf`,
`Survivor-Part06.mp3`, `Haunted_ A Novel by Chuck Palahniuk.epub`. The chapter MP3s
of *Choke* and *Survivor* shared one directory. Jellyfin showed **30 audiobook items
for 5 books**, one per chapter file ("001", "Chapter 1. The Sacrifice Poles"), and
titled ebooks from embedded junk. A Saramago PDF was titled "José Saramago", and
*High-Rise* claimed the year 101.

There were three causes:

1. **`renameBooks` was off** (the image default). With it off, Bookshelf keeps the
   release filename *and ignores the book-folder half of the naming format*, so
   every file lands loose in the author folder.
2. **Jellyfin cannot stack a multi-file audiobook.** Its `AudioResolver` skips any
   audiobook with more than one file, "until we sort out naming for multi-part
   books" (still on master). No naming scheme fixes that.
3. **Jellyfin titles a book from the file**, and release files carry junk metadata.

## Decision

- **Naming, both instances** (`bookshelf_setup.py`, asserted): `renameBooks` on,
  `{Author Name}/{Book Title}/{Author Name} - {Book Title}{ (PartNumber:00)}`.
  - One folder per book. Parts are zero-padded so chapter 10 sorts after 9.
  - **No year:** `{Release Year}` is whichever edition's date the metadata picked,
    so the same book got `Survivor (2018)` as an audiobook and `Survivor (1999)` as
    an ebook.
- **Recycle bin:** `/data/downloads/.bookshelf-recycle`, 14 days, delete-empty-folders
  on. Every delete through Bookshelf (de-duplication, upgrades, the merges below) is a
  recoverable move for two weeks.
- **`books_sidecars.py`** (cron `:46`) writes `metadata.opf` and `cover.jpg` beside
  every book from Bookshelf's metadata:
  - Contents: title, sort title, author with sort name, original date, description,
    publisher, language, ISBN, genres, and Calibre series plus index (taken from
    `/series`, which is more complete than a book's own `seriesTitle`).
  - Jellyfin's Bookshelf plugin reads it; the "Open Packaging Format" reader is
    pinned first in the Books library. Audiobookshelf reads the same file.
  - The script also reaps folders left holding only the sidecars.
- **`audiobook_merge.py`** (cron `:51`) turns every multi-file audiobook into **one
  chaptered M4B**:
  - One chapter per part, named from the part's own tag when meaningful, else
    "Part N". Cover and metadata are embedded.
  - It verifies duration (within 1%) and chapter count, then swaps the file in
    *through* Bookshelf: the parts go to the recycle bin, and the M4B is
    ManualImported.
  - Encoding runs in a throwaway container from the jellyfin image, using its
    `libfdk_aac`.
  - Dry run is the default; cron passes `--apply`.

Result: 64 ebooks, each with a real title, author, original year, description,
cover and correct sort. 5 audiobooks shown as 5 items, with chapters (7 to 16),
covers and authors.

## Invariants

- **Never write tags *into* a library file.** Library files are hardlinks to what
  qBittorrent is still seeding (ADR-0002), so rewriting one corrupts the torrent.
  Metadata goes in sidecars. The merge writes a *new* file, and the torrent's own
  hardlink under `downloads/` keeps seeding untouched. This is also why Bookshelf's
  `writeAudioTags` stays `no`.
- **`dc:date` is a full ISO date.** The plugin `DateTime.TryParse()`s it, and a bare
  `1987` failed silently, so every book lost its year.
- **`calibre:series_index` must be an integer.** The plugin reads it as `Int32`,
  so `1.5` is dropped rather than written.
- **AAC-LC, not HE-AAC.** HE-AAC would suit ~40 kbps speech better, but this
  jellyfin-ffmpeg's libfdk writes no packets for any `aac_he` or `aac_he_v2`
  setting (measured). LC is floored at 48k stereo / 32k mono and capped at 128k.
- **A new Jellyfin library needs one library scan.** An item refresh does not
  discover children. After that, the realtime monitor picks imports up.
- `check-books-stack.py` fails on any book file loose in an author folder, which
  is the symptom of `renameBooks` being switched off again.

## One-time clean-up done with this

- Renamed all 89 tracked files; none were untracked or missing.
- Three wrong imports, found by scoring each filename against its assigned book:
  - the *Fight Club* MOBI filed as the *Fight Club 2* comic (a duplicate of the
    EPUB): recycled, and the comic unmonitored;
  - *Blind Willow, Sleeping Woman* filed as *What I Talk About When I Talk About
    Running*: reassigned;
  - a critical study *about* Banks filed as a Culture box set: recycled, and the
    set unmonitored.
- *Surface Detail* was titled "Surface Detail 1st (first) edition Text Only" by its
  monitored edition; it was switched to a clean edition.
- Goodreads splits Banks into "Iain M. Banks" (SF) and "Iain Banks" (literary). Both
  are kept, and the `Iain M. Banks (1)` folder is now `Iain Banks`.
