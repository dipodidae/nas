# Various Artists compilation support + resilient query degradation

Date: 2026-06-18
Status: Design — approved scope

## Problem

Feeding a "Various Artists" compilation (`Various Artists - <title>`, `various - …`, or a
Spotify compilation playlist) into lidarr-bulk currently fails. The user wants these to
"logically work" — added to Lidarr **as the compilation itself** — because compilations
often hold the best gems. Primary input path is **Spotify playlists**.

Separately: when a front-end-driven Lidarr query errors out — frequently a Lidarr
image-fetch error, or a title with a bad appendix like `(Extended Edition)` — the item
hard-errors instead of degrading gracefully. The user wants these to either drop the
image requirement or retry a name variation.

## Verified findings (live Lidarr + api.lidarr.audio, 2026-06-18)

A reversible end-to-end test against the live instance established every unknown:

1. **Lidarr accepts the special "Various Artists" entity.** It is already present (artist
   id 2246, path `/music/Various Artists`) holding 14 comps, each *individually* monitored.
   The special-purpose VA MusicBrainz MBID is `89ad4ac3-39f7-470e-963a-56509c546377`.
   Adding one comp links it under this single artist and does **not** drag in the whole VA
   universe (album count stayed bounded).
2. **Lidarr text search never surfaces VA comps.** `album/lookup?term=<comp title>` returned
   **0** VA-credited results across six well-known soundtracks/comps (Trainspotting, Pulp
   Fiction, Guardians of the Galaxy, NOW…, Saturday Night Fever, Dazed and Confused). It
   instead returns same-titled albums by *real* artists — the source of the current bug.
3. **The only working add path is by album MBID.** `album/lookup?term=lidarr:<albumMBID>`
   returns a correct candidate whose `artist.foreignArtistId` is the special VA MBID, and
   `POST /api/v1/album` with that candidate (artist block `monitor:none`,
   `monitorNewItems:none`) **succeeds (HTTP 201)**, links under VA, and the album comes back
   `monitored:true`. **The existing `addAlbum` code needs no change** — only the resolve step
   that produces the candidate.
4. **MusicBrainz is 403 from this host; `api.lidarr.audio` is reachable (HTTP 200)** and its
   `search?type=all&query=<title>` returns album entries carrying `album.artistid`. Filtering
   to the VA MBID yields the comp's album MBID. This is the title→MBID resolver.

## Approach

### Part 1 — Various Artists compilation support

**Detection.** A `ParsedItem` gains an optional flag `variousArtists?: true`.
- In `spotify.albumItemsFromTracks`: flag when the album's primary artist normalizes to a
  various-artists token (`various artists`, `various`, `va`, `v.a.`) **or** Spotify reports
  `album.album_type === 'compilation'`. To support this, widen the playlist track fetch
  `fields` to include `album(album_type,release_date,…)`; carry `release_date`'s year on the
  parsed item (new optional `year?: number`) for disambiguation.
- In `parsers.parseAlbums` (free-text): flag when the parsed `artist` normalizes to a
  various-artists token. (No year available here — resolver falls back to title-only.)

**New unit — `server/utils/metadata.ts`** (pure-ish I/O client, unit-testable transform
split from the fetch, mirroring `spotify.ts`):
- `VARIOUS_ARTISTS_MBID = '89ad4ac3-39f7-470e-963a-56509c546377'`
- `resolveVariousArtistsAlbumMbids(title: string, year?: number, limit = 5): Promise<string[]>`
  - `GET ${LIDARR_METADATA_URL}/search?type=all&query=<title>`
  - keep entries where `album?.artistid === VARIOUS_ARTISTS_MBID`
  - rank by `similarity(normKey(album.title), normKey(title))` (reuse `matching.ts`), with
    release-year proximity as tiebreak; return up to `limit` album MBIDs
  - pure ranking helper (`rankVaAlbums(entries, title, year)`) separated from the fetch
- New optional env `LIDARR_METADATA_URL`, default `https://api.lidarr.audio/api/v0.4`.

**Wiring — `jobs.searchCandidates`.** When `kind === 'album'` and `parsed.variousArtists`:
1. `resolveVariousArtistsAlbumMbids(parsed.title ?? parsed.raw, parsed.year)`
2. for each MBID, `lookupAlbum('lidarr:' + mbid)` → flatten to `Candidate[]`
3. return those candidates (no text-term lookup). Empty result → `not-found` as usual.

Everything downstream is unchanged: `pickAutoMatch` auto-selects the best title match (all
candidates share artist "Various Artists", so title disambiguates); ambiguity falls to
`needs-choice` showing the comp options; `addAlbum` adds it exactly as the verified test did.

### Part 2 — Resilient query degradation

**Name variations (lookups).** Replace the single inline parens-strip fallback in
`searchCandidates` with an ordered variation generator `albumQueryVariations(parsed)`:
1. original `artist title` (or `title` for VA)
2. parens/brackets stripped: `s.replace(/[([][^)\]]*[)\]]/g, ' ')`
3. trailing edition appendix stripped — regex over a maintained suffix list
   (`Deluxe( Edition)?`, `Extended( Edition| Version)?`, `Remaster(ed)?`, `Anniversary…`,
   `Bonus Track Version`, `Special Edition`, `Expanded Edition`, …)
Try variations in order; accept the first that yields a title-similar candidate
(`similarity(normKeyLoose) > 0.8`). Variations also apply to the VA resolver's title.

**Image-fetch errors (adds).** In `jobs.processAdd` / `addToLidarr`, classify add errors
whose message matches `/image|mediacover|cover art|failed to (download|fetch)/i`:
- retry the add once after a short backoff (these are typically transient metadata-server
  hiccups), then
- if it still fails, query Lidarr for the record by foreign id; if it was in fact created,
  treat as added (force-monitor via the existing nudge/monitor helpers) instead of erroring.
Add `console.error` logging of the raw Lidarr error body on add failure so the exact
image-error string can be confirmed and the matcher tightened during implementation.

## Data flow

```
Spotify playlist ─► fetchPlaylistTracks (fields incl album_type, release_date)
                 ─► albumItemsFromTracks ─► ParsedItem{ artist:"Various Artists",
                                                         variousArtists:true, year }
job.run ─► searchCandidates
            ├─ VA item ─► metadata.resolveVariousArtistsAlbumMbids(title, year)
            │            ─► lidarr.lookupAlbum("lidarr:<mbid>") ×N ─► Candidate[]
            └─ normal  ─► lidarr.lookupAlbum(term) with query variations
         ─► pickAutoMatch / rankCandidates  (unchanged)
         ─► addAlbum (unchanged) ─► linked under VA artist, monitored
```

## Error handling

- VA resolver network/parse failure → caught, item becomes `not-found` (never crashes the job).
- `LIDARR_METADATA_URL` unreachable → logged, VA items resolve to zero candidates → `not-found`.
- Add image errors → retry-then-verify-then-monitor as above; non-image errors keep current
  behavior (including the existing "already been added" → nudge path).
- All new I/O wrapped by the existing `retryOnTransient` where it shares the transient
  signature.

## Testing

- `tests/metadata.test.ts`: `rankVaAlbums` ordering (title sim + year tiebreak), VA filter,
  limit; fetch mocked.
- `tests/spotify.test.ts`: `albumItemsFromTracks` flags VA by artist name and by
  `album_type==='compilation'`; carries year; non-VA unaffected.
- `tests/parsers.test.ts`: free-text `Various Artists - X` / `various - X` flagged.
- `tests/matching` / variations: `albumQueryVariations` produces ordered, deduped variants;
  appendix stripping covers the suffix list.
- `tests/jobs` (or focused unit): VA branch in `searchCandidates` calls the resolver and not
  the text lookup; image-error add path retries then verifies.
- Manual: re-run the verified live flow (add "Pulp Fiction: Music From the Motion Picture"
  via the app end-to-end, then remove) to confirm parity with the curl test.

## Out of scope (YAGNI)

- Exploding comps into individual per-track real artists/albums (user chose "add as-is").
- A general MusicBrainz client (blocked from host; `api.lidarr.audio` suffices).
- Changing `addAlbum`/`addArtist` add semantics — verified to already work for VA.
```
