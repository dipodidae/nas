// Jellyfin music-library client + pure matching. Mirrors spotify.ts: pure
// helpers (unit-tested) live beside thin REST calls (untested) so the matching
// logic can be verified without a live Jellyfin. Reuses the album/artist
// normalization from matching.ts (normKey / normKeyLoose / similarity).
import type { Env } from './env'
import type { JellyfinPushResult, SpotifyTrack } from '~~/shared/types'
import { normKey, normKeyLoose, similarity } from './matching'

// A title is "matched" when its loose-normalized name is ~equal to the track's
// and the artist matches; reuse the same strict threshold matching.ts uses.
const MIN_TITLE_SIMILARITY = 0.95
const MIN_ARTIST_SIMILARITY = 0.9

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

function titleSimilarity(a: string, b: string): number {
  return Math.max(
    similarity(normKeyLoose(stripNoise(a)), normKeyLoose(stripNoise(b))),
    similarity(normKey(stripNoise(a)), normKey(stripNoise(b))),
  )
}

function artistMatches(trackArtist: string, item: JellyfinAudioItem): boolean {
  const want = normKey(trackArtist)
  const names = [...(item.Artists ?? []), item.AlbumArtist].filter((n): n is string => Boolean(n))
  return names.some(n => similarity(normKey(n), want) >= MIN_ARTIST_SIMILARITY)
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

// Match each track against the library (order-preserving, deduped by item id),
// then replace any same-name playlist with a fresh one. Per-track search errors
// are swallowed (track is skipped); only connection/auth-level errors propagate.
// Zero matches → create nothing and leave any existing playlist untouched.
export async function recreatePlaylistInJellyfin(
  env: Env,
  name: string,
  tracks: SpotifyTrack[],
): Promise<JellyfinPushResult> {
  const matchedIds: string[] = []
  const seen = new Set<string>()
  const skipped: { title: string, artist: string }[] = []

  for (const track of tracks) {
    let id: string | null
    try {
      id = pickBestMatch(track, await searchAudio(env, stripNoise(track.title)))
    }
    catch {
      id = null // per-track search failure → treat as a miss, keep going
    }
    if (id && !seen.has(id)) {
      seen.add(id)
      matchedIds.push(id)
    }
    else if (!id) {
      skipped.push({ title: track.title, artist: track.artist })
    }
  }

  if (matchedIds.length === 0)
    return { playlistName: name, total: tracks.length, matched: 0, skipped, jellyfinPlaylistId: null }

  const existing = await findPlaylistByName(env, name)
  if (existing)
    await deletePlaylist(env, existing.Id)
  const jellyfinPlaylistId = await createPlaylist(env, name, matchedIds)

  return { playlistName: name, total: tracks.length, matched: matchedIds.length, skipped, jellyfinPlaylistId }
}
