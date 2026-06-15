# Spotify → Recreate-in-Jellyfin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-playlist "Recreate in Jellyfin" action that matches a Spotify playlist's tracks against the Jellyfin music library (title + artist, fuzzy), replaces any same-name Jellyfin playlist, and reports matched/skipped counts in a modal.

**Architecture:** A new `server/utils/jellyfin.ts` (pure matching helpers reusing `matching.ts` normalization + thin Jellyfin REST calls), a track-level Spotify fetcher added to `spotify.ts`, an orchestration endpoint `server/api/spotify/to-jellyfin.post.ts`, a `jellyfin` capability flag added to `/api/spotify/status`, and a second action + summary modal in `SpotifyPanel.vue`.

**Tech Stack:** Nuxt 4 / Nitro (h3), TypeScript, Zod, Vitest, @nuxt/ui. Jellyfin REST (`X-Emby-Token`).

---

## File Structure

- Create: `server/utils/jellyfin.ts` — Jellyfin enabled-check, pure matching (`stripFeat`, `pickBestMatch`), network (`searchAudio`, `findPlaylistByName`, `deletePlaylist`, `createPlaylist`), orchestration (`recreatePlaylistInJellyfin`).
- Create: `server/api/spotify/to-jellyfin.post.ts` — endpoint wiring Spotify track fetch → Jellyfin recreate.
- Create: `tests/jellyfin.test.ts` — unit tests for `jellyfinEnabled`, `stripFeat`, `pickBestMatch`.
- Modify: `server/utils/spotify.ts` — add `trackDetailsFromItems` (pure) + `fetchPlaylistTrackDetails` (network).
- Modify: `tests/spotify.test.ts` — add `trackDetailsFromItems` tests.
- Modify: `shared/types.ts` — add `SpotifyTrack`, `JellyfinPushResult`; extend `SpotifyStatus` with `jellyfin: boolean`.
- Modify: `server/utils/env.ts` — add `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_USER_ID`.
- Modify: `server/api/spotify/status.get.ts` — report `jellyfin` capability.
- Modify: `app/components/SpotifyPanel.vue` — second action + summary modal.
- Modify: `.env.example`, `README.md` — document the three Jellyfin vars.
- Modify: `/home/tom/nas/docker-compose.yml` — pass the three vars into `lidarr-bulk`.

---

## Task 1: Env vars for Jellyfin

**Files:**
- Modify: `server/utils/env.ts`

- [ ] **Step 1: Add the three optional vars to the zod schema**

In `server/utils/env.ts`, after the Spotify block (`SPOTIFY_REDIRECT_URI`), add:

```ts
  // Optional — enables the Spotify "Recreate in Jellyfin" action. All three
  // required to enable; when any is unset the button is hidden and the endpoint
  // reports disabled. Reuses the nas stack's existing Jellyfin API key + user id.
  JELLYFIN_URL: z.string().optional().default(''),
  JELLYFIN_API_KEY: z.string().optional().default(''),
  JELLYFIN_USER_ID: z.string().optional().default(''),
```

- [ ] **Step 2: Typecheck**

Run: `pnpm typecheck`
Expected: PASS (no new errors).

- [ ] **Step 3: Commit**

```bash
git add server/utils/env.ts
git commit -m "feat(jellyfin): add JELLYFIN_URL/API_KEY/USER_ID env vars"
```

---

## Task 2: Shared types

**Files:**
- Modify: `shared/types.ts`

- [ ] **Step 1: Add the new types and extend SpotifyStatus**

Append to `shared/types.ts`:

```ts
export interface SpotifyTrack {
  title: string
  artist: string
  album?: string
}

export interface JellyfinPushResult {
  playlistName: string
  total: number // tracks considered after local/episode filtering
  matched: number // unique Jellyfin items added
  skipped: { title: string, artist: string }[]
  jellyfinPlaylistId: string | null // null when zero matches (nothing created)
}
```

Then change the existing `SpotifyStatus` interface to add the capability flag:

```ts
export interface SpotifyStatus {
  enabled: boolean
  connected: boolean
  jellyfin: boolean
}
```

- [ ] **Step 2: Typecheck**

Run: `pnpm typecheck`
Expected: FAIL — `status.get.ts` no longer returns `jellyfin`. That is fixed in Task 6. (If you want a clean checkpoint, do Task 6 before committing; otherwise commit the type now and accept the transient error.)

- [ ] **Step 3: Commit**

```bash
git add shared/types.ts
git commit -m "feat(jellyfin): add SpotifyTrack + JellyfinPushResult types, jellyfin status flag"
```

---

## Task 3: Spotify track-level fetch (pure transform first)

**Files:**
- Modify: `server/utils/spotify.ts`
- Test: `tests/spotify.test.ts`

- [ ] **Step 1: Write the failing test**

Add to `tests/spotify.test.ts` (and add `trackDetailsFromItems` to the import from `../server/utils/spotify`):

```ts
describe('trackDetailsFromItems', () => {
  const t = (name: string, artist: string, album = 'Some Album', extra = {}) => ({
    is_local: false,
    track: { type: 'track', name, artists: [{ name: artist }], album: { name: album }, ...extra },
  })

  it('maps name + primary artist + album, preserving order and duplicates', () => {
    const out = trackDetailsFromItems([
      t('A Forest', 'The Cure'),
      t('A Forest', 'The Cure'), // duplicate kept — dedupe happens later by Jellyfin id
      t('Stranger', 'Clan of Xymox', 'Medusa'),
    ])
    expect(out).toEqual([
      { title: 'A Forest', artist: 'The Cure', album: 'Some Album' },
      { title: 'A Forest', artist: 'The Cure', album: 'Some Album' },
      { title: 'Stranger', artist: 'Clan of Xymox', album: 'Medusa' },
    ])
  })

  it('skips local files, episodes, and rows missing a title or artist', () => {
    const out = trackDetailsFromItems([
      { is_local: true, track: { type: 'track', name: 'Local', artists: [{ name: 'X' }], album: { name: 'Y' } } },
      { is_local: false, track: { type: 'episode', name: 'Pod', artists: [{ name: 'X' }], album: { name: 'Y' } } },
      { is_local: false, track: { type: 'track', name: '', artists: [{ name: 'X' }], album: { name: 'Y' } } },
      { is_local: false, track: { type: 'track', name: 'NoArtist', artists: [], album: { name: 'Y' } } },
      t('Keep', 'Keeper'),
    ])
    expect(out).toEqual([{ title: 'Keep', artist: 'Keeper', album: 'Some Album' }])
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- tests/spotify.test.ts`
Expected: FAIL — `trackDetailsFromItems is not exported` / not defined.

- [ ] **Step 3: Implement the pure transform + network fetcher**

In `server/utils/spotify.ts`, extend the `ApiTrack` interface to include the fields we now read, then add the two functions. Replace the `ApiTrack` interface:

```ts
interface ApiTrack { type?: unknown, name?: unknown, artists?: ApiArtist[], album?: ApiAlbum | null }
```

Add after `albumItemsFromTracks`:

```ts
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
  const items = await fetchAllPages<PlaylistTrackItem>(
    accessToken,
    `/playlists/${encoded}/tracks?limit=100&fields=next,items(is_local,track(type,name,artists(name),album(name)))`,
  )
  return trackDetailsFromItems(items)
}
```

Add `SpotifyTrack` to the existing type import at the top of the file:

```ts
import type { ParsedItem, SpotifyPlaylist, SpotifyResolveResult, SpotifyTrack } from '~~/shared/types'
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm test -- tests/spotify.test.ts`
Expected: PASS (all spotify tests, old + new).

- [ ] **Step 5: Commit**

```bash
git add server/utils/spotify.ts tests/spotify.test.ts
git commit -m "feat(spotify): fetch track-level details (title/artist/album)"
```

---

## Task 4: Jellyfin util — pure matching helpers (TDD)

**Files:**
- Create: `server/utils/jellyfin.ts`
- Test: `tests/jellyfin.test.ts`

- [ ] **Step 1: Write the failing test**

Create `tests/jellyfin.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import { jellyfinEnabled, pickBestMatch, stripFeat } from '../server/utils/jellyfin'

const ENV = {
  JELLYFIN_URL: 'http://jellyfin:8096',
  JELLYFIN_API_KEY: 'key',
  JELLYFIN_USER_ID: 'uid',
} as never

describe('jellyfinEnabled', () => {
  it('true only when all three vars are set', () => {
    expect(jellyfinEnabled(ENV)).toBe(true)
    expect(jellyfinEnabled({ ...ENV, JELLYFIN_URL: '' } as never)).toBe(false)
    expect(jellyfinEnabled({ ...ENV, JELLYFIN_API_KEY: '' } as never)).toBe(false)
    expect(jellyfinEnabled({ ...ENV, JELLYFIN_USER_ID: '' } as never)).toBe(false)
  })
})

describe('stripFeat', () => {
  it('removes feat./ft. tails and parentheticals', () => {
    expect(stripFeat('Song feat. Other')).toBe('Song')
    expect(stripFeat('Song ft. Other')).toBe('Song')
    expect(stripFeat('Song (feat. Other)')).toBe('Song')
    expect(stripFeat('Plain')).toBe('Plain')
  })
})

describe('pickBestMatch', () => {
  const cand = (Id: string, Name: string, Artists: string[]) => ({ Id, Name, Artists, AlbumArtist: Artists[0] })

  it('matches on normalized title + artist', () => {
    const id = pickBestMatch(
      { title: 'A Forest', artist: 'The Cure' },
      [cand('1', 'A Forest', ['The Cure']), cand('2', 'Boys Don’t Cry', ['The Cure'])],
    )
    expect(id).toBe('1')
  })

  it('tolerates feat tags, remaster suffixes, punctuation, and case', () => {
    const id = pickBestMatch(
      { title: 'Dancing (feat. X)', artist: 'Robyn' },
      [cand('9', 'Dancing - 2010 Remaster', ['ROBYN'])],
    )
    expect(id).toBe('9')
  })

  it('returns null when the artist does not match', () => {
    const id = pickBestMatch(
      { title: 'A Forest', artist: 'Joy Division' },
      [cand('1', 'A Forest', ['The Cure'])],
    )
    expect(id).toBeNull()
  })

  it('returns null on an empty candidate list', () => {
    expect(pickBestMatch({ title: 'X', artist: 'Y' }, [])).toBeNull()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pnpm test -- tests/jellyfin.test.ts`
Expected: FAIL — cannot resolve `../server/utils/jellyfin`.

- [ ] **Step 3: Implement the pure parts of `server/utils/jellyfin.ts`**

Create `server/utils/jellyfin.ts` with the enabled-check, types, and pure matching. (Network functions added in Task 5 — file must still typecheck after this step, so include the pure parts only.)

```ts
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

// Drop "feat./ft. …" tails and any parenthetical so "Song (feat. X)" and
// "Song feat. X" both reduce to "Song" before normalization.
export function stripFeat(s: string): string {
  return s
    .replace(/\s*[([]\s*(feat\.?|ft\.?)\b[^)\]]*[)\]]/gi, '')
    .replace(/\s+(feat\.?|ft\.?)\b.*$/i, '')
    .trim()
}

function titleSimilarity(a: string, b: string): number {
  return Math.max(
    similarity(normKeyLoose(stripFeat(a)), normKeyLoose(stripFeat(b))),
    similarity(normKey(stripFeat(a)), normKey(stripFeat(b))),
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pnpm test -- tests/jellyfin.test.ts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/utils/jellyfin.ts tests/jellyfin.test.ts
git commit -m "feat(jellyfin): pure title+artist matching helpers"
```

---

## Task 5: Jellyfin util — network calls + orchestration

**Files:**
- Modify: `server/utils/jellyfin.ts`

- [ ] **Step 1: Add REST helpers + orchestration**

Append to `server/utils/jellyfin.ts`:

```ts
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
    let id: string | null = null
    try {
      id = pickBestMatch(track, await searchAudio(env, stripFeat(track.title)))
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
```

- [ ] **Step 2: Typecheck + run full unit suite**

Run: `pnpm typecheck && pnpm test`
Expected: PASS (pure tests still pass; new network code compiles).

- [ ] **Step 3: Commit**

```bash
git add server/utils/jellyfin.ts
git commit -m "feat(jellyfin): REST search/create/delete + recreate orchestration"
```

---

## Task 6: Status endpoint reports the jellyfin capability

**Files:**
- Modify: `server/api/spotify/status.get.ts`

- [ ] **Step 1: Add the jellyfin flag**

Rewrite `server/api/spotify/status.get.ts`:

```ts
import type { SpotifyStatus } from '~~/shared/types'
import { defineEventHandler } from 'h3'
import { loadEnv } from '../../utils/env'
import { jellyfinEnabled } from '../../utils/jellyfin'
import { readToken, spotifyEnabled } from '../../utils/spotify'

// Lets the UI decide whether to show the Spotify tab and whether to prompt for
// connect — without leaking secrets. `connected` reflects a stored token;
// validity is enforced lazily on use. `jellyfin` gates the Recreate action.
export default defineEventHandler(async (): Promise<SpotifyStatus> => {
  const env = loadEnv()
  const enabled = spotifyEnabled(env)
  const connected = enabled ? (await readToken()) !== null : false
  return { enabled, connected, jellyfin: jellyfinEnabled(env) }
})
```

- [ ] **Step 2: Typecheck**

Run: `pnpm typecheck`
Expected: PASS (resolves the transient error from Task 2).

- [ ] **Step 3: Commit**

```bash
git add server/api/spotify/status.get.ts
git commit -m "feat(spotify): report jellyfin capability in status"
```

---

## Task 7: Orchestration endpoint

**Files:**
- Create: `server/api/spotify/to-jellyfin.post.ts`

- [ ] **Step 1: Implement the endpoint**

Create `server/api/spotify/to-jellyfin.post.ts`:

```ts
import type { JellyfinPushResult } from '~~/shared/types'
import { createError, defineEventHandler, readBody } from 'h3'
import { z } from 'zod'
import { loadEnv } from '../../utils/env'
import { jellyfinEnabled, recreatePlaylistInJellyfin } from '../../utils/jellyfin'
import { fetchPlaylistTrackDetails, getValidAccessToken, spotifyEnabled } from '../../utils/spotify'

const schema = z.object({
  playlistId: z.string().min(1),
  playlistName: z.string().min(1),
})

export default defineEventHandler(async (event): Promise<JellyfinPushResult> => {
  const env = loadEnv()
  if (!spotifyEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Spotify is not configured.' })
  if (!jellyfinEnabled(env))
    throw createError({ statusCode: 503, statusMessage: 'Jellyfin is not configured.' })

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
    const tracks = await fetchPlaylistTrackDetails(token, parsed.data.playlistId)
    return await recreatePlaylistInJellyfin(env, parsed.data.playlistName, tracks)
  }
  catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err)
    throw createError({ statusCode: 502, statusMessage: `Recreate in Jellyfin failed: ${msg}` })
  }
})
```

- [ ] **Step 2: Typecheck + lint**

Run: `pnpm typecheck && pnpm lint`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add server/api/spotify/to-jellyfin.post.ts
git commit -m "feat(spotify): /api/spotify/to-jellyfin recreate endpoint"
```

---

## Task 8: UI — second action + summary modal

**Files:**
- Modify: `app/components/SpotifyPanel.vue`

- [ ] **Step 1: Add state, capability, and the recreate handler (script block)**

In `<script setup>`, extend the types import and add state after `resolvingId`:

```ts
import type { JellyfinPushResult, ParsedItem, SpotifyPlaylist, SpotifyResolveResult, SpotifyStatus } from '~~/shared/types'
```

```ts
const jellyfinEnabled = ref(false)
const recreatingId = ref<string | null>(null)
const result = ref<JellyfinPushResult | null>(null)
const showResult = ref(false)
```

In the `onMounted` status fetch, capture the flag (the `$fetch<SpotifyStatus>` already runs there):

```ts
    const s = await $fetch<SpotifyStatus>('/api/spotify/status')
    connected.value = s.connected
    jellyfinEnabled.value = s.jellyfin
```

Add the handler next to `pick`:

```ts
async function recreate(playlist: SpotifyPlaylist): Promise<void> {
  if (recreatingId.value)
    return
  recreatingId.value = playlist.id
  try {
    const res = await $fetch<JellyfinPushResult>('/api/spotify/to-jellyfin', {
      method: 'POST',
      body: { playlistId: playlist.id, playlistName: playlist.name },
    })
    result.value = res
    showResult.value = true
  }
  catch (err: unknown) {
    toast.add({ title: 'Recreate failed', description: describeError(err), color: 'error' })
  }
  finally {
    recreatingId.value = null
  }
}
```

- [ ] **Step 2: Restructure the card + add the modal (template block)**

Replace the `<button v-for=...>` card (the whole element from `<button` to its closing `</button>`) with a `div`-based card carrying two actions:

```vue
        <div
          v-for="p in playlists"
          :key="p.id"
          class="text-left rounded-lg border border-default p-2 transition"
          :class="{ 'opacity-50': resolvingId !== null || recreatingId !== null }"
        >
          <button
            type="button"
            :disabled="resolvingId !== null || recreatingId !== null"
            class="block w-full text-left hover:opacity-90"
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
          <UButton
            v-if="jellyfinEnabled"
            class="mt-2 w-full justify-center"
            size="xs"
            color="neutral"
            variant="soft"
            icon="i-lucide-list-music"
            :loading="recreatingId === p.id"
            :disabled="resolvingId !== null || recreatingId !== null"
            :label="recreatingId === p.id ? 'Recreating…' : 'Recreate in Jellyfin'"
            @click="recreate(p)"
          />
        </div>
```

Add the summary modal just before the closing `</UCard>`:

```vue
    <UModal v-model:open="showResult" title="Recreate in Jellyfin">
      <template #body>
        <div v-if="result">
          <p class="text-sm">
            <span class="font-medium">{{ result.matched }}</span> of
            <span class="font-medium">{{ result.total }}</span> tracks matched in
            “{{ result.playlistName }}”.
          </p>
          <p v-if="result.matched === 0" class="text-sm text-muted mt-1">
            No tracks were found in your Jellyfin library — nothing was created.
          </p>
          <div v-if="result.skipped.length" class="mt-3">
            <p class="text-xs uppercase tracking-wide text-muted mb-1">
              Skipped ({{ result.skipped.length }})
            </p>
            <ul class="max-h-64 overflow-y-auto text-sm space-y-0.5">
              <li v-for="(s, i) in result.skipped" :key="i" class="truncate">
                {{ s.artist }} — {{ s.title }}
              </li>
            </ul>
          </div>
        </div>
      </template>
    </UModal>
```

- [ ] **Step 3: Lint + typecheck**

Run: `pnpm lint && pnpm typecheck`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/components/SpotifyPanel.vue
git commit -m "feat(spotify): Recreate-in-Jellyfin button + summary modal"
```

---

## Task 9: Config docs + compose wiring

**Files:**
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `/home/tom/nas/docker-compose.yml`

- [ ] **Step 1: Document the vars in `.env.example`**

Add to `.env.example` near the Spotify block:

```bash
# Optional — enables the per-playlist "Recreate in Jellyfin" action. All three
# required. Reuses the nas stack's Jellyfin API key + user id.
JELLYFIN_URL=http://jellyfin:8096
JELLYFIN_API_KEY=your-jellyfin-api-key
JELLYFIN_USER_ID=your-jellyfin-user-id
```

- [ ] **Step 2: Document in `README.md`**

Add a short bullet under the Spotify feature section noting that setting `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_USER_ID` enables a "Recreate in Jellyfin" button on each playlist that matches tracks against the Jellyfin library (title + artist), replaces any same-name playlist, and skips tracks not in the library.

- [ ] **Step 3: Wire compose**

In `/home/tom/nas/docker-compose.yml`, in the `lidarr-bulk` service `environment:` block, add (reusing the existing nas vars):

```yaml
      - JELLYFIN_URL=http://jellyfin:8096
      - JELLYFIN_API_KEY=${API_KEY_JELLYFIN}
      - JELLYFIN_USER_ID=${JELLYFIN_USER_ID}
```

- [ ] **Step 4: Validate compose**

Run: `cd /home/tom/nas && docker compose config > /dev/null`
Expected: no errors.

- [ ] **Step 5: Commit (two repos)**

```bash
cd /home/tom/nas/webapps/lidarr-bulk
git add .env.example README.md
git commit -m "docs(jellyfin): document Recreate-in-Jellyfin env vars"

cd /home/tom/nas
git add docker-compose.yml
git commit -m "feat(lidarr-bulk): wire Jellyfin env for Recreate-in-Jellyfin"
```

---

## Task 10: Final verification

- [ ] **Step 1: Full gates**

Run: `pnpm test && pnpm typecheck && pnpm lint`
Expected: all PASS.

- [ ] **Step 2: Build smoke**

Run: `pnpm build`
Expected: builds without error.

---

## Self-Review notes

- Spec coverage: env (T1), types (T2), track fetch (T3), matching (T4), REST+orchestration incl. replace + zero-match skip (T5), capability flag (T6), endpoint with error mapping (T7), UI button + modal incl. zero-match message + skipped list (T8), config/compose/docs (T9), verification (T10). All spec sections covered.
- Open item from spec resolved: capability surfaced by extending `/api/spotify/status` (no extra round-trip).
- Type consistency: `JellyfinPushResult`, `SpotifyTrack`, `JellyfinAudioItem`, `recreatePlaylistInJellyfin`, `fetchPlaylistTrackDetails`, `pickBestMatch`, `stripFeat`, `jellyfinEnabled` used consistently across tasks.
- Normalization reuses `matching.ts` (`normKey`/`normKeyLoose`/`similarity`) — no duplicate normalizer.
