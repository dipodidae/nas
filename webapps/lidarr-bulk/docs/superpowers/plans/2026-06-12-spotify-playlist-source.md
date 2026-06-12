# Spotify Playlist → Lidarr Album Source — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Spotify as an album source — connect once (persistent OAuth), pick a playlist, and one-click queue every unique album behind its tracks into Lidarr via the existing job pipeline.

**Architecture:** A server util `spotify.ts` owns OAuth (Authorization Code, single-user, refresh token persisted to `CONFIG_DIR/spotify-token.json`) and a pure playlist→albums transform. Thin endpoints under `server/api/spotify/` expose login/callback/status/disconnect/playlists/resolve. The resolver emits the existing `ParsedItem` shape (`{raw, kind:'album', artist, title}`), so a new `SpotifyPanel.vue` tab hands resolved albums straight to the existing `useJob().start()` → `createJob` machinery. No new matching code.

**Tech Stack:** Nuxt 4 / Nitro (h3), Zod, Vitest, Nuxt UI (`U*` components). Spotify Web API.

---

## File structure

- **Create** `server/utils/spotify.ts` — env helpers, authorize-URL builder, token storage/refresh, paginated fetch, and the pure `albumItemsFromTracks` transform.
- **Create** `server/api/spotify/status.get.ts`, `login.get.ts`, `callback.get.ts`, `disconnect.post.ts`, `playlists.get.ts`, `resolve.post.ts`.
- **Create** `app/components/SpotifyPanel.vue` — connect button / playlist grid / one-click queue.
- **Create** `tests/spotify.test.ts` — unit tests for the pure helpers.
- **Modify** `server/utils/env.ts` — add `SPOTIFY_API_CLIENT_ID`, `SPOTIFY_API_CLIENT_SECRET`, `SPOTIFY_REDIRECT_URI`.
- **Modify** `shared/types.ts` — add `SpotifyStatus`, `SpotifyPlaylist`, `SpotifyResolveStats`, `SpotifyResolveResult`.
- **Modify** `app/pages/index.vue` — add the Spotify tab (when enabled) and a one-click queue handler.
- **Modify** `.env.example` and `README.md` — document the new env vars and the redirect-URI setup step.

**Network functions are not unit-tested** (mirrors the existing `openai.ts` convention, where only pure shaping helpers are tested). Tests target the pure transform and pure helpers.

---

## Task 1: Env vars, shared types, and docs

**Files:**
- Modify: `server/utils/env.ts`
- Modify: `shared/types.ts`
- Modify: `.env.example`
- Modify: `README.md`

- [ ] **Step 1: Add Spotify env vars to the schema**

In `server/utils/env.ts`, add these three fields to the `z.object({ … })` schema, right after the `OPENAI_MODEL` line:

```ts
  // Optional — enables the Spotify "Spotify" tab. All three required to enable;
  // when any is unset the tab is hidden and the endpoints report disabled.
  SPOTIFY_API_CLIENT_ID: z.string().optional().default(''),
  SPOTIFY_API_CLIENT_SECRET: z.string().optional().default(''),
  // Must exactly match a Redirect URI registered in the Spotify dashboard, e.g.
  // https://lidarr-bulk.example.com/api/spotify/callback
  SPOTIFY_REDIRECT_URI: z.string().optional().default(''),
```

- [ ] **Step 2: Add shared types**

Append to `shared/types.ts`:

```ts
export interface SpotifyStatus {
  enabled: boolean
  connected: boolean
}

export interface SpotifyPlaylist {
  id: string
  name: string
  trackCount: number
  imageUrl?: string
  owner?: string
}

export interface SpotifyResolveStats {
  tracks: number
  skipped: number
  uniqueAlbums: number
}

export interface SpotifyResolveResult {
  items: ParsedItem[]
  stats: SpotifyResolveStats
}
```

- [ ] **Step 3: Document env vars in `.env.example`**

Append to `.env.example`:

```bash

# Optional — enables the "Spotify" tab (queue albums from your playlists).
# Create an app at https://developer.spotify.com/dashboard, then register the
# redirect URI below (must match SPOTIFY_REDIRECT_URI exactly). All three are
# required to enable the tab.
SPOTIFY_API_CLIENT_ID=
SPOTIFY_API_CLIENT_SECRET=
SPOTIFY_REDIRECT_URI=https://lidarr-bulk.example.com/api/spotify/callback
```

- [ ] **Step 4: Document the feature in `README.md`**

Add a short subsection under the existing feature/usage docs (place it after the AI "Discover" description; if no obvious anchor, add it near the other optional-env descriptions):

```markdown
### Spotify playlists

Set `SPOTIFY_API_CLIENT_ID`, `SPOTIFY_API_CLIENT_SECRET`, and
`SPOTIFY_REDIRECT_URI` to enable the **Spotify** tab. Register the redirect URI
(`https://<your-host>/api/spotify/callback`) in your Spotify app dashboard so it
matches `SPOTIFY_REDIRECT_URI` exactly. Click **Connect Spotify** once to
authorize; the refresh token is stored in `CONFIG_DIR/spotify-token.json`. Then
click any playlist to queue every unique album behind its tracks into Lidarr
using your saved default profiles and monitor mode.
```

- [ ] **Step 5: Verify it typechecks and commit**

Run: `pnpm lint`
Expected: PASS (no new lint errors in the edited files).

```bash
git add server/utils/env.ts shared/types.ts .env.example README.md
git commit -m "feat(spotify): env vars, shared types, and docs for playlist source"
```

---

## Task 2: Pure helpers in `spotify.ts` (TDD)

**Files:**
- Create: `server/utils/spotify.ts`
- Test: `tests/spotify.test.ts`

- [ ] **Step 1: Write the failing tests**

Create `tests/spotify.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import {
  albumItemsFromTracks,
  buildAuthorizeUrl,
  needsRefresh,
  spotifyEnabled,
} from '../server/utils/spotify'

const ENV = {
  SPOTIFY_API_CLIENT_ID: 'cid',
  SPOTIFY_API_CLIENT_SECRET: 'secret',
  SPOTIFY_REDIRECT_URI: 'https://app.example/api/spotify/callback',
} as never

describe('spotifyEnabled', () => {
  it('true only when all three vars are set', () => {
    expect(spotifyEnabled(ENV)).toBe(true)
    expect(spotifyEnabled({ ...ENV, SPOTIFY_API_CLIENT_SECRET: '' } as never)).toBe(false)
    expect(spotifyEnabled({ ...ENV, SPOTIFY_REDIRECT_URI: '' } as never)).toBe(false)
  })
})

describe('buildAuthorizeUrl', () => {
  it('encodes client_id, redirect_uri, scope and state', () => {
    const url = new URL(buildAuthorizeUrl(ENV, 'xyz'))
    expect(url.origin + url.pathname).toBe('https://accounts.spotify.com/authorize')
    expect(url.searchParams.get('client_id')).toBe('cid')
    expect(url.searchParams.get('response_type')).toBe('code')
    expect(url.searchParams.get('redirect_uri')).toBe('https://app.example/api/spotify/callback')
    expect(url.searchParams.get('state')).toBe('xyz')
    expect(url.searchParams.get('scope')).toContain('playlist-read-private')
  })
})

describe('needsRefresh', () => {
  it('true within the 60s skew window, false outside', () => {
    expect(needsRefresh({ access_token: 'a', refresh_token: 'r', expires_at: 100_000 }, 50_000)).toBe(false)
    expect(needsRefresh({ access_token: 'a', refresh_token: 'r', expires_at: 100_000 }, 60_000)).toBe(true) // 100000-60000 skew
    expect(needsRefresh({ access_token: 'a', refresh_token: 'r', expires_at: 100_000 }, 99_000)).toBe(true)
  })
})

describe('albumItemsFromTracks', () => {
  const track = (id: string, name: string, artist: string) => ({
    is_local: false,
    track: { type: 'track', album: { id, name, artists: [{ name: artist }] } },
  })

  it('dedupes by album id and shapes ParsedItems', () => {
    const res = albumItemsFromTracks([
      track('a1', 'Faith', 'The Cure'),
      track('a1', 'Faith', 'The Cure'), // same album, different track
      track('a2', 'Medusa', 'Clan of Xymox'),
    ])
    expect(res.items).toEqual([
      { raw: 'The Cure - Faith', kind: 'album', artist: 'The Cure', title: 'Faith' },
      { raw: 'Clan of Xymox - Medusa', kind: 'album', artist: 'Clan of Xymox', title: 'Medusa' },
    ])
    expect(res.stats).toEqual({ tracks: 3, skipped: 0, uniqueAlbums: 2 })
  })

  it('skips local files, episodes, and missing album/artist; collapses whitespace', () => {
    const res = albumItemsFromTracks([
      { is_local: true, track: { type: 'track', album: { id: 'x', name: 'Local', artists: [{ name: 'Me' }] } } },
      { is_local: false, track: { type: 'episode', album: { id: 'e', name: 'Pod', artists: [{ name: 'Host' }] } } },
      { is_local: false, track: null },
      { is_local: false, track: { type: 'track', album: { id: 'n', name: '', artists: [{ name: 'A' }] } } },
      { is_local: false, track: { type: 'track', album: { id: 'm', name: 'Disint  egration', artists: [{ name: '  The   Cure ' }] } } },
    ])
    expect(res.items).toEqual([
      { raw: 'The Cure - Disint egration', kind: 'album', artist: 'The Cure', title: 'Disint egration' },
    ])
    expect(res.stats).toEqual({ tracks: 5, skipped: 4, uniqueAlbums: 1 })
  })

  it('returns empty result for an empty playlist', () => {
    expect(albumItemsFromTracks([])).toEqual({ items: [], stats: { tracks: 0, skipped: 0, uniqueAlbums: 0 } })
  })
})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pnpm vitest run tests/spotify.test.ts`
Expected: FAIL — cannot resolve `../server/utils/spotify` (module not created yet).

- [ ] **Step 3: Implement the pure helpers**

Create `server/utils/spotify.ts` with exactly these exports (network/storage functions are added in Task 3; this step only adds the pure pieces and shared constants/types):

```ts
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pnpm vitest run tests/spotify.test.ts`
Expected: PASS (all describe blocks green).

- [ ] **Step 5: Commit**

```bash
git add server/utils/spotify.ts tests/spotify.test.ts
git commit -m "feat(spotify): pure authorize-url + playlist→albums transform with tests"
```

---

## Task 3: Token storage + network calls in `spotify.ts`

**Files:**
- Modify: `server/utils/spotify.ts`

No new unit tests (network/IO, mirrors `openai.ts`). Verified via typecheck + the live flow.

- [ ] **Step 1: Add imports for storage + crypto**

At the top of `server/utils/spotify.ts`, add to the existing imports:

```ts
import { mkdir, readFile, unlink, writeFile } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { loadEnv } from './env'
```

- [ ] **Step 2: Add token persistence helpers**

Append to `server/utils/spotify.ts`:

```ts
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
```

- [ ] **Step 3: Add the token-endpoint calls (exchange + refresh)**

Append to `server/utils/spotify.ts`:

```ts
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
```

- [ ] **Step 4: Add the paginated Web API fetch + playlist/track fetchers**

Append to `server/utils/spotify.ts`:

```ts
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

export function trimPlaylist(p: RawPlaylist): import('~~/shared/types').SpotifyPlaylist {
  return {
    id: p.id,
    name: p.name,
    trackCount: p.tracks?.total ?? 0,
    imageUrl: p.images?.[0]?.url,
    owner: p.owner?.display_name,
  }
}
```

- [ ] **Step 5: Verify lint + existing tests still pass, then commit**

Run: `pnpm lint && pnpm vitest run tests/spotify.test.ts`
Expected: PASS — no lint errors, pure-helper tests still green.

```bash
git add server/utils/spotify.ts
git commit -m "feat(spotify): token storage, refresh, and paginated Web API fetchers"
```

---

## Task 4: Auth endpoints (status, login, callback, disconnect)

**Files:**
- Create: `server/api/spotify/status.get.ts`
- Create: `server/api/spotify/login.get.ts`
- Create: `server/api/spotify/callback.get.ts`
- Create: `server/api/spotify/disconnect.post.ts`

- [ ] **Step 1: Create the status endpoint**

Create `server/api/spotify/status.get.ts`:

```ts
import type { SpotifyStatus } from '~~/shared/types'
import { defineEventHandler } from 'h3'
import { loadEnv } from '../../utils/env'
import { readToken, spotifyEnabled } from '../../utils/spotify'

// Lets the UI decide whether to show the Spotify tab and whether to prompt for
// connect — without leaking secrets. `connected` reflects a stored token;
// validity is enforced lazily on use (refresh / re-connect).
export default defineEventHandler(async (): Promise<SpotifyStatus> => {
  const env = loadEnv()
  const enabled = spotifyEnabled(env)
  const connected = enabled ? (await readToken()) !== null : false
  return { enabled, connected }
})
```

- [ ] **Step 2: Create the login (authorize redirect) endpoint**

Create `server/api/spotify/login.get.ts`:

```ts
import { randomUUID } from 'node:crypto'
import { createError, defineEventHandler, sendRedirect, setCookie } from 'h3'
import { loadEnv } from '../../utils/env'
import { buildAuthorizeUrl, spotifyEnabled } from '../../utils/spotify'

export default defineEventHandler((event) => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })
  const state = randomUUID()
  // Short-lived CSRF guard echoed back by Spotify and checked in the callback.
  setCookie(event, 'spotify_oauth_state', state, {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: 600,
  })
  return sendRedirect(event, buildAuthorizeUrl(env, state))
})
```

- [ ] **Step 3: Create the callback endpoint**

Create `server/api/spotify/callback.get.ts`:

```ts
import { defineEventHandler, getCookie, getQuery, sendRedirect, setCookie } from 'h3'
import { loadEnv } from '../../utils/env'
import { exchangeCode, spotifyEnabled } from '../../utils/spotify'

// Spotify redirects the browser here after consent. We verify the state cookie,
// exchange the code for tokens, then bounce back to the app with a status flag
// the UI can surface as a toast.
export default defineEventHandler(async (event) => {
  const env = loadEnv()
  const query = getQuery(event)
  const code = typeof query.code === 'string' ? query.code : ''
  const state = typeof query.state === 'string' ? query.state : ''
  const expected = getCookie(event, 'spotify_oauth_state')
  // Clear the one-shot state cookie regardless of outcome.
  setCookie(event, 'spotify_oauth_state', '', { path: '/', maxAge: 0 })

  if (!spotifyEnabled(env) || query.error || !code || !state || state !== expected)
    return sendRedirect(event, '/?spotify=error')

  try {
    await exchangeCode(env, code)
    return sendRedirect(event, '/?spotify=connected')
  }
  catch {
    return sendRedirect(event, '/?spotify=error')
  }
})
```

- [ ] **Step 4: Create the disconnect endpoint**

Create `server/api/spotify/disconnect.post.ts`:

```ts
import { defineEventHandler } from 'h3'
import { deleteToken } from '../../utils/spotify'

export default defineEventHandler(async () => {
  await deleteToken()
  return { ok: true }
})
```

- [ ] **Step 5: Verify lint + typecheck, then commit**

Run: `pnpm lint`
Expected: PASS.

```bash
git add server/api/spotify/status.get.ts server/api/spotify/login.get.ts server/api/spotify/callback.get.ts server/api/spotify/disconnect.post.ts
git commit -m "feat(spotify): status, login, callback, and disconnect endpoints"
```

---

## Task 5: Data endpoints (playlists, resolve)

**Files:**
- Create: `server/api/spotify/playlists.get.ts`
- Create: `server/api/spotify/resolve.post.ts`

- [ ] **Step 1: Create the playlists endpoint**

Create `server/api/spotify/playlists.get.ts`:

```ts
import type { SpotifyPlaylist } from '~~/shared/types'
import { createError, defineEventHandler } from 'h3'
import { loadEnv } from '../../utils/env'
import { fetchPlaylists, getValidAccessToken, spotifyEnabled, trimPlaylist } from '../../utils/spotify'

export default defineEventHandler(async (): Promise<{ playlists: SpotifyPlaylist[] }> => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })
  let token: string | null
  try {
    token = await getValidAccessToken(env)
  }
  catch {
    // Refresh failed (revoked grant) — surface as "not connected".
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })
  }
  if (!token)
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })
  try {
    const raw = await fetchPlaylists(token)
    return { playlists: raw.map(trimPlaylist) }
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    throw createError({ statusCode: 502, statusMessage: `Spotify playlist fetch failed: ${msg}` })
  }
})
```

- [ ] **Step 2: Create the resolve endpoint**

Create `server/api/spotify/resolve.post.ts`:

```ts
import type { SpotifyResolveResult } from '~~/shared/types'
import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { loadEnv } from '../../utils/env'
import { albumItemsFromTracks, fetchPlaylistTracks, getValidAccessToken, spotifyEnabled } from '../../utils/spotify'

const schema = z.object({ playlistId: z.string().min(1) })

export default defineEventHandler(async (event): Promise<SpotifyResolveResult> => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })

  const parsed = schema.safeParse(await readBody(event))
  if (!parsed.success)
    throw createError({ statusCode: 400, statusMessage: parsed.error.message })

  let token: string | null
  try {
    token = await getValidAccessToken(env)
  }
  catch {
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })
  }
  if (!token)
    throw createError({ statusCode: 401, statusMessage: 'Spotify not connected.' })

  try {
    const tracks = await fetchPlaylistTracks(token, parsed.data.playlistId)
    return albumItemsFromTracks(tracks)
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    throw createError({ statusCode: 502, statusMessage: `Spotify resolve failed: ${msg}` })
  }
})
```

- [ ] **Step 3: Verify lint, then commit**

Run: `pnpm lint`
Expected: PASS.

```bash
git add server/api/spotify/playlists.get.ts server/api/spotify/resolve.post.ts
git commit -m "feat(spotify): playlists and resolve endpoints"
```

---

## Task 6: `SpotifyPanel.vue` component

**Files:**
- Create: `app/components/SpotifyPanel.vue`

This panel owns connect/disconnect and the playlist grid. Clicking a playlist resolves it and emits `queue` with the resolved `ParsedItem[]`; `index.vue` (Task 7) owns the job. Mirrors `AiDiscoverPanel.vue` conventions (`useToast`, `$fetch`, error extraction).

- [ ] **Step 1: Create the component**

Create `app/components/SpotifyPanel.vue`:

```vue
<script setup lang="ts">
import type { ParsedItem, SpotifyPlaylist, SpotifyResolveResult, SpotifyStatus } from '~~/shared/types'

const emit = defineEmits<{ queue: [items: ParsedItem[]] }>()
const toast = useToast()

const connected = ref(false)
const playlists = ref<SpotifyPlaylist[]>([])
const loading = ref(false)
const resolvingId = ref<string | null>(null)

function describeError(err: unknown): string {
  const e = err as { statusMessage?: string, data?: { statusMessage?: string }, message?: string }
  return e.data?.statusMessage ?? e.statusMessage ?? e.message ?? 'Spotify request failed.'
}

async function loadPlaylists(): Promise<void> {
  loading.value = true
  try {
    const res = await $fetch<{ playlists: SpotifyPlaylist[] }>('/api/spotify/playlists')
    playlists.value = res.playlists
  }
  catch (err: unknown) {
    // 401 → token revoked/expired; fall back to the connect prompt.
    connected.value = false
    toast.add({ title: 'Spotify', description: describeError(err), color: 'warning' })
  }
  finally {
    loading.value = false
  }
}

onMounted(async () => {
  try {
    const s = await $fetch<SpotifyStatus>('/api/spotify/status')
    connected.value = s.connected
  }
  catch {
    connected.value = false
  }
  // Surface the OAuth callback outcome carried back as a query flag.
  const flag = useRoute().query.spotify
  if (flag === 'connected') {
    connected.value = true
    toast.add({ title: 'Spotify connected', color: 'success' })
  }
  else if (flag === 'error') {
    toast.add({ title: 'Spotify connection failed', description: 'Authorization was cancelled or failed.', color: 'error' })
  }
  if (connected.value)
    await loadPlaylists()
})

async function disconnect(): Promise<void> {
  await $fetch('/api/spotify/disconnect', { method: 'POST' })
  connected.value = false
  playlists.value = []
  toast.add({ title: 'Spotify disconnected', color: 'neutral' })
}

async function pick(playlist: SpotifyPlaylist): Promise<void> {
  if (resolvingId.value)
    return
  resolvingId.value = playlist.id
  try {
    const res = await $fetch<SpotifyResolveResult>('/api/spotify/resolve', {
      method: 'POST',
      body: { playlistId: playlist.id },
    })
    if (res.items.length === 0) {
      toast.add({ title: 'No albums', description: 'This playlist has no resolvable albums (local files / episodes only).', color: 'warning' })
      return
    }
    toast.add({
      title: `Queuing ${res.items.length} album${res.items.length === 1 ? '' : 's'}`,
      description: `from ${res.stats.tracks} tracks in “${playlist.name}”`,
      color: 'success',
    })
    emit('queue', res.items)
  }
  catch (err: unknown) {
    toast.add({ title: 'Resolve failed', description: describeError(err), color: 'error' })
  }
  finally {
    resolvingId.value = null
  }
}
</script>

<template>
  <UCard>
    <template v-if="!connected">
      <p class="text-muted mt-0 text-sm">
        Connect your Spotify account, then click a playlist to queue every unique album behind its tracks into Lidarr.
      </p>
      <UButton class="mt-3" icon="i-lucide-music" label="Connect Spotify" to="/api/spotify/login" external />
    </template>

    <template v-else>
      <div class="flex items-center justify-between gap-4 flex-wrap">
        <p class="text-muted m-0 text-sm">
          Click a playlist to queue its unique albums using your saved default profiles and monitor mode.
        </p>
        <UButton size="xs" color="neutral" variant="link" label="Disconnect" @click="disconnect" />
      </div>

      <div v-if="loading" class="mt-4 text-sm text-muted">
        Loading playlists…
      </div>
      <div v-else-if="playlists.length === 0" class="mt-4 text-sm text-muted">
        No playlists found on your account.
      </div>
      <div v-else class="mt-4 grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
        <button
          v-for="p in playlists"
          :key="p.id"
          type="button"
          :disabled="resolvingId !== null"
          class="text-left rounded-lg border border-default p-2 hover:bg-elevated transition disabled:opacity-50"
          @click="pick(p)"
        >
          <img v-if="p.imageUrl" :src="p.imageUrl" :alt="p.name" class="w-full aspect-square object-cover rounded-md mb-2">
          <div v-else class="w-full aspect-square rounded-md mb-2 bg-elevated flex items-center justify-center">
            <UIcon name="i-lucide-music" class="text-2xl text-muted" />
          </div>
          <div class="text-sm font-medium truncate">
            {{ p.name }}
          </div>
          <div class="text-xs text-muted">
            <template v-if="resolvingId === p.id">resolving…</template>
            <template v-else>{{ p.trackCount }} track{{ p.trackCount === 1 ? '' : 's' }}</template>
          </div>
        </button>
      </div>
    </template>
  </UCard>
</template>
```

- [ ] **Step 2: Verify lint, then commit**

Run: `pnpm lint`
Expected: PASS.

```bash
git add app/components/SpotifyPanel.vue
git commit -m "feat(spotify): SpotifyPanel — connect, playlist grid, one-click resolve"
```

---

## Task 7: Wire the Spotify tab into `index.vue`

**Files:**
- Modify: `app/pages/index.vue`

- [ ] **Step 1: Add the tab type, status fetch, computed tab list, and queue handler**

In `app/pages/index.vue` `<script setup>`, make these changes:

1. Widen the `Tab` type:

```ts
type Tab = 'artist' | 'album' | 'ai' | 'spotify'
```

2. The `kind` computed must still resolve to an album-producing kind for both `ai` and `spotify`. Replace the existing `kind` computed with:

```ts
// The AI and Spotify tabs both produce album rows, so they drive an album job.
const kind = computed<Kind>(() => (tab.value === 'artist' ? 'artist' : 'album'))
```

3. Fetch Spotify status and the saved monitor mode, and build the tab list as a computed that appends the Spotify tab only when enabled. Replace the static `tabItems` array with:

```ts
const spotifyEnabled = ref(false)
const savedMonitorMode = ref<'all' | 'future'>('all')

onMounted(async () => {
  try {
    const s = await $fetch<{ enabled: boolean }>('/api/spotify/status')
    spotifyEnabled.value = s.enabled
  }
  catch {
    spotifyEnabled.value = false
  }
  try {
    const settings = await $fetch<{ monitorMode: 'all' | 'future' }>('/api/settings')
    savedMonitorMode.value = settings.monitorMode
  }
  catch {
    savedMonitorMode.value = 'all'
  }
})

const tabItems = computed(() => {
  const base = [
    { label: 'Artists', value: 'artist', icon: 'i-lucide-mic-vocal' },
    { label: 'Albums', value: 'album', icon: 'i-lucide-disc-3' },
    { label: 'Discover ✨', value: 'ai', icon: 'i-lucide-sparkles' },
  ]
  if (spotifyEnabled.value)
    base.push({ label: 'Spotify', value: 'spotify', icon: 'i-lucide-music' })
  return base
})
```

4. Add a one-click queue handler that starts an album job with saved defaults (server fills profiles/root folder from `settings.json`; we pass the saved monitor mode):

```ts
async function onSpotifyQueue(items: ParsedItem[]): Promise<void> {
  deduped.value = items.length
  await start('album', items, savedMonitorMode.value)
}
```

- [ ] **Step 2: Add the panel to the template**

In the `<template>`, add the Spotify panel alongside the AI panel (immediately after the `<AiDiscoverPanel … />` line):

```vue
    <SpotifyPanel v-if="tab === 'spotify'" @queue="onSpotifyQueue" />
```

And ensure the `BulkAddForm` is hidden on the Spotify tab (it has no paste box). Change the `BulkAddForm` opening tag to add a `v-if`:

```vue
    <BulkAddForm
      v-if="tab !== 'spotify'"
      v-model:blob="blob"
      :kind="kind"
      :job-in-flight="jobInFlight"
      @start="onStart"
    >
```

- [ ] **Step 3: Verify lint + typecheck, then commit**

Run: `pnpm lint`
Expected: PASS.

```bash
git add app/pages/index.vue
git commit -m "feat(spotify): wire Spotify tab + one-click queue into index page"
```

---

## Task 8: Full verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full local CI-equivalent gates**

Run: `pnpm lint && pnpm vitest run`
Expected: PASS — all lint clean, all tests (including `tests/spotify.test.ts`) green.

- [ ] **Step 2: Typecheck via build**

Run: `pnpm build` (or the project's typecheck script if one exists — check `package.json` `scripts`)
Expected: Build/typecheck succeeds with no type errors.

- [ ] **Step 3: Manual smoke (requires real Spotify creds + redirect URI registered)**

With `SPOTIFY_API_CLIENT_ID`/`SECRET`/`REDIRECT_URI` set and the redirect URI registered in the Spotify dashboard:

1. `pnpm dev`, open the app — confirm a **Spotify** tab appears.
2. Click **Connect Spotify** → authorize → land back with a "Spotify connected" toast and a playlist grid.
3. Click a playlist → confirm a "Queuing N albums" toast and the `JobMonitor` shows the albums matching/adding in Lidarr.
4. Click **Disconnect** → confirm the grid is replaced by the Connect button.

- [ ] **Step 4: Final confirmation**

Confirm `git status` is clean (all work committed) and summarize the result.

---

## Self-review notes

- **Spec coverage:** auth (Task 3/4), token persistence to `CONFIG_DIR` (Task 3), playlist listing (Task 5), dedup-by-album resolve transform (Task 2), one-click queue via existing pipeline (Task 7), status gating like AI (Task 1/7), error handling — disabled/expired/upstream/empty (Tasks 4–6), tests for pure transform + helpers (Task 2), docs (Task 1). All covered.
- **Type consistency:** `SpotifyStatus`/`SpotifyPlaylist`/`SpotifyResolveResult`/`SpotifyResolveStats` defined once in Task 1 and used unchanged in Tasks 3–7. `StoredToken`, `PlaylistTrackItem`, `albumItemsFromTracks`, `getValidAccessToken`, `fetchPlaylists`, `fetchPlaylistTracks`, `trimPlaylist`, `exchangeCode`, `deleteToken`, `readToken` defined in Tasks 2–3 and consumed with matching signatures in Tasks 4–5.
- **No placeholders:** every code step contains complete code; every run step states the exact command and expected outcome.
