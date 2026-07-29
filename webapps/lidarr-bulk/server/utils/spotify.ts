// Spotify playlist source. Persistent single-user OAuth (Authorization Code)
// plus a pure playlist→albums transform. Pure helpers live alongside the network
// calls so they can be unit-tested without hitting Spotify (mirrors openai.ts).
import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import type { Env } from './env'
import type { ParsedItem, SpotifyPlaylist, SpotifyResolveResult, SpotifyTrack } from '~~/shared/types'
import { loadEnv } from './env'
import { isVariousArtists } from './matching'

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
interface ApiAlbum { id?: unknown, name?: unknown, album_type?: unknown, release_date?: unknown, artists?: ApiArtist[] }
interface ApiTrack { type?: unknown, name?: unknown, artists?: ApiArtist[], album?: ApiAlbum | null }
export interface PlaylistTrackItem { is_local?: unknown, track?: ApiTrack | null }

function cleanString(v: unknown): string {
  return typeof v === 'string' ? v.replace(/\s+/g, ' ').trim() : ''
}

// Spotify release_date is 'YYYY', 'YYYY-MM', or 'YYYY-MM-DD'. Take the year only.
function releaseYear(v: unknown): number | undefined {
  if (typeof v !== 'string')
    return undefined
  const y = Number.parseInt(v.slice(0, 4), 10)
  return Number.isFinite(y) ? y : undefined
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
    const isComp = album?.album_type === 'compilation' || isVariousArtists(artist)
    const item: ParsedItem = { raw: `${artist} - ${title}`, kind: 'album', artist, title }
    const year = releaseYear(album?.release_date)
    if (year !== undefined)
      item.year = year
    if (isComp)
      item.variousArtists = true
    out.push(item)
  }
  return { items: out, stats: { tracks: items.length, skipped, uniqueAlbums: out.length } }
}

// Track-level view of a playlist: ordered title + primary artist + album, for
// matching against an external library. Skips local files, podcast episodes,
// and rows missing a title or primary artist. Order is preserved and duplicates
// are kept — a playlist may legitimately list a track twice; dedupe happens
// downstream by matched library id.
export function trackDetailsFromItems(items: PlaylistTrackItem[]): SpotifyTrack[] {
  const out: SpotifyTrack[] = []
  for (const it of items) {
    const tr = it?.track
    const title = cleanString(tr?.name)
    const artist = Array.isArray(tr?.artists) ? cleanString(tr?.artists[0]?.name) : ''
    const album = cleanString(tr?.album?.name)
    const isLocal = it?.is_local === true
    const isEpisode = tr?.type === 'episode'
    if (isLocal || isEpisode || !title || !artist)
      continue
    out.push({ title, artist, album: album || undefined })
  }
  return out
}

export async function fetchPlaylistTrackDetails(accessToken: string, playlistId: string): Promise<SpotifyTrack[]> {
  const encoded = encodeURIComponent(playlistId)
  const items = await fetchPagesByOffset<PlaylistTrackItem>(
    accessToken,
    `/playlists/${encoded}/tracks?limit=${PAGE_LIMIT}&fields=total,next,items(is_local,track(type,name,artists(name),album(name)))`,
  )
  return trackDetailsFromItems(items)
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

interface Page<T> { items: T[], next?: string | null, total?: number }

// How many playlist pages to request at once. Spotify's `next` cursor forces one
// round trip per 100 tracks — 19 of them, strictly sequential, for a 1842-track
// playlist. The endpoint also accepts `offset`, and the first response tells us
// `total`, so every page after the first can be fetched in parallel.
const PAGE_CONCURRENCY = 3
const PAGE_LIMIT = 100

// Spotify answers 429 with a Retry-After (seconds) and the limit is account-wide,
// so once tripped every later call fails too — including simply listing the user's
// playlists. Honour the header rather than retrying blind, and pause the whole
// module for that long so parallel page fetches back off together instead of each
// discovering the limit for itself.
const SPOTIFY_MAX_ATTEMPTS = 4
const SPOTIFY_MAX_BACKOFF_MS = 30_000
let spotifyPausedUntil = 0

const napt = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms))

function retryAfterMs(res: Response): number {
  const header = Number.parseInt(res.headers.get('retry-after') ?? '', 10)
  const ms = Number.isFinite(header) ? header * 1000 : 2000
  return Math.min(ms, SPOTIFY_MAX_BACKOFF_MS)
}

export async function spotifyFetch(url: string, accessToken: string): Promise<Response> {
  for (let attempt = 0; attempt < SPOTIFY_MAX_ATTEMPTS; attempt++) {
    const pause = spotifyPausedUntil - Date.now()
    if (pause > 0)
      await napt(pause)
    const res = await fetch(url, { headers: { Authorization: `Bearer ${accessToken}` } })
    if (res.status !== 429)
      return res
    const wait = retryAfterMs(res)
    spotifyPausedUntil = Math.max(spotifyPausedUntil, Date.now() + wait)
    console.warn(`[spotify] 429, backing off ${wait}ms (attempt ${attempt + 1}/${SPOTIFY_MAX_ATTEMPTS})`)
    if (attempt === SPOTIFY_MAX_ATTEMPTS - 1)
      return res
  }
  throw new Error('unreachable')
}

export function resetSpotifyBackoff(): void {
  spotifyPausedUntil = 0
}

async function getPage<T>(accessToken: string, url: string): Promise<Page<T>> {
  const res = await spotifyFetch(url, accessToken)
  if (!res.ok) {
    const body = await res.text()
    if (res.status === 429)
      throw new Error(`Spotify rate limit hit (429) — try again in a moment: ${body.slice(0, 120)}`)
    throw new Error(`Spotify API failed (${res.status}): ${body}`)
  }
  return await res.json() as Page<T>
}

// Offset-paged fetch: one request to learn `total`, then the rest concurrently in
// bounded waves. `path` must already contain `limit` and any `fields` selector and
// must NOT contain `offset`.
async function fetchPagesByOffset<T>(accessToken: string, path: string): Promise<T[]> {
  const base = path.startsWith('http') ? path : `${SPOTIFY_API}${path}`
  const withOffset = (offset: number): string =>
    `${base}${base.includes('?') ? '&' : '?'}offset=${offset}`

  const first = await getPage<T>(accessToken, withOffset(0))
  const out: T[] = [...(first.items ?? [])]
  const total = typeof first.total === 'number' ? first.total : out.length
  if (out.length === 0 || total <= out.length)
    return out

  const offsets: number[] = []
  for (let o = out.length; o < total; o += PAGE_LIMIT)
    offsets.push(o)

  // Ordered result slots so track order survives concurrent completion.
  const pages: T[][] = Array.from({ length: offsets.length }, () => [])
  for (let i = 0; i < offsets.length; i += PAGE_CONCURRENCY) {
    const wave = offsets.slice(i, i + PAGE_CONCURRENCY)
    await Promise.all(wave.map(async (offset, k) => {
      const page = await getPage<T>(accessToken, withOffset(offset))
      pages[i + k] = page.items ?? []
    }))
  }
  return out.concat(...pages)
}

// Follow Spotify's `next` cursor until exhausted, accumulating items. `first`
// is a path under SPOTIFY_API (e.g. '/me/playlists?limit=50'); subsequent pages
// use the absolute `next` URL Spotify returns.
async function fetchAllPages<T>(accessToken: string, first: string): Promise<T[]> {
  const out: T[] = []
  let url: string | null = first.startsWith('http') ? first : `${SPOTIFY_API}${first}`
  while (url) {
    const page: Page<T> = await getPage<T>(accessToken, url)
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
  return fetchPagesByOffset<PlaylistTrackItem>(
    accessToken,
    `/playlists/${encoded}/tracks?limit=${PAGE_LIMIT}&fields=total,next,items(is_local,track(type,album(id,name,album_type,release_date,artists(name))))`,
  )
}

// Map a Spotify /search playlist page into trimmed playlists. Spotify returns
// `null` entries for playlists it owns / has deprecated (since late 2024), and
// occasionally rows without an id — drop both before trimming so the UI never
// renders a phantom card or crashes on a missing id.
export function playlistsFromSearch(items: (RawPlaylist | null)[]): SpotifyPlaylist[] {
  return items
    .filter((p): p is RawPlaylist => p != null && typeof p.id === 'string')
    .map(trimPlaylist)
}

// Search ALL public playlists by keyword (not just the connected account's).
// Thin I/O wrapper around the pure mapper above; a blank query short-circuits
// so we never spend a request on it. Single page — `limit` results, no paging.
export async function searchPlaylists(accessToken: string, query: string, limit = 24): Promise<SpotifyPlaylist[]> {
  const q = query.trim()
  if (!q)
    return []
  const params = new URLSearchParams({ q, type: 'playlist', limit: String(limit) })
  const res = await spotifyFetch(`${SPOTIFY_API}/search?${params.toString()}`, accessToken)
  if (!res.ok) {
    const text = await res.text()
    throw new Error(res.status === 429
      ? `Spotify rate limit hit (429) — try again in a moment`
      : `Spotify API failed (${res.status}): ${text}`)
  }
  const body = await res.json() as { playlists?: { items?: (RawPlaylist | null)[] } }
  return playlistsFromSearch(body.playlists?.items ?? [])
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
