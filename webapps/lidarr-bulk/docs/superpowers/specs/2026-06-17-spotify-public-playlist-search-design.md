# Spotify public-playlist search — design

**Date:** 2026-06-17
**Status:** Approved

## Goal

From the existing **Spotify** tab, let the user search **all public Spotify
playlists** by keyword (e.g. "synthwave", "90s rock"), pick one, and queue its
unique albums into Lidarr — the same one-click flow that already works for the
user's own playlists, including the "Recreate in Jellyfin" button.

Today the tab only loads the connected account's own playlists
(`GET /me/playlists`). This adds keyword discovery across Spotify's public
catalog.

## Decisions (from brainstorming)

- **Lives in the existing Spotify tab**, not a new tab.
- **Reuses the connected account's OAuth token** (and its refresh logic) — no
  new auth path. The search box is only shown when connected.
- **First page of results only** (default `limit=24`). No infinite pagination,
  no debounced live search — submit on Enter / button click. Easy to extend.

## Why this is small

The *resolve* step needs **zero new code**: `/api/spotify/resolve` already
accepts an arbitrary `playlistId` and reads its tracks, so any public playlist
chosen from search results flows through the existing queue-to-Lidarr and
Recreate-in-Jellyfin handlers unchanged.

## New / changed pieces

1. **`server/utils/spotify.ts`**
   - `playlistsFromSearch(items)` — **pure**, unit-tested. Maps a Spotify
     `/search` playlist page to `SpotifyPlaylist[]` via the existing
     `trimPlaylist`, **filtering out `null` entries** (Spotify-owned /
     deprecated playlists return `null` in search results since late 2024;
     unfiltered they would crash the map) and entries lacking a string `id`.
   - `searchPlaylists(accessToken, query, limit=24)` — thin network wrapper:
     `GET /v1/search?type=playlist&q=<query>&limit=<n>`, returns
     `playlistsFromSearch(body.playlists.items)`. Blank query returns `[]`
     without calling Spotify. Mirrors the existing untested network fetchers.

2. **`server/api/spotify/search-playlists.get.ts`**
   - Reads `?q=`. Blank → `{ playlists: [] }` without hitting Spotify.
   - Guards: 503 if Spotify not configured, 401 if not connected / refresh
     failed, 502 on upstream error — mirroring `playlists.get.ts`.
   - Returns `{ playlists: SpotifyPlaylist[] }`.

3. **`app/components/SpotifyPlaylistCard.vue`** (extracted)
   - The playlist-card markup currently inlined in `SpotifyPanel.vue` is
     extracted so both grids (own playlists + search results) reuse it.
   - Props: `playlist`, `busy`, `resolving`, `recreating`, `jellyfinEnabled`.
   - Emits: `pick`, `recreate`. Also shows `owner` (useful for public results).

4. **`app/components/SpotifyPanel.vue`**
   - When connected, render a **search section** above the "Your playlists"
     section: a `UInput` + Search button, then a results grid of
     `SpotifyPlaylistCard`s. Adds `searchQuery`/`searchResults`/`searching`/
     `searchError`/`searched` refs and a `runSearch()` method, plus a `busy`
     computed shared by both grids. Existing `pick()` / `recreate()` are reused
     verbatim (they already take a playlist object).

## No env / config changes

Reuses existing `SPOTIFY_API_CLIENT_ID/SECRET/REDIRECT_URI` and the stored
token. Nothing to document in `.env.example` / `AGENTS.md`.

## Testing

- Unit-test `playlistsFromSearch`: drops `null` items, drops id-less items,
  maps remaining via `trimPlaylist` (id/name/trackCount/imageUrl/owner),
  returns `[]` for an empty page. (Vitest, alongside existing `spotify.test.ts`.)
- Network `searchPlaylists` and the route follow the existing pattern of thin,
  un-unit-tested I/O (consistent with `fetchPlaylists` / `playlists.get.ts`).

## Out of scope (YAGNI)

Pagination / "load more", live-as-you-type search, client-credentials
(anonymous) search, market/locale filters.
