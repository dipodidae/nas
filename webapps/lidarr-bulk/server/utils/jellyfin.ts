// Jellyfin music-library client + pure matching. Mirrors spotify.ts: pure
// helpers (unit-tested) live beside thin REST calls (untested) so the matching
// logic can be verified without a live Jellyfin. Reuses the album/artist
// normalization from matching.ts (normKey / normKeyLoose / similarity).
import type { Env } from './env'
import type { JellyfinPushResult, SpotifyTrack } from '~~/shared/types'
import { mapWithConcurrency } from './concurrency'
import { artistNameVariants, normKeyLoose } from './matching'
import { bestCrossScriptSimilarity } from './script'
import { normKey } from './text'

// A title is "matched" when its loose-normalized name is ~equal to the track's
// and the artist matches; reuse the same strict threshold matching.ts uses.
const MIN_TITLE_SIMILARITY = 0.95
const MIN_ARTIST_SIMILARITY = 0.9
// Jellyfin is on the LAN and answers quickly, but a 1842-track playlist is still
// 1842 searches; 8 in flight keeps it responsive without hammering the server.
const SEARCH_CONCURRENCY = 8

export interface JellyfinAudioItem {
  Id: string
  Name?: string
  Artists?: string[]
  AlbumArtist?: string
}

export function jellyfinEnabled(env: Env): boolean {
  return Boolean(env.JELLYFIN_URL && env.JELLYFIN_API_KEY && env.JELLYFIN_USER_ID)
}

// Drop title noise that varies between Spotify and a Jellyfin rip but doesn't
// change the song: "feat./ft. …" tails and "(feat. …)" parentheticals, plus
// dash-style remaster suffixes. So "Song (feat. X)", "Song feat. X" and
// "Song - 2010 Remaster" all reduce to "Song" before normalization.
export function stripNoise(s: string): string {
  return s
    .replace(/\s*[([]\s*(feat\.?|ft\.?)\b[^)\]]*[)\]]/gi, '')
    .replace(/\s+(feat\.?|ft\.?)\b.*$/i, '')
    .replace(/\s*-\s*[^-]*\bremaster(ed)?\b.*$/i, '')
    .trim()
}

// Cross-script, for the same reason the Lidarr matcher is: Spotify hands us a
// romanized artist ("Basta") while the file we downloaded through Lidarr is tagged
// with MusicBrainz's native spelling ("Баста"). Comparing those as raw strings
// scores 0, so a playlist that queued and downloaded perfectly would then recreate
// as empty in Jellyfin — the same input that worked upstream has to work here.
function titleSimilarity(a: string, b: string): number {
  const x = stripNoise(a)
  const y = stripNoise(b)
  return Math.max(
    bestCrossScriptSimilarity(normKeyLoose(x), normKeyLoose(y)),
    bestCrossScriptSimilarity(normKey(x), normKey(y)),
  )
}

function artistMatches(trackArtist: string, item: JellyfinAudioItem): boolean {
  const names = [...(item.Artists ?? []), item.AlbumArtist].filter((n): n is string => Boolean(n))
  // Spotify qualifiers ("Trial (swe)") are not part of the name the file carries.
  const wants = artistNameVariants(trackArtist).map(normKey).filter(Boolean)
  return names.some(n =>
    wants.some(want => bestCrossScriptSimilarity(normKey(n), want) >= MIN_ARTIST_SIMILARITY))
}

// Best Jellyfin item id for a track, or null if nothing clears the bar.
export function pickBestMatch(track: SpotifyTrack, candidates: JellyfinAudioItem[]): string | null {
  let bestId: string | null = null
  let bestScore = 0
  for (const c of candidates) {
    if (!c.Id || !c.Name)
      continue
    if (!artistMatches(track.artist, c))
      continue
    const score = titleSimilarity(track.title, c.Name)
    if (score >= MIN_TITLE_SIMILARITY && score > bestScore) {
      bestScore = score
      bestId = c.Id
    }
  }
  return bestId
}

function authHeaders(env: Env): Record<string, string> {
  return { 'X-Emby-Token': env.JELLYFIN_API_KEY, 'Accept': 'application/json' }
}

async function jf(env: Env, path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${env.JELLYFIN_URL}${path}`, {
    ...init,
    headers: { ...authHeaders(env), ...(init?.headers ?? {}) },
  })
  if (!res.ok)
    throw new Error(`Jellyfin ${path} failed (${res.status}): ${await res.text()}`)
  return res
}

// Search the user's library for Audio items matching a title (the track's
// artist is applied afterwards by pickBestMatch).
export async function searchAudio(env: Env, title: string): Promise<JellyfinAudioItem[]> {
  const params = new URLSearchParams({
    userId: env.JELLYFIN_USER_ID,
    searchTerm: title,
    IncludeItemTypes: 'Audio',
    Recursive: 'true',
    Fields: 'Artists,AlbumArtist',
    Limit: '25',
  })
  const res = await jf(env, `/Items?${params.toString()}`)
  const body = await res.json() as { Items?: JellyfinAudioItem[] }
  return body.Items ?? []
}

export async function findPlaylistByName(env: Env, name: string): Promise<{ Id: string } | null> {
  const params = new URLSearchParams({
    userId: env.JELLYFIN_USER_ID,
    IncludeItemTypes: 'Playlist',
    Recursive: 'true',
  })
  const res = await jf(env, `/Items?${params.toString()}`)
  const body = await res.json() as { Items?: { Id: string, Name?: string }[] }
  const want = name.trim().toLowerCase()
  const hit = (body.Items ?? []).find(p => (p.Name ?? '').trim().toLowerCase() === want)
  return hit ? { Id: hit.Id } : null
}

export async function deletePlaylist(env: Env, id: string): Promise<void> {
  await jf(env, `/Items/${encodeURIComponent(id)}`, { method: 'DELETE' })
}

export async function createPlaylist(env: Env, name: string, itemIds: string[]): Promise<string> {
  const res = await jf(env, '/Playlists', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ Name: name, Ids: itemIds, UserId: env.JELLYFIN_USER_ID, MediaType: 'Audio' }),
  })
  const body = await res.json() as { Id?: string }
  return body.Id ?? ''
}

// Best-effort: fetch the remote cover and set it as the playlist's primary
// image. Jellyfin's image endpoint wants the bytes base64-encoded in the body
// with the image MIME as Content-Type. Any failure is swallowed by the caller —
// a missing cover must never fail the recreate.
export async function setPlaylistImage(env: Env, playlistId: string, imageUrl: string): Promise<void> {
  const img = await fetch(imageUrl)
  if (!img.ok)
    throw new Error(`cover fetch failed (${img.status})`)
  const contentType = img.headers.get('content-type') ?? 'image/jpeg'
  const base64 = Buffer.from(await img.arrayBuffer()).toString('base64')
  await jf(env, `/Items/${encodeURIComponent(playlistId)}/Images/Primary`, {
    method: 'POST',
    headers: { 'Content-Type': contentType },
    body: base64,
  })
}

// Match each track against the library (order-preserving, deduped by item id),
// then replace any same-name playlist with a fresh one. Per-track search errors
// are swallowed (track is skipped); only connection/auth-level errors propagate.
// Zero matches → create nothing and leave any existing playlist untouched.
export async function recreatePlaylistInJellyfin(
  env: Env,
  name: string,
  tracks: SpotifyTrack[],
  imageUrl?: string,
): Promise<JellyfinPushResult> {
  const matchedIds: string[] = []
  const seen = new Set<string>()
  const skipped: { title: string, artist: string }[] = []

  // Searched in parallel — a 1842-track playlist done one request at a time is
  // minutes of pure waiting. Results are collected into ordered slots and only
  // then walked in order, so playlist order and dedupe-by-first-occurrence are
  // exactly as they were when this was a serial loop.
  const ids = await mapWithConcurrency(tracks, SEARCH_CONCURRENCY, async (track) => {
    try {
      return pickBestMatch(track, await searchAudio(env, stripNoise(track.title)))
    }
    catch {
      return null // per-track search failure → treat as a miss, keep going
    }
  })

  tracks.forEach((track, i) => {
    const id = ids[i]
    if (id && !seen.has(id)) {
      seen.add(id)
      matchedIds.push(id)
    }
    else if (!id) {
      skipped.push({ title: track.title, artist: track.artist })
    }
  })

  if (matchedIds.length === 0)
    return { playlistName: name, total: tracks.length, matched: 0, skipped, jellyfinPlaylistId: null }

  const existing = await findPlaylistByName(env, name)
  if (existing)
    await deletePlaylist(env, existing.Id)
  const jellyfinPlaylistId = await createPlaylist(env, name, matchedIds)

  if (imageUrl && jellyfinPlaylistId) {
    try {
      await setPlaylistImage(env, jellyfinPlaylistId, imageUrl)
    }
    catch {
      // best-effort cover — playlist creation already succeeded, so ignore.
    }
  }

  return { playlistName: name, total: tracks.length, matched: matchedIds.length, skipped, jellyfinPlaylistId }
}
