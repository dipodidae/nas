# ADR-0057 — Books: Bookshelf for the *arr side, a bridge for Z-Library, Jellyfin to read

**Date:** 2026-09-27
**Status:** accepted

## Decision

- **Bookshelf** (`pennydreadful/bookshelf`, the maintained Readarr revival), pinned at
  `softcover-v0.4.21.182`, in **two instances**: `bookshelf` (ebooks,
  `/data/books/ebooks`) and `bookshelf-audio` (audiobooks, `/data/books/audiobooks`).
  Upstream holds one media type per instance. Readarr itself is retired and its
  metadata service is dead.
- **Prowlarr** syncs its book-capable indexers into both (Readarr app type), with
  non-overlapping categories: 7000/7020 for ebooks, 3030 for audiobooks. Grabs go
  to qBittorrent under `arr-bookshelf` / `arr-bookshelf-audio`.
- **Z-Library** reaches the ebook instance through `scripts/bookshelf_zlib_bridge.py`
  (cron `:41`), not through Prowlarr. It picks up missing monitored books, matches
  them on Z-Library, downloads the exact md5 (LibGen first, the Z-Library account
  second), and hands that one file to Bookshelf as a ManualImport.
- **Jellyfin** reads them: the Bookshelf plugin (13.0) plus two `books` libraries,
  **Books** (`/data/movies/books/ebooks`) and **Audiobooks**
  (`/data/movies/books/audiobooks`), both with the realtime monitor on. They use the
  existing load-bearing mount (ADR-0016), so compose needs no Jellyfin change.

Configuration that lives in app databases is declared in `scripts/bookshelf_setup.py`
(`make bookshelf-setup`) and asserted by `make verify-runtime`, together with
`scripts/check-books-stack.py`.

## What was tried, and why it is not here

**Librarr** (`JeremiahM37/librarr`), the community "Z-Library for the *arrs" bridge,
was spiked first and removed the same day:

1. Its Torznab feed, the part meant to make it an indexer, advertises every direct
   download as `/api/download/nzb/<md5>`. That route is never registered, so an *arr
   grab from it 404s by construction (`internal/torznab/xml.go`).
2. Used only as a downloader, it retries a failed LibGen job twice at about 90 s each,
   and reports the final state as `dead_letter`.
3. Its EPUB check compares the embedded `dc:title` with the requested title and
   **deletes the file** on a mismatch. A genuine *Project Hail Mary* epub whose md5
   matched the ISBN-matched catalogue record carries the title "A Novel", so a correct
   download was thrown away. The check cannot be disabled.
4. Its source registry is fetched at runtime from a third-party repo unless it is
   pinned by hand.

The bridge does the same job in one tested script, with the md5 as proof of identity,
and with no container holding the Z-Library credentials.

**The `hardcover-*` metadata flavour** was the first choice for quality. On day one
its shared server (`hardcover.bookinfo.pro`) timed out on every endpoint, including
`/author/1`. Self-hosting it needs a personal Hardcover API token that **expires every
1 January**, which is a scheduled silent failure.

**A self-hosted rreading-glasses (Goodreads mode)** was tried next, to avoid depending
on a shared server. Cold, it returned 4 of Andy Weir's 22 works, with zero ratings and
no language. The metadata profile's popularity floor then filtered all of them, so
"every book by this author" could not work. Nothing in Bookshelf's refresh path warms
the rest. The shared Goodreads instance (`api.bookinfo.pro`, the `softcover` image's
default) returned all 22 with real metadata. Completeness won. The shared instance
is watched by `check-books-stack.py`, which does a real author lookup rather than a
ping. If it dies, the way back is self-hosting with a warm-up plan, not a tag change,
because the two flavours' databases are not interchangeable.

## Invariants

- **Both Bookshelf instances mount the whole share at `/data`** (asserted,
  `DATA_REQUIRED`). qBittorrent's `/downloads/` reaches them through a remote path
  mapping to `/data/downloads/`, and the bridge stages under
  `/data/downloads/zlib-bridge/<md5>/`. Every import is a rename or a hardlink.
- **Pinned, both instances on the same tag.** diun's policy offers `softcover-*`
  only, because a `hardcover-*` tag is a different database.
- **Z-Library quota is spent only on files LibGen lacks.** LibGen serves the same md5
  with no cap. When LibGen has the md5 but every mirror fails (they 503 and time out
  in bursts), the book is **deferred** an hour, not sent to Z-Library. A spent quota
  also defers. Deferrals never count toward the give-up budget. Tested
  (`test_a_flaky_libgen_mirror_defers_instead_of_spending_zlibrary_quota`).
- **The md5 is the identity.** The download is hashed while it streams, and a
  mismatch never reaches Bookshelf. The matcher never accepts a candidate without an
  ISBN overlap unless the title is at least 0.85 similar *and* the author's surname
  matches. That rules out the summaries, study guides and other-language editions
  that crowd every search.
- **`z-lib.gd` is the API host.** It answers the JSON API directly. `z-library.sk`,
  `1lib.sk` and `z-lib.fm` sit behind a DiamWall browser challenge, and `z-lib.org`
  is the domain seized in 2022. All measured 2026-09-27.
- **The bridge publishes nas-media itself.** Bookshelf's CustomScript connector
  (`arr_notify.sh`, which now has a Readarr branch) fires only for downloads Bookshelf
  tracked, never for a ManualImport. Torrent imports announce through the connector;
  bridge imports announce through `notify.py`.
- **Forms login behind the door.** `TINYAUTH_USER` + `BOOKSHELF_PASSWORD`, like every
  *arr. `/api` stays path-scoped open on the app's own key. Upstream validates that
  `instanceName` contains "Readarr", hence "Readarr (Bookshelf)".

## Things that cost time

- Anna's Archive search is behind a DDoS-Guard hCaptcha since the 2026 lawsuit.
  FlareSolverr cannot solve it. `annas-archive.is` serves SEO spam under `/books/`
  URLs, and `.li` is a parked domain. Z-Library's own API is the working search.
- LibGen `get.php` answers browser User-Agents with `503` and `curl` with a default
  nginx page during its bad spells. The same mirror served the same md5 an hour
  earlier. Treat it as weather, not as a block.
- A Readarr `wanted/missing` record carries **no editions**: the monitored edition's id
  is top-level, and ISBNs come from `/edition?bookId=`. The missing list also requires
  the *author* to be monitored, and adding an author with `monitor: none` leaves it
  unmonitored.
- Metadata language codes are ISO 639-3 only: `nld` works, `dut` is rejected.
- A brand-new Jellyfin library is not populated by an item refresh. It needs a
  library scan once; after that the realtime monitor picked up a new import within
  about a minute.
- The Readarr Ntfy connector supports no failure triggers at all, so Bookshelf has no
  attention connector. `stack_watchdog.py` owns *arr health.
