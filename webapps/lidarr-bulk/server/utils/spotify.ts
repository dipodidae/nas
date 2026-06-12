// Spotify playlist source. Persistent single-user OAuth (Authorization Code)
// plus a pure playlist→albums transform. Pure helpers live alongside the network
// calls so they can be unit-tested without hitting Spotify (mirrors openai.ts).
import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import type { Env } from './env'
import type { ParsedItem, SpotifyPlaylist, SpotifyResolveResult } from '~~/shared/types'
import { loadEnv } from './env'

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
    // Force the account/consent screen every time so the user can switch
    // accounts instead of being silently re-authorized as the last one.
    show_dialog: 'true',
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

function tokenPath(): string {
  return join(loadEnv().CONFIG_DIR, 'spotify-token.json')
}

function isNotFound(err: unknown): boolean {
  return typeof err === 'object' && err !== null && (err as { code?: string }).code === 'ENOENT'
}

export async function readToken(): Promise<StoredToken | null> {
  try {
    return JSON.parse(await readFile(tokenPath(), 'utf8')) as StoredToken
  }
  catch (err: unknown) {
    if (isNotFound(err))
      return null
    throw err
  }
}

export async function writeToken(token: StoredToken): Promise<void> {
  const path = tokenPath()
  await mkdir(dirname(path), { recursive: true })
  await writeFile(path, `${JSON.stringify(token, null, 2)}\n`, 'utf8')
}

export async function deleteToken(): Promise<void> {
  try {
    await unlink(tokenPath())
  }
  catch (err: unknown) {
    if (!isNotFound(err))
      throw err
  }
}

function basicAuth(env: Env): string {
  return Buffer.from(`${env.SPOTIFY_API_CLIENT_ID}:${env.SPOTIFY_API_CLIENT_SECRET}`).toString('base64')
}

interface TokenResponse {
  access_token: string
  refresh_token?: string
  expires_in: number
}

async function postToken(env: Env, body: Record<string, string>): Promise<TokenResponse> {
  const res = await fetch(`${SPOTIFY_ACCOUNTS}/api/token`, {
    method: 'POST',
    headers: {
      'Authorization': `Basic ${basicAuth(env)}`,
      'Content-Type': 'application/x-www-form-urlencoded',
    },
    body: new URLSearchParams(body).toString(),
  })
  if (!res.ok)
    throw new Error(`Spotify token request failed (${res.status}): ${await res.text()}`)
  return await res.json() as TokenResponse
}

// Exchange the callback `code` for tokens and persist them.
export async function exchangeCode(env: Env, code: string): Promise<void> {
  const t = await postToken(env, {
    grant_type: 'authorization_code',
    code,
    redirect_uri: env.SPOTIFY_REDIRECT_URI,
  })
  await writeToken({
    access_token: t.access_token,
    refresh_token: t.refresh_token ?? '',
    expires_at: Date.now() + t.expires_in * 1000,
  })
}

// Refresh using the stored refresh token. Spotify may omit a new refresh_token,
// in which case we keep the existing one.
async function refreshAccessToken(env: Env, current: StoredToken): Promise<StoredToken> {
  const t = await postToken(env, {
    grant_type: 'refresh_token',
    refresh_token: current.refresh_token,
  })
  const next: StoredToken = {
    access_token: t.access_token,
    refresh_token: t.refresh_token ?? current.refresh_token,
    expires_at: Date.now() + t.expires_in * 1000,
  }
  await writeToken(next)
  return next
}

// Return a valid access token, refreshing if needed. Returns null when not
// connected (no token stored). Throws if a refresh fails (revoked grant).
export async function getValidAccessToken(env: Env): Promise<string | null> {
  const token = await readToken()
  if (!token)
    return null
  if (!needsRefresh(token, Date.now()))
    return token.access_token
  const refreshed = await refreshAccessToken(env, token)
  return refreshed.access_token
}

interface Page<T> { items: T[], next?: string | null }

// Follow Spotify's `next` cursor until exhausted, accumulating items. `first`
// is a path under SPOTIFY_API (e.g. '/me/playlists?limit=50'); subsequent pages
// use the absolute `next` URL Spotify returns.
async function fetchAllPages<T>(accessToken: string, first: string): Promise<T[]> {
  const out: T[] = []
  let url: string | null = first.startsWith('http') ? first : `${SPOTIFY_API}${first}`
  while (url) {
    const res = await fetch(url, { headers: { Authorization: `Bearer ${accessToken}` } })
    if (!res.ok)
      throw new Error(`Spotify API failed (${res.status}): ${await res.text()}`)
    const page = await res.json() as Page<T>
    out.push(...(page.items ?? []))
    url = page.next ?? null
  }
  return out
}

interface RawPlaylist {
  id: string
  name: string
  owner?: { display_name?: string }
  images?: { url?: string }[]
  tracks?: { total?: number }
}

export async function fetchPlaylists(accessToken: string): Promise<RawPlaylist[]> {
  return fetchAllPages<RawPlaylist>(accessToken, '/me/playlists?limit=50')
}

export async function fetchPlaylistTracks(accessToken: string, playlistId: string): Promise<PlaylistTrackItem[]> {
  const encoded = encodeURIComponent(playlistId)
  return fetchAllPages<PlaylistTrackItem>(
    accessToken,
    `/playlists/${encoded}/tracks?limit=100&fields=next,items(is_local,track(type,album(id,name,artists(name))))`,
  )
}

export function trimPlaylist(p: RawPlaylist): SpotifyPlaylist {
  return {
    id: p.id,
    name: p.name,
    trackCount: p.tracks?.total ?? 0,
    imageUrl: p.images?.[0]?.url,
    owner: p.owner?.display_name,
  }
}
