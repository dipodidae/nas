# Various Artists Compilation Support + Resilient Query Degradation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make "Various Artists" compilations resolve and add to Lidarr as the compilation itself, and make Lidarr query failures (image-fetch errors, bad title appendices) degrade gracefully instead of hard-erroring.

**Architecture:** A compilation is detected at parse time (Spotify or free-text) and flagged on `ParsedItem`. Flagged items skip Lidarr's text search (which deliberately hides Various Artists) and instead resolve the album's MusicBrainz MBID via Lidarr's own metadata backend (`api.lidarr.audio`), then feed the unchanged `lookupAlbum("lidarr:<mbid>")` → `pickAutoMatch` → `addAlbum` pipeline. Non-VA lookups gain an ordered query-variation fallback (strip parens / edition suffixes), and the add path retries once on image-fetch errors then verifies the record was created.

**Tech Stack:** Nuxt 4 (Nitro server), TypeScript, Zod, Vitest. ESLint flat config (`pnpm lint`).

## Global Constraints

- Test runner: `pnpm test` (`vitest run`). Lint: `pnpm lint` (must pass; no warnings introduced).
- Never hard-code Lidarr/metadata base URLs in logic — read from `loadEnv()`.
- Pure logic (parsing, ranking, classification) lives in exported functions and is unit-tested; network I/O is a thin wrapper around the pure transform (mirror the existing `spotify.ts` / `openai.ts` split).
- Special Various Artists MusicBrainz MBID is `89ad4ac3-39f7-470e-963a-56509c546377` (verified live).
- Reuse `normKey` / `similarity` from `server/utils/matching.ts`; do not reimplement string normalization.
- Follow existing code style: 2-space indent, no semicolons, single quotes (matches the repo).

---

### Task 1: VA detection helper + `ParsedItem` fields

**Files:**
- Modify: `shared/types.ts:5-13` (add `variousArtists?` and `year?` to `ParsedItem`)
- Modify: `server/utils/matching.ts` (add exported `isVariousArtists`)
- Test: `tests/matching.test.ts`

**Interfaces:**
- Produces: `isVariousArtists(name: string | undefined): boolean`; `ParsedItem.variousArtists?: true`; `ParsedItem.year?: number`

- [ ] **Step 1: Write the failing test** — append to `tests/matching.test.ts`:

```ts
import { isVariousArtists } from '../server/utils/matching'

describe('isVariousArtists', () => {
  it('matches the various-artists tokens, case/space-insensitive', () => {
    for (const s of ['Various Artists', 'various', 'VA', 'v.a.', '  Various   Artists '])
      expect(isVariousArtists(s)).toBe(true)
  })
  it('does not match real artist names or empty', () => {
    for (const s of ['Variation', 'The Various', 'Avant', '', undefined])
      expect(isVariousArtists(s)).toBe(false)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- tests/matching.test.ts`
Expected: FAIL — `isVariousArtists is not a function`.

- [ ] **Step 3: Add the type fields** — in `shared/types.ts`, replace the `ParsedItem` body's trailing field block so it reads:

```ts
export interface ParsedItem {
  raw: string
  kind: Kind
  // Album rows expose split fields if we recognized a shape.
  artist?: string
  title?: string
  // Anything we couldn't confidently split lands here for the user to review.
  needsReview?: boolean
  // Set when the row is a Various Artists compilation — resolved by MBID, not text search.
  variousArtists?: true
  // Release year, when known (Spotify) — disambiguates same-titled comps.
  year?: number
}
```

- [ ] **Step 4: Implement the helper** — append to `server/utils/matching.ts`:

```ts
// "Various Artists" comes in many spellings; Lidarr hides the special VA entity
// from text search, so we detect it up front and resolve such rows by MBID.
const VA_TOKENS = new Set(['various artists', 'various artist', 'various', 'va', 'v a'])
export function isVariousArtists(name: string | undefined): boolean {
  if (!name)
    return false
  const k = normKey(name) // normKey strips punctuation, so "v.a." → "v a"
  return VA_TOKENS.has(k)
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pnpm test -- tests/matching.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add shared/types.ts server/utils/matching.ts tests/matching.test.ts
git commit -m "feat(matching): add isVariousArtists + ParsedItem VA/year fields"
```

---

### Task 2: Flag VA compilations + carry year from Spotify

**Files:**
- Modify: `server/utils/spotify.ts:46-80` (`ApiAlbum`, `albumItemsFromTracks`) and `:244-250` (`fetchPlaylistTracks` fields)
- Test: `tests/spotify.test.ts`

**Interfaces:**
- Consumes: `isVariousArtists` (Task 1)
- Produces: `albumItemsFromTracks` emits `variousArtists: true` and `year` on compilation rows

- [ ] **Step 1: Write the failing test** — append to the `albumItemsFromTracks` describe block in `tests/spotify.test.ts`:

```ts
it('flags Various Artists comps and carries the release year', () => {
  const res = albumItemsFromTracks([
    { track: { type: 'track', album: { id: 'a1', name: 'Pulp Fiction', album_type: 'compilation', release_date: '1994-09-26', artists: [{ name: 'Various Artists' }] } } },
    { track: { type: 'track', album: { id: 'a2', name: 'Mix', album_type: 'compilation', release_date: '2001', artists: [{ name: 'DJ Real' }] } } },
    { track: { type: 'track', album: { id: 'a3', name: 'Solo LP', album_type: 'album', release_date: '2010-01-01', artists: [{ name: 'A Band' }] } } },
  ] as never)
  expect(res.items[0]).toMatchObject({ kind: 'album', artist: 'Various Artists', title: 'Pulp Fiction', variousArtists: true, year: 1994 })
  // album_type 'compilation' alone flags it even when the credited artist isn't literally "Various Artists"
  expect(res.items[1]).toMatchObject({ variousArtists: true, year: 2001 })
  // a normal album is not flagged and has no variousArtists key
  expect(res.items[2].variousArtists).toBeUndefined()
  expect(res.items[2].year).toBe(2010)
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- tests/spotify.test.ts`
Expected: FAIL — `variousArtists`/`year` undefined.

- [ ] **Step 3: Widen the API album type** — in `server/utils/spotify.ts` replace the `ApiAlbum` interface (line ~47):

```ts
interface ApiAlbum { id?: unknown, name?: unknown, album_type?: unknown, release_date?: unknown, artists?: ApiArtist[] }
```

- [ ] **Step 4: Add a year parser + flag rows** — in `server/utils/spotify.ts`, add the import and a helper, then update the push in `albumItemsFromTracks`. Add to the existing import from `./matching` (create the import if absent):

```ts
import { isVariousArtists } from './matching'
```

Add near `cleanString`:

```ts
// Spotify release_date is 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'. Take the year only.
function releaseYear(v: unknown): number | undefined {
  if (typeof v !== 'string')
    return undefined
  const y = Number.parseInt(v.slice(0, 4), 10)
  return Number.isFinite(y) ? y : undefined
}
```

Replace the `out.push(...)` line in `albumItemsFromTracks` with:

```ts
    const isComp = album?.album_type === 'compilation' || isVariousArtists(artist)
    const item: ParsedItem = { raw: `${artist} - ${title}`, kind: 'album', artist, title }
    const year = releaseYear(album?.release_date)
    if (year !== undefined)
      item.year = year
    if (isComp)
      item.variousArtists = true
    out.push(item)
```

- [ ] **Step 5: Widen the fetch fields** — in `fetchPlaylistTracks` (line ~248) replace the `fields=` query so the album sub-select includes `album_type` and `release_date`:

```ts
    `/playlists/${encoded}/tracks?limit=100&fields=next,items(is_local,track(type,album(id,name,album_type,release_date,artists(name))))`,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pnpm test -- tests/spotify.test.ts`
Expected: PASS (existing `albumItemsFromTracks` tests still green — new keys are additive).

- [ ] **Step 7: Commit**

```bash
git add server/utils/spotify.ts tests/spotify.test.ts
git commit -m "feat(spotify): flag Various Artists comps + carry release year"
```

---

### Task 3: Flag VA in the free-text album parser

**Files:**
- Modify: `server/utils/parsers.ts` (import `isVariousArtists`; set flag on split album rows)
- Test: `tests/parsers.test.ts`

**Interfaces:**
- Consumes: `isVariousArtists` (Task 1)
- Produces: `parseAlbums` sets `variousArtists: true` when the parsed artist is a VA token

- [ ] **Step 1: Write the failing test** — append to `tests/parsers.test.ts`:

```ts
it('flags Various Artists rows from free text', () => {
  const out = parseAlbums('Various Artists - Pulp Fiction\nvarious - Trainspotting\nReal Band - Their LP')
  const byTitle = (t: string) => out.find(i => i.title === t)
  expect(byTitle('Pulp Fiction')?.variousArtists).toBe(true)
  expect(byTitle('Trainspotting')?.variousArtists).toBe(true)
  expect(byTitle('Their LP')?.variousArtists).toBeUndefined()
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- tests/parsers.test.ts`
Expected: FAIL — `variousArtists` undefined.

- [ ] **Step 3: Implement** — in `server/utils/parsers.ts` add the import at the top:

```ts
import { isVariousArtists } from './matching'
```

Then add a helper above `parseAlbums` and use it for each split-album push:

```ts
function albumItem(rawLine: string, artist: string, title: string): ParsedItem {
  const item: ParsedItem = { raw: rawLine, kind: 'album', artist, title }
  if (isVariousArtists(artist))
    item.variousArtists = true
  return item
}
```

Replace each of the four `out.push({ raw: rawLine, kind: 'album', artist: ..., title: ... })` calls inside `parseAlbums` (the CSV, pipe, "by", and dash branches) with `out.push(albumItem(rawLine, <artist>, <title>))`, e.g. the dash branch becomes:

```ts
      if (a && b) {
        out.push(albumItem(rawLine, clean(a), clean(b)))
        continue
      }
```

(The needs-review branch at the end is unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm test -- tests/parsers.test.ts`
Expected: PASS (existing parser tests still green).

- [ ] **Step 5: Commit**

```bash
git add server/utils/parsers.ts tests/parsers.test.ts
git commit -m "feat(parsers): flag Various Artists rows in free-text album parse"
```

---

### Task 4: Metadata backend client (`api.lidarr.audio`) + VA album resolver

**Files:**
- Modify: `server/utils/env.ts` (add `LIDARR_METADATA_URL`)
- Create: `server/utils/metadata.ts`
- Test: `tests/metadata.test.ts`

**Interfaces:**
- Consumes: `normKey`, `similarity` (matching.ts); `loadEnv` (env.ts)
- Produces: `VARIOUS_ARTISTS_MBID: string`; `rankVaAlbums(entries, title, year?, limit?): VaAlbumMatch[]`; `resolveVariousArtistsAlbumMbids(title, year?, limit?): Promise<string[]>`; `interface VaAlbumMatch { mbid: string, title: string, year?: number }`

- [ ] **Step 1: Add the env var** — in `server/utils/env.ts` add inside the schema (after `LIDARR_API_KEY`):

```ts
  // Lidarr's metadata backend, used to resolve Various Artists comps by MBID
  // (Lidarr's own /album/lookup hides the special VA entity from text search).
  LIDARR_METADATA_URL: z.string().url().default('https://api.lidarr.audio/api/v0.4'),
```

- [ ] **Step 2: Write the failing test** — create `tests/metadata.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { rankVaAlbums, VARIOUS_ARTISTS_MBID } from '../server/utils/metadata'

const VA = VARIOUS_ARTISTS_MBID
function entry(id: string, title: string, artistid: string, releasedate?: string) {
  return { album: { id, title, artistid, releasedate } }
}

describe('rankVaAlbums', () => {
  it('keeps only VA-credited albums and ranks by title similarity', () => {
    const out = rankVaAlbums([
      entry('m1', 'Pulp Fiction', VA, '1994-09-26'),
      entry('m2', 'Pulp Fiction Karaoke', VA, '2005'),
      entry('m3', 'Pulp Fiction', 'real-artist-id', '1994'), // not VA → dropped
      { album: null },
    ], 'Pulp Fiction')
    expect(out.map(m => m.mbid)).toEqual(['m1', 'm2'])
    expect(out[0]).toMatchObject({ mbid: 'm1', title: 'Pulp Fiction', year: 1994 })
  })

  it('uses release year as a tiebreak between equally-titled comps', () => {
    const out = rankVaAlbums([
      entry('old', 'Greatest Hits', VA, '1980'),
      entry('new', 'Greatest Hits', VA, '2014'),
    ], 'Greatest Hits', 2014)
    expect(out[0].mbid).toBe('new')
  })

  it('dedupes by mbid and respects the limit', () => {
    const out = rankVaAlbums([entry('m1', 'X', VA), entry('m1', 'X', VA), entry('m2', 'X', VA)], 'X', undefined, 1)
    expect(out).toHaveLength(1)
  })
})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pnpm test -- tests/metadata.test.ts`
Expected: FAIL — cannot find module `metadata`.

- [ ] **Step 4: Implement** — create `server/utils/metadata.ts`:

```ts
// Client for Lidarr's metadata backend (api.lidarr.audio). Used only to resolve
// Various Artists compilations: Lidarr's own /album/lookup deliberately hides the
// special VA entity from text search, but the backend's raw search returns it, so
// we search by title here, keep the VA-credited albums, and hand their MBIDs back
// to the normal lookup-by-MBID + add pipeline. Pure ranking is split from the
// fetch so it can be unit-tested (mirrors spotify.ts / openai.ts).
import { loadEnv } from './env'
import { normKey, similarity } from './matching'

export const VARIOUS_ARTISTS_MBID = '89ad4ac3-39f7-470e-963a-56509c546377'

interface MetaAlbum { id?: unknown, title?: unknown, artistid?: unknown, releasedate?: unknown }
interface MetaSearchEntry { album?: MetaAlbum | null }
export interface VaAlbumMatch { mbid: string, title: string, year?: number }

function year(v: unknown): number | undefined {
  if (typeof v !== 'string')
    return undefined
  const y = Number.parseInt(v.slice(0, 4), 10)
  return Number.isFinite(y) ? y : undefined
}

export function rankVaAlbums(entries: MetaSearchEntry[], title: string, want?: number, limit = 5): VaAlbumMatch[] {
  const target = normKey(title)
  const scored = entries
    .map(e => e.album)
    .filter((a): a is { id: string, title: string, artistid: string, releasedate?: string } =>
      !!a && a.artistid === VARIOUS_ARTISTS_MBID && typeof a.id === 'string' && typeof a.title === 'string')
    .map((a) => {
      const y = year(a.releasedate)
      // Title similarity dominates; year proximity is a small tiebreak (<=0.05).
      const yearPenalty = want !== undefined && y !== undefined ? Math.min(Math.abs(y - want), 50) / 1000 : 0
      return { mbid: a.id, title: a.title, year: y, score: similarity(normKey(a.title), target) - yearPenalty }
    })
    .sort((x, z) => z.score - x.score)
  const seen = new Set<string>()
  const out: VaAlbumMatch[] = []
  for (const m of scored) {
    if (seen.has(m.mbid))
      continue
    seen.add(m.mbid)
    out.push({ mbid: m.mbid, title: m.title, year: m.year })
    if (out.length >= limit)
      break
  }
  return out
}

export async function resolveVariousArtistsAlbumMbids(title: string, want?: number, limit = 5): Promise<string[]> {
  const base = loadEnv().LIDARR_METADATA_URL.replace(/\/$/, '')
  const url = `${base}/search?type=all&query=${encodeURIComponent(title)}`
  const res = await fetch(url, { headers: { 'User-Agent': 'lidarr-bulk' } })
  if (!res.ok)
    throw new Error(`metadata search failed (${res.status})`)
  const body = await res.json() as MetaSearchEntry[]
  return rankVaAlbums(Array.isArray(body) ? body : [], title, want, limit).map(m => m.mbid)
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pnpm test -- tests/metadata.test.ts`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/utils/env.ts server/utils/metadata.ts tests/metadata.test.ts
git commit -m "feat(metadata): api.lidarr.audio client + Various Artists MBID resolver"
```

---

### Task 5: Query-variation fallback + VA branch in `searchCandidates`

**Files:**
- Modify: `server/utils/matching.ts` (add `stripEditionAppendix`, `albumQueryVariations`)
- Modify: `server/utils/jobs.ts:24` (imports) and `:333-367` (`searchCandidates`)
- Test: `tests/matching.test.ts`

**Interfaces:**
- Consumes: `resolveVariousArtistsAlbumMbids` (Task 4); `ParsedItem.variousArtists`/`year` (Task 1)
- Produces: `stripEditionAppendix(title: string): string`; `albumQueryVariations(parsed: ParsedItem): string[]`

- [ ] **Step 1: Write the failing test** — append to `tests/matching.test.ts`:

```ts
import { albumQueryVariations, stripEditionAppendix } from '../server/utils/matching'

describe('stripEditionAppendix', () => {
  it('strips parenthetical and trailing edition suffixes', () => {
    expect(stripEditionAppendix('Powerslave (2015 Remaster)')).toBe('Powerslave')
    expect(stripEditionAppendix('The Album - Deluxe Edition')).toBe('The Album')
    expect(stripEditionAppendix('Songs (Expanded Edition)')).toBe('Songs')
  })
  it('leaves a clean title unchanged', () => {
    expect(stripEditionAppendix('Master of Puppets')).toBe('Master of Puppets')
  })
})

describe('albumQueryVariations', () => {
  it('orders original, paren-stripped, edition-stripped, deduped, with the artist prefix', () => {
    expect(albumQueryVariations({ raw: 'x', kind: 'album', artist: 'Iron Maiden', title: 'Powerslave (2015 Remaster)' }))
      .toEqual(['Iron Maiden Powerslave (2015 Remaster)', 'Iron Maiden Powerslave'])
  })
  it('a clean title yields a single variation', () => {
    expect(albumQueryVariations({ raw: 'x', kind: 'album', artist: 'A', title: 'B' })).toEqual(['A B'])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- tests/matching.test.ts`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement the variation helpers** — append to `server/utils/matching.ts` (add `ParsedItem` to the existing type import at the top of the file):

```ts
// Trailing "edition" appendices that make a lookup miss when present. Anchored to
// the end so an album literally named "Remaster" isn't mangled mid-title.
const EDITION_SUFFIX = /\s*[-–—]?\s*(?:deluxe|expanded|special|extended|anniversary|legacy|collector'?s|bonus[\s-]?track|remaster(?:ed)?)(?:\s+(?:edition|version|reissue))?\s*$/i

export function stripEditionAppendix(title: string): string {
  const noParens = title.replace(/[([][^)\]]*[)\]]/g, ' ').replace(/\s+/g, ' ').trim()
  const noEdition = noParens.replace(EDITION_SUFFIX, '').trim()
  return noEdition || noParens || title
}

// Ordered, deduped Lidarr lookup terms for an album row: as-typed first, then
// progressively stripped variants. searchCandidates tries them until one yields
// a title-similar hit, so a stray "(Deluxe Edition)" no longer dead-ends.
export function albumQueryVariations(parsed: ParsedItem): string[] {
  const title = parsed.title ?? parsed.raw
  const artist = parsed.artist ?? ''
  const term = (t: string): string => (artist ? `${artist} ${t}` : t).trim()
  const parenStripped = title.replace(/[([][^)\]]*[)\]]/g, ' ').replace(/\s+/g, ' ').trim()
  const editionStripped = stripEditionAppendix(title)
  return [...new Set([term(title), term(parenStripped), term(editionStripped)].filter(Boolean))]
}
```

- [ ] **Step 4: Run the variation tests to verify they pass**

Run: `pnpm test -- tests/matching.test.ts`
Expected: PASS.

- [ ] **Step 5: Wire `searchCandidates`** — in `server/utils/jobs.ts` update the matching import (line ~24) to:

```ts
import { albumQueryVariations, isVariousArtists, normKeyLoose, pickAutoMatch, rankCandidates, similarity } from './matching'
```

Add a metadata import near the other util imports:

```ts
import { resolveVariousArtistsAlbumMbids } from './metadata'
```

Replace the entire body of `searchCandidates` (lines ~333-367) with:

```ts
async function searchCandidates(kind: Kind, parsed: ParsedItem): Promise<Candidate[]> {
  if (kind === 'artist') {
    const res = await retryOnTransient(() => lookupArtist(parsed.raw))
    return res.map(value => ({ kind: 'artist', value }))
  }

  // Various Artists compilations: Lidarr text search hides the special VA entity,
  // so resolve the comp's MBID via the metadata backend and look it up by id.
  if (parsed.variousArtists || isVariousArtists(parsed.artist)) {
    const mbids = await resolveVariousArtistsAlbumMbids(parsed.title ?? parsed.raw, parsed.year).catch(() => [] as string[])
    const looked = await Promise.all(
      mbids.map(mbid => retryOnTransient(() => lookupAlbum(`lidarr:${mbid}`)).catch(() => [])),
    )
    const seen = new Set<string>()
    const out: Candidate[] = []
    for (const value of looked.flat()) {
      if (seen.has(value.foreignAlbumId))
        continue
      seen.add(value.foreignAlbumId)
      out.push({ kind: 'album', value })
    }
    return out
  }

  // Normal albums: try query variations (as-typed, paren-stripped, edition-
  // stripped) until one returns a title-similar hit; merge any extra results in.
  const variations = albumQueryVariations(parsed)
  const want = normKeyLoose(parsed.title ?? parsed.raw)
  const merged: LidarrAlbumCandidate[] = []
  const seen = new Set<string>()
  for (const term of variations) {
    const res = await retryOnTransient(() => lookupAlbum(term)).catch(() => [] as LidarrAlbumCandidate[])
    for (const c of res) {
      if (!seen.has(c.foreignAlbumId)) {
        seen.add(c.foreignAlbumId)
        merged.push(c)
      }
    }
    if (merged.some(c => similarity(normKeyLoose(c.title), want) > 0.8))
      break
  }
  return merged.map(value => ({ kind: 'album', value }))
}
```

Add the `LidarrAlbumCandidate` type to the `~~/shared/types` import at the top of `jobs.ts` (it is referenced above):

```ts
import type {
  AppSettings,
  Candidate,
  JobItem,
  JobSnapshot,
  Kind,
  LidarrAlbumCandidate,
  ParsedItem,
} from '~~/shared/types'
```

- [ ] **Step 6: Run the full suite + lint**

Run: `pnpm test && pnpm lint`
Expected: PASS. (The old inline parens-fallback block is now replaced by the variation loop; no test depended on it directly.)

- [ ] **Step 7: Commit**

```bash
git add server/utils/matching.ts server/utils/jobs.ts tests/matching.test.ts
git commit -m "feat(jobs): VA-by-MBID resolution + query-variation lookup fallback"
```

---

### Task 6: Image-fetch error resilience on add

**Files:**
- Modify: `server/utils/jobs.ts` (add `isImageFetchError`; harden `processAdd`)
- Test: `tests/jobs.test.ts`

**Interfaces:**
- Consumes: existing `addToLidarr`, `nudgeExisting`, `setStatus`
- Produces: `isImageFetchError(msg: string): boolean` (exported)

- [ ] **Step 1: Write the failing test** — append to `tests/jobs.test.ts`:

```ts
import { isImageFetchError } from '../server/utils/jobs'

describe('isImageFetchError', () => {
  it('matches Lidarr image/cover fetch failures', () => {
    for (const m of [
      'Lidarr 500: failed to download image',
      'Error fetching cover art for album',
      'MediaCover refresh failed',
    ])
      expect(isImageFetchError(m)).toBe(true)
  })
  it('does not match unrelated errors', () => {
    for (const m of ['album has already been added', 'Lidarr 404 not found'])
      expect(isImageFetchError(m)).toBe(false)
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- tests/jobs.test.ts`
Expected: FAIL — `isImageFetchError is not a function`.

- [ ] **Step 3: Implement the classifier** — in `server/utils/jobs.ts` add (near `TRANSIENT_LOOKUP_ERROR`):

```ts
// Lidarr occasionally fails an add while fetching artwork from its metadata
// server. The record itself is usually created; the image is cosmetic. Detect
// these so processAdd can retry once and then verify rather than hard-erroring.
export function isImageFetchError(msg: string): boolean {
  return /image|mediacover|cover art|cover image|failed to (?:download|fetch)/i.test(msg)
}
```

- [ ] **Step 4: Harden `processAdd`** — in `server/utils/jobs.ts`, replace the `catch` block of `processAdd` (the `catch (err: unknown) { ... }` starting ~line 264) with:

```ts
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    console.error('[job]', j.id, 'add failed:', msg)
    // Already in Lidarr is no longer a dead end: nudge the existing record.
    if (/already been added/i.test(msg) && item.chosen) {
      try {
        const summary = await nudgeExisting(item.chosen, j.monitorMode)
        setStatus(j, item, { status: 'nudged', message: summary })
      }
      catch (nudgeErr: unknown) {
        const nudgeMsg = nudgeErr instanceof Error ? nudgeErr.message : String(nudgeErr)
        setStatus(j, item, { status: 'error', message: `already in lidarr but nudge failed: ${nudgeMsg}` })
      }
      return
    }
    // Image-fetch failures are usually transient and the record often got created
    // anyway: retry once, and if Lidarr then reports it already added, nudge it.
    if (isImageFetchError(msg) && item.chosen) {
      try {
        await new Promise(r => setTimeout(r, 1500))
        const added = await addToLidarr(item.chosen, effective, j.monitorMode)
        setStatus(j, item, { status: 'searching-on-lidarr' })
        if (j.kind === 'album' && added.albumId && added.artistId) {
          await waitForArtistRefresh(added.artistId).catch(() => undefined)
          await monitorAlbums([added.albumId], true).catch(() => undefined)
        }
        setStatus(j, item, { status: 'done' })
        return
      }
      catch (retryErr: unknown) {
        const retryMsg = retryErr instanceof Error ? retryErr.message : String(retryErr)
        if (/already been added/i.test(retryMsg)) {
          try {
            const summary = await nudgeExisting(item.chosen, j.monitorMode)
            setStatus(j, item, { status: 'nudged', message: `${summary} (image fetch retried)` })
            return
          }
          catch { /* fall through to error below */ }
        }
        setStatus(j, item, { status: 'error', message: `image-fetch add failed twice: ${retryMsg}` })
        return
      }
    }
    setStatus(j, item, { status: 'error', message: msg })
  }
```

- [ ] **Step 5: Run the full suite + lint**

Run: `pnpm test && pnpm lint`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/utils/jobs.ts tests/jobs.test.ts
git commit -m "feat(jobs): retry+verify on Lidarr image-fetch add errors"
```

---

### Task 7: Build, deploy, and live verification

**Files:** none (operational). Run from the nas stack root `/home/tom/nas`.

- [ ] **Step 1: Typecheck + full gate locally**

Run (from `webapps/lidarr-bulk`): `pnpm lint && pnpm test`
Expected: all PASS.

- [ ] **Step 2: Rebuild + restart the container** (per the lidarr-bulk addAlbum-clobber memory, code changes need a rebuild)

Run (from `/home/tom/nas`): `docker compose up -d --build lidarr-bulk`
Expected: container recreated, healthy.

- [ ] **Step 3: Confirm `LIDARR_METADATA_URL` default is in effect** (only set the env if the backend host ever changes; default is baked in).

Run: `docker compose exec lidarr-bulk printenv | grep -i metadata || echo 'using default api.lidarr.audio'`
Expected: prints the default note or an override value.

- [ ] **Step 4: Live end-to-end VA add via the app** — through the Spotify tab (or free-text album box), submit `Various Artists - Pulp Fiction: Music From the Motion Picture`. Confirm in Lidarr it links under the existing "Various Artists" artist (id 2246) and the comp shows monitored. Then remove that one album in Lidarr to restore state (matches the design's verified curl test).
Expected: item reaches `done`; album present under VA; removed cleanly afterward.

- [ ] **Step 5: Commit any deploy notes if config changed** (skip if nothing changed).

```bash
git add -A && git commit -m "chore(lidarr-bulk): deploy Various Artists comp support"
```

---

## Self-Review

**Spec coverage:**
- VA detection (Spotify) → Task 2; (free-text) → Task 3; helper → Task 1. ✅
- MBID resolution via api.lidarr.audio → Task 4. ✅
- Branch in searchCandidates feeding unchanged addAlbum → Task 5. ✅
- Name-variation fallback (parens + edition suffix) → Task 5. ✅
- Image-fetch retry-then-verify + logging → Task 6 (plus `console.error` on every add failure). ✅
- `LIDARR_METADATA_URL` env → Task 4. ✅
- Build/restart per addAlbum-clobber memory + live verification → Task 7. ✅
- Out-of-scope (explode-to-artists, general MB client) → intentionally absent. ✅

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows full code. ✅

**Type consistency:** `isVariousArtists`, `albumQueryVariations`, `stripEditionAppendix` (matching.ts); `rankVaAlbums`, `resolveVariousArtistsAlbumMbids`, `VARIOUS_ARTISTS_MBID`, `VaAlbumMatch` (metadata.ts); `isImageFetchError` (jobs.ts); `ParsedItem.variousArtists`/`year` (types.ts) — names used identically across consuming tasks. `LidarrAlbumCandidate` import added in Task 5 where referenced. ✅
