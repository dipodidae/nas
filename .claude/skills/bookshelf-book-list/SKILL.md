---
name: bookshelf-book-list
description: Use when someone hands you a list of books/authors (a reading list, a curated recommendation dump, "add these to my library") to add to Bookshelf, this stack's Readarr revival for ebooks and audiobooks. Covers the non-obvious Readarr v1 API shape needed to add a book that isn't already in the library, and a real failure mode (silent un-monitoring) that makes an "added" book never get searched.
---

# Bulk-adding a book list to Bookshelf

Use `scripts/bookshelf_add_list.py`, not ad hoc curl. It already encodes
everything below.

```bash
# one book per line: "Title" or "Title | Author" (author strongly recommended
# -- see "wrong top match" below). Blank lines and `#` comments are skipped.
python scripts/bookshelf_add_list.py --instance ebook --file mylist.txt
python scripts/bookshelf_add_list.py --instance audio --file mylist.txt

# preview only, adds nothing
python scripts/bookshelf_add_list.py --instance ebook --file mylist.txt --dry-run
```

A JSON array of `{"title": ..., "author": ...}` objects also works as the
`--file`.

It is idempotent: a title already present (exact match, case-insensitive) is
skipped, not duplicated, so re-running after a partial failure is the normal
recovery path, not a special case.

## Why this needs a script and not three curl calls

Readarr's v1 "add a book that has no author yet" endpoint (`POST /book`) is
underdocumented and will 500 with an unhelpful stack trace until the request
body has exactly the right shape:

1. `book/lookup?term=` returns a book with `authorId: 0` and no `editions`
   array. Posting that straight to `/book` 500s
   (`NullReferenceException` on the quality profile, because the author
   doesn't exist yet).
2. The book needs a full **embedded `author` object**, built from
   `author/lookup?term=`, with `qualityProfileId`, `metadataProfileId`,
   `rootFolderPath`, `monitored`, `monitorNewItems` and `addOptions` set on
   it -- there is no separate "add author" call in this flow.
3. It also needs an **`editions` array**, which the book lookup does not
   provide. Build one minimal edition from the lookup result's own fields
   (`foreignEditionId`, `titleSlug`, etc.) -- omitting it 500s with
   `ArgumentNullException` deep in `EditionResourceMapper`.

Both were found by trial and error against a live instance on 2026-09-28; the
working shapes are what's in the script's `add_entry`/`build_edition`.

## The silent failure this catches: added != monitored

Adding a **new** author with `monitored: False` (so you get one book, not
their entire bibliography pulled in unmonitored-but-present) has a side
effect: the book you just added and explicitly set `"monitored": true` on
gets **reset to unmonitored** a few seconds later, asynchronously. `POST
/book` answers `201` with `"monitored": true` in the response body -- the
response lies about the state the record ends up in a moment later. A book
search or download client never touches an unmonitored book, so this is a
book that looks added and never arrives.

This is a genuine **race, not a one-time state**, and it caught this
script's first fix (2026-09-28): calling `PUT /book/monitor` immediately
after the `POST` looked like it worked (`202`, `monitored: true` in that
response too) but measurement showed it flips back to `false` about 1-3
seconds later regardless -- something in Bookshelf re-syncs the book's
monitored flag from the author's `monitorNewItems: "none"` shortly after
creation, overwriting whatever you just asserted. Seven books added in one
run this way (`The Golden Bough`, `The Witch Cult in Western Europe`, `The
Devils of Loudun`, `Melmoth the Wanderer`, `The Melancholy of Resistance`,
`The Fisherman`, `The Masks of God, Volume 1`) were all silently
unmonitored despite the script's own "fix" call succeeding.

The actual fix: wait out the settle window *before* asserting, then verify
the assertion stuck and retry if it didn't:

```
sleep ~5s                                    # let the async settle happen first
PUT /book/monitor {"bookIds":[id],"monitored":true}
GET /book/<id>                               # confirm monitored:true actually persisted
# if still false, repeat the PUT+GET a few times
```

`bookshelf_add_list.py` does this (`MONITOR_SETTLE_S` / `MONITOR_ASSERT_RETRIES`)
and prints a `WARNING: still unmonitored after retries` on the rare title
where it doesn't stick, so you don't have to comb the library for silent
casualties. If you ever add a book by hand, do the same and verify with a
`GET /book/<id>` a few seconds later, not immediately after your `PUT`.

## The failure mode you have to catch yourself: wrong top match

`book/lookup` returns Goodreads search results and the script always takes
the first one. There is no author filter on this endpoint -- passing an
author only changes which *author* record gets attached after the fact, it
does **not** narrow the book search itself. So the script author-qualifies
the search string too (`"{title} {author}"`, not just `{title}`) whenever
you give it an author, which is the actual fix, not a nicety. Without it,
measured 2026-09-28:

- `"The White People"` (Machen) matched **"Stuff White People Like"**,
- `"The Devils of Loudun"` (no author hint) matched an unrelated 19th-century
  historical volume, not Huxley's book,
- one-word or very short titles are worst-affected in general.

With the author folded into the query (`"The White People Arthur Machen"`)
the correct edition became the top result. Still, **read the "added: ..."
line the script prints for each book**; it names the exact title matched and
prints a ready-to-use removal command:

```
added: The Devils of Loudun (id 845) -- verify this matches 'The Devils of Loudun'
by Aldous Huxley; if not, remove with: curl -X DELETE http://localhost:8787/api/v1/book/845
```

If the title still doesn't match what you meant, delete it (the printed curl
command, plus your own `-H "X-Api-Key: $API_KEY_BOOKSHELF"`) and re-add with
a more specific search term (add a subtitle word, or the exact author name
as it appears on the book's Goodreads page).

**The worst version of this isn't a wrong edition, it's a wrong author
entirely**, discovered 2026-09-28 mid-session: an author-lookup for an
obscure name, made while the metadata service was degraded, silently
resolved to a completely unrelated real author (an occult grimoire search
for "Asenath Mason" landed on a self-help author, "Pete Walker" -- zero
words in common) -- and because that existing author was already
`monitored: true` with `monitorNewItems: "all"`, adding one book under it
pulled in **five of his unrelated books, all pre-monitored**, ready to be
searched and downloaded. This didn't show as an error anywhere; it looked
exactly like every other successful add. The only way it was caught was a
post-run audit grouping every newly-monitored book by author and
eye-balling any author with a suspicious count:

```bash
curl -s ".../book" -H "X-Api-Key: $KEY" > books.json
curl -s ".../author" -H "X-Api-Key: $KEY" > authors.json
# group books.json by authorId, cross-reference against authors.json,
# and check every author against what you actually asked for
```

Do this after any run that touches obscure/small-press authors. Delete
anything that doesn't belong (`DELETE /book/<id>`) -- it's cheap to check
and expensive to leave a wrong author's back-catalogue silently downloading.

**A related, narrower trap: the same Goodreads *work* can have a foreign
edition as its canonically-cached title.** McCarthy's *The Road* repeatedly
came back titled `"Yol"` (its Turkish edition) with zeroed page count and
rating, even when queried by the correct English `foreignEditionId` --
`RefreshBook`/`RefreshAuthor` did not fix it, and it persisted across
delete-and-re-add. A monitored book titled `"Yol"` is actively harmful. not
just cosmetically wrong: Bookshelf's release matching searches for that
title, so it will hunt for a Turkish-titled torrent instead of an English
one. **Never leave a book monitored under a foreign-edition display title
you didn't ask for** -- delete it and either retry later (the shared
service's per-work cache does eventually clear) or add it manually once you
find an edition id whose lookup returns the right title *and* it survives a
few minutes without reverting.

Also watch for outright **duplicate/orphaned edition rows** from a client
timeout that actually succeeded server-side: a retried `POST /book` for the
same `foreignEditionId` can then 409 with `UNIQUE constraint failed:
Editions.ForeignEditionId` even though nothing shows up in `GET /book`. If
that happens, check `GET /book?authorId=<id>` for a stray entry before
concluding the edition is unusable; if none exists, pick a *different*
edition id from the lookup results rather than fighting the same one
repeatedly.

## The shared metadata service is flaky -- expect it, don't fight it

Both instances get book/author data from a **shared** Goodreads-scraping
proxy (`api.bookinfo.pro`, see ADR-0057). It goes through bursts of `503`s
and full timeouts, verified 2026-09-28 over roughly 15 minutes of a real
30-book run before recovering on its own. This is not something to route
around -- LibGen/Anna's Archive-flavoured alternatives to Bookshelf were
already tried and rejected for this exact reason (see ADR-0057's "What was
tried" section).

`bookshelf_add_list.py` retries each lookup a few times with backoff and
keeps going past a failed title rather than aborting the whole list. If a
large fraction of a run fails with 503/timeout, that is the metadata service
having a bad day: **just run the script again later** (it will skip
everything already added) rather than debugging your list or the script.
Confirm from the container logs if you want proof it's upstream and not the
script:

```bash
docker logs bookshelf --since 5m 2>&1 | grep -A2 BookInfoProxy
```

## Instance choice

`--instance ebook` -> `bookshelf` (port 8787, root `/data/books/ebooks`,
quality profile `eBook`). `--instance audio` -> `bookshelf-audio` (port 8788,
root `/data/books/audiobooks`, quality profile `Spoken`). Both need their
respective `API_KEY_BOOKSHELF` / `API_KEY_BOOKSHELF_AUDIO` in the environment
(source `.env` first). One title can exist correctly in both if it's wanted
as both an ebook and an audiobook -- run the script twice, once per
`--instance`, with the same list file.
