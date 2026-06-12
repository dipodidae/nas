// Spotify playlist source. Persistent single-user OAuth (Authorization Code)
// plus a pure playlist→albums transform. Pure helpers live alongside the network
// calls so they can be unit-tested without hitting Spotify (mirrors openai.ts).
import type { Env } from './env'
import type { ParsedItem, SpotifyResolveResult } from '~~/shared/types'

export const SPOTIFY_ACCOUNTS = 'https://accounts.spotify.com'
export const SPOTIFY_API = 'https://api.spotify.com/v1'
const SCOPES = 'playlist-read-private playlist-read-collaborative'
// Refresh slightly early so an in-flight request never races token expiry.
const EXPIRY_SKEW_MS = 60_000

export interface StoredToken {
  access_token: string
  refresh_token: string
  expires_at: number // epoch ms
}

export function spotifyEnabled(env: Env): boolean {
  return Boolean(env.SPOTIFY_API_CLIENT_ID && env.SPOTIFY_API_CLIENT_SECRET && env.SPOTIFY_REDIRECT_URI)
}

export function buildAuthorizeUrl(env: Env, state: string): string {
  const params = new URLSearchParams({
    client_id: env.SPOTIFY_API_CLIENT_ID,
    response_type: 'code',
    redirect_uri: env.SPOTIFY_REDIRECT_URI,
    scope: SCOPES,
    state,
  })
  return `${SPOTIFY_ACCOUNTS}/authorize?${params.toString()}`
}

export function needsRefresh(token: StoredToken, now: number): boolean {
  return now >= token.expires_at - EXPIRY_SKEW_MS
}

// Shapes of the slices of the Spotify API responses we read. Everything is
// `unknown`-guarded because we never trust the upstream payload.
interface ApiArtist { name?: unknown }
interface ApiAlbum { id?: unknown, name?: unknown, artists?: ApiArtist[] }
interface ApiTrack { type?: unknown, album?: ApiAlbum | null }
export interface PlaylistTrackItem { is_local?: unknown, track?: ApiTrack | null }

function cleanString(v: unknown): string {
  return typeof v === 'string' ? v.replace(/\s+/g, ' ').trim() : ''
}

// Collapse a playlist's tracks into unique albums (deduped by Spotify album id),
// shaped as the ParsedItems the album job pipeline already consumes. Local
// files, podcast episodes, and rows missing an album id / title / artist are
// counted as `skipped`; same-album duplicates are silently merged.
export function albumItemsFromTracks(items: PlaylistTrackItem[]): SpotifyResolveResult {
  const seen = new Set<string>()
  const out: ParsedItem[] = []
  let skipped = 0
  for (const it of items) {
    const album = it?.track?.album
    const albumId = typeof album?.id === 'string' ? album.id : ''
    const title = cleanString(album?.name)
    const artist = Array.isArray(album?.artists) ? cleanString(album?.artists[0]?.name) : ''
    const isLocal = it?.is_local === true
    const isEpisode = it?.track?.type === 'episode'
    if (isLocal || isEpisode || !albumId || !title || !artist) {
      skipped++
      continue
    }
    if (seen.has(albumId))
      continue
    seen.add(albumId)
    out.push({ raw: `${artist} - ${title}`, kind: 'album', artist, title })
  }
  return { items: out, stats: { tracks: items.length, skipped, uniqueAlbums: out.length } }
}
