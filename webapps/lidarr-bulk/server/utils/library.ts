// What Lidarr already has. Consulted *before* any MusicBrainz lookup, so a row for
// an album that is already sitting complete on disk costs nothing: no search, no
// add attempt, no nudge, and above all no download request.
//
// Why this is worth a module: a metal playlist run against a library that already
// holds 14k complete albums is mostly rows we own. Each of those used to spend the
// full cascade (1–4 Lidarr lookups, sometimes an artist lookup plus discography
// fetches), then an addAlbum that fails with "already been added", then a nudge —
// all to conclude there was nothing to do.
//
// Scoping matters. GET /api/v1/album returns the *whole* library: 56k albums and
// 431 MB, 17 s — unusable per job. The artist list is 3.2k rows / 25 MB / 2 s, and
// a single artist's albums is ~300 KB / 45 ms. So we index artists once, then pull
// only the discographies the playlist actually mentions.

import type { ParsedItem } from '~~/shared/types'
import { libraryAlbums, libraryArtists } from './lidarr'
import { pickAutoMatch } from './matching'
import { bestCrossScriptSimilarity } from './script'
import { normKey } from './text'

export interface ExistingAlbum {
  albumId: number
  artistId: number
  title: string
  artistName: string
  // Fraction of the album's tracks present on disk, 0–100. `undefined` when Lidarr
  // has not produced statistics yet (freshly added, not refreshed).
  percentOfTracks?: number
  complete: boolean
}

// Same bar as the matcher's ARTIST_IDENTITY — one notion of "same artist".
const MIN_ARTIST_IDENTITY = 0.9
const CACHE_TTL_MS = 5 * 60_000

interface LibraryArtist { id: number, name: string, foreignArtistId?: string }

let artistIndex: { at: number, artists: LibraryArtist[] } | null = null
const albumsByArtist = new Map<number, { at: number, albums: ExistingAlbum[] }>()
let inFlightArtists: Promise<LibraryArtist[]> | null = null
const inFlightAlbums = new Map<number, Promise<ExistingAlbum[]>>()

export function clearLibraryCache(): void {
  artistIndex = null
  albumsByArtist.clear()
  inFlightArtists = null
  inFlightAlbums.clear()
}

async function artists(): Promise<LibraryArtist[]> {
  const now = Date.now()
  if (artistIndex && now - artistIndex.at < CACHE_TTL_MS)
    return artistIndex.artists
  if (inFlightArtists)
    return inFlightArtists
  inFlightArtists = libraryArtists()
    .then((rows) => {
      artistIndex = { at: Date.now(), artists: rows }
      return rows
    })
    .catch((err: unknown) => {
      console.error('[library] artist index failed:', err instanceof Error ? err.message : String(err))
      return [] as LibraryArtist[]
    })
    .finally(() => {
      inFlightArtists = null
    })
  return inFlightArtists
}

async function albumsFor(artistId: number): Promise<ExistingAlbum[]> {
  const now = Date.now()
  const hit = albumsByArtist.get(artistId)
  if (hit && now - hit.at < CACHE_TTL_MS)
    return hit.albums
  const pending = inFlightAlbums.get(artistId)
  if (pending)
    return pending
  const p = libraryAlbums(artistId)
    .then((albums) => {
      albumsByArtist.set(artistId, { at: Date.now(), albums })
      return albums
    })
    .catch((err: unknown) => {
      console.error('[library] albums fetch failed for artist', artistId, err instanceof Error ? err.message : String(err))
      return [] as ExistingAlbum[]
    })
    .finally(() => {
      inFlightAlbums.delete(artistId)
    })
  inFlightAlbums.set(artistId, p)
  return p
}

// Cross-script so a romanized Spotify artist finds the Cyrillic-named record we
// already imported — the same reason the Lidarr matcher folds scripts.
export function matchLibraryArtists(wanted: string | undefined, rows: LibraryArtist[]): LibraryArtist[] {
  const want = normKey(wanted)
  if (!want)
    return []
  return rows.filter(a => bestCrossScriptSimilarity(normKey(a.name), want) >= MIN_ARTIST_IDENTITY)
}

// Find the requested album among an artist's existing releases. Reuses the shared
// matcher with completeCatalogue: this *is* the artist's full local catalogue, so
// "nothing else comes close" is a fact here, exactly as with a discography.
export function matchLibraryAlbum(parsed: ParsedItem, albums: ExistingAlbum[]): ExistingAlbum | undefined {
  if (albums.length === 0 || !parsed.title)
    return undefined
  const byId = new Map(albums.map(a => [String(a.albumId), a]))
  const chosen = pickAutoMatch(
    'album',
    parsed,
    albums.map(a => ({
      kind: 'album' as const,
      value: {
        foreignAlbumId: String(a.albumId),
        title: a.title,
        artist: { artistName: a.artistName },
      },
    })),
    { artistProven: true, requireTitleEvidence: true, allowSubtitleMatch: true, completeCatalogue: true },
  )
  if (!chosen || chosen.kind !== 'album')
    return undefined
  return byId.get(chosen.value.foreignAlbumId)
}

// The album this row refers to, if Lidarr already holds it. Null when we don't, or
// when the library index is unavailable — callers then fall through to the normal
// lookup path, so an outage here only costs the optimisation, never correctness.
export async function findExistingAlbum(parsed: ParsedItem): Promise<ExistingAlbum | null> {
  if (!parsed.artist || !parsed.title)
    return null
  const candidates = matchLibraryArtists(parsed.artist, await artists())
  for (const artist of candidates) {
    const hit = matchLibraryAlbum(parsed, await albumsFor(artist.id))
    if (hit)
      return hit
  }
  return null
}
