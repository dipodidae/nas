# 0038 — Artist images come from Deezer, verified against the albums on disk

**Status:** accepted · **Date:** 2026-09-13

## Context

Album covers were solved and artist images were not, and nobody had measured
which. On 2026-09-13:

- **16,248 of 16,882** album folders had a `folder.jpg` (96.3%), and Jellyfin
  was missing a primary image on 535 of 15,941 albums (3%).
- **756 of 2,741** artist directories had any image at all, and Jellyfin was
  missing a primary image on **1,647 of 3,331 artists — 49%**.

Three things were being blamed and none of them was the cause:

**Not sacad.** `scripts/album_art.py` is an _album_ cover tool; `sacad_r` walks
album directories and never touches an artist directory. It was doing its job.

**Not Lidarr.** Its Kodi/Emby metadata consumer has `artistImages: True`, but
Lidarr's metadata server holds image URLs for only **956 of its 3,519 artists**
(27%) — which is almost exactly the 756 on disk. There is nothing more to write.

**Not the remaining 634 album gaps either.** Those were resolved to MusicBrainz
release-group IDs through Lidarr and checked against Cover Art Archive: **0 of
25 sampled had a front image** (all 404). They are rehearsal tapes, demos,
splits and obscure black metal. 3.7% is the floor, not a tooling failure.

The real question was source coverage for artists, so it was measured on random
samples of the actual gap:

| source                                       | coverage                          |
| -------------------------------------------- | --------------------------------- |
| **Deezer** — exact-name artist picture       | **25/30 (83%)**                   |
| fanart.tv — what the Jellyfin plugin queries | 3/40 (7.5%), one an `artistthumb` |
| Wikidata P18 via a MusicBrainz URL relation  | 1/30 (3%)                         |

This library is deep-catalogue underground metal and experimental. The
metadata-curation sites have barely touched it. Deezer, being a shop, carries it
anyway.

## Decision

**`scripts/artist_art.py`** — the missing sibling of `album_art.py`. It walks
the artist directories, asks Deezer, and writes `folder.jpg` into the artist
folder, which Jellyfin reads as that artist's primary image on the next scan.
Writing to disk rather than into Jellyfin is deliberate: the share is mounted
read-only into Jellyfin (ADR-0016), the file survives a Jellyfin rebuild, and
every other consumer sees it.

**A candidate must share an album title with the folder on disk.** An artist
directory named `33` or `Beware` matches something on Deezer no matter what, and
a wrong artist image is worse than none — it looks correct and nothing
downstream ever flags it. The gate is on by default (`--no-verify` disables it)
and the run reports how many it rejected, split from "no exact name match" and
"matched, but Deezer has no photo" so the three are never conflated.

The shape is the house one: dry-run by default, `--limit` per run, a state file
with a 45-day cooldown so an artist no source has is not re-queried weekly
forever. Cron: Sunday 03:50, `--limit 400`.

**Jellyfin's own music image providers are all enabled.** The Fanart, Cover Art
Archive and last.fm plugins were installed and Active with only TheAudioDB
ticked on the music library. Now:

- MusicArtist: `Fanart`, `TheAudioDB`
- MusicAlbum: `Cover Art Archive`, `Fanart`, `TheAudioDB`, `last.fm`

This is worth doing and is **not** the fix — at 7.5% fanart.tv coverage it
recovers a tail, not the 49%. `scripts/jellyfin_image_backfill.py` (Sunday
05:45) walks the items still missing an image and refreshes those, because a
library scan will not: Jellyfin only queries image providers for an item it is
actually refreshing, and a scan skips items whose files have not changed. It
re-reads the batch after a settle period and reports how many _gained_ an image,
since a refresh answers `204` for work it has merely queued.

## The Sunday chain is ordered, and was not

03:50 artist-art → 04:45 album-art → **05:20** jellyfin-scan-music → 05:45
jellyfin-image-backfill → 06:05 music-library-sweep.

The music scan was at 05:05 while `album_art.py` measured 21.6 minutes from
04:45, so the last covers sacad wrote each week waited a week to be seen.

## Consequences

First real run: **310 artist images written in 535 s, 0 failures**, 45 rejected
for no exact name match, **41 rejected by the album cross-check**, 4 matched an
artist Deezer has no photo for. That 41 is the gate earning its place — those
are the wrong-band matches that would have been written silently.

At 400/week the ~1,970 gaps drain in about five weeks.

The Jellyfin-side backfill's first run refreshed 300 items and **96 gained an
image** (32%) — a real contribution, and a fifth of what the disk-side script
manages, which is the ratio the coverage numbers predicted.

## What was rejected

- **Wikidata/Wikimedia Commons via MusicBrainz relations** — 3% coverage here.
  Broad for mainstream artists, empty for this library.
- **Enabling `SaveLocalMetadata`** so Jellyfin writes its fetched art to disk —
  it cannot: the share is mounted `:ro` (ADR-0016), and turning it on would
  produce write errors, not files.
- **Replacing sacad** with beets `fetchart` or an MBID-targeted Cover Art
  Archive fetcher. The 634 remaining album gaps are not findable by any of them;
  0/25 sampled exist in CAA at all.

## See also

- ADR-0016 — Jellyfin's paths are load-bearing, and the share is read-only
- ADR-0003 — the Lidarr → Jellyfin bridge, and why `/data/music` matters
- `.claude/skills/nas-music-pipeline`
