# Spotify → "Recreate in Jellyfin" — Design

Date: 2026-06-15
Status: Approved, pending implementation plan

## Goal

Add a second action to each Spotify playlist card in the lidarr-bulk app: **Recreate in
Jellyfin**. Clicking it fetches the Spotify playlist's tracks, matches each track against the
Jellyfin music library (title + primary artist, fuzzy), collects the matched library item IDs in
playlist order, replaces any existing same-name Jellyfin playlist, and creates the playlist for the
configured Jellyfin user. Tracks not present in the Jellyfin library are skipped ("c'est la vie").
The result (matched count + skipped track list) is shown inline in a summary modal.

This is distinct from the existing per-card action, which collapses a playlist's tracks into unique
**albums** and queues them into Lidarr. The new action operates at the **track** level and targets
Jellyfin, not Lidarr.

## Decisions (locked)

- **Duplicate same-name playlist:** Replace — delete the existing same-name Jellyfin playlist for
  the user, then create a fresh one from current matches. Makes re-runs idempotent and keeps the
  Jellyfin playlist a faithful mirror of Spotify.
- **Matching:** Title + primary artist, fuzzy (normalized: lowercase, strip punctuation, drop
  `feat.`/parenthetical/remaster noise). Balances recall against false matches.
- **Run/feedback:** Inline synchronous run, then a summary modal ("N of M tracks matched" + a
  scrollable list of skipped tracks).
- **Zero matches:** Skip — create nothing, and do **not** delete any existing same-name playlist.
  The summary modal still reports `0 of M matched`. Avoids empty/cluttered playlists.

## Reachability & credentials (already solved)

The nas stack already exposes Jellyfin to other nas-network services and already defines the
credentials the sibling `jellyfin-playlist-generator` uses for its own "Push to Jellyfin" export:

- Jellyfin reachable at `http://jellyfin:8096` on `nas-network` (lidarr-bulk already joins this
  network to reach Lidarr).
- `.env` already holds `API_KEY_JELLYFIN` and `JELLYFIN_USER_ID`.

We reuse this env contract rather than inventing a new one.

## Components

### 1. `server/utils/jellyfin.ts` (new) — Jellyfin client + pure matching helpers

Mirrors the structure of `server/utils/spotify.ts`: pure helpers alongside thin network calls so
the matching logic is unit-testable without hitting Jellyfin.

Pure (no network, unit-tested):
- `jellyfinEnabled(env): boolean` — true when `JELLYFIN_URL`, `JELLYFIN_API_KEY`, and
  `JELLYFIN_USER_ID` are all non-empty.
- `normalizeTitle(s): string` and `normalizeArtist(s): string` — lowercase, collapse whitespace,
  strip punctuation, drop `feat.`/`ft.` segments, trailing parentheticals (`(Remastered 2011)`,
  `(feat. X)`), and `- … Remaster`-style suffixes.
- `pickBestMatch(track: SpotifyTrack, candidates: JellyfinAudioItem[]): string | null` — return the
  `Id` of the best candidate whose normalized title equals the track's normalized title **and**
  whose normalized artist (any of the item's `Artists`/`AlbumArtist`) matches the track's normalized
  primary artist; otherwise `null`. This is the single source of truth for "matched vs skipped".

Network (thin, untested, following spotify.ts convention):
- `searchAudio(env, title, artist): Promise<JellyfinAudioItem[]>` —
  `GET {JELLYFIN_URL}/Items?IncludeItemTypes=Audio&Recursive=true&searchTerm=<title>&userId=<uid>&Fields=Artists,AlbumArtist&Limit=25`
  with header `X-Emby-Token: <JELLYFIN_API_KEY>`.
- `findPlaylistByName(env, name): Promise<{ id: string } | null>` —
  `GET /Items?IncludeItemTypes=Playlist&Recursive=true&userId=<uid>&searchTerm=<name>`, exact
  (case-insensitive) name match.
- `deletePlaylist(env, id): Promise<void>` — `DELETE /Items/<id>`.
- `createPlaylist(env, name, itemIds): Promise<string>` —
  `POST /Playlists` with body `{ Name, Ids: itemIds, UserId }`; returns the new playlist id.

### 2. `server/utils/spotify.ts` (extend) — track-level fetch

- `fetchPlaylistTrackDetails(accessToken, playlistId): Promise<SpotifyTrack[]>` — like
  `fetchPlaylistTracks` but with a `fields` mask that pulls track name + artists + album name, in
  playlist order. Delegates row-shaping to a new pure helper:
- `trackDetailsFromItems(items): SpotifyTrack[]` — pure, unit-tested, mirroring `albumItemsFromTracks`.
  Skips local files and podcast episodes and rows missing a title or primary artist. Preserves order;
  does **not** dedupe (a Spotify playlist may legitimately list a track twice — dedupe of matched
  Jellyfin ids happens later, by item id).

### 3. `server/api/spotify/to-jellyfin.post.ts` (new) — orchestration

Body: `{ playlistId: string, playlistName: string }` (zod-validated).

Flow:
1. Guard `spotifyEnabled` (503 if not), resolve a valid access token (401 if not connected),
   guard `jellyfinEnabled` (503 if not).
2. `fetchPlaylistTrackDetails`.
3. For each track in order: `searchAudio` → `pickBestMatch`. A per-track Jellyfin error is caught
   and the track is counted as skipped (run does not abort). Accumulate matched ids
   (order-preserving, deduped by id) and skipped `{ title, artist }`.
4. If zero matched ids → return summary with `matched: 0`, `jellyfinPlaylistId: null`, **without**
   touching Jellyfin (no delete, no create).
5. Otherwise: `findPlaylistByName` → if found `deletePlaylist` → `createPlaylist`.
6. Return `JellyfinPushResult`.

Connection/auth failures against Jellyfin (not per-track search failures) surface as a 502.

### 4. `shared/types.ts` (extend)

```ts
export interface SpotifyTrack { title: string, artist: string, album?: string }

export interface JellyfinPushResult {
  playlistName: string
  total: number                       // tracks considered (after local/episode filtering)
  matched: number                     // unique Jellyfin items added
  skipped: { title: string, artist: string }[]
  jellyfinPlaylistId: string | null   // null when zero matches (nothing created)
}
```

### 5. `app/components/SpotifyPanel.vue` (modify)

The playlist card is currently a single `<button>` whose click queues albums to Lidarr. Restructure
the card into a `div` containing two actions (a nested button inside a button is invalid HTML):
- Primary: existing "queue unique albums to Lidarr" behavior (`pick`).
- New: a small "Recreate in Jellyfin" button (`i-lucide-list-music` or similar) that calls
  `recreate(playlist)`.

`recreate` POSTs to `/api/spotify/to-jellyfin`, tracking a separate `recreatingId` ref so the two
actions disable independently. On success, open a `UModal` summarizing
`N of M tracks matched in "<name>"` with a scrollable list of skipped tracks (and a note when the
playlist was skipped due to zero matches). Errors → error toast via the existing `describeError`.

If Jellyfin is not configured, the Recreate button is hidden (status comes from a small addition to
`/api/spotify/status` or a dedicated capability flag — see Open items).

### 6. Config

- `server/utils/env.ts`: add optional `JELLYFIN_URL`, `JELLYFIN_API_KEY`, `JELLYFIN_USER_ID`
  (default `''`); feature is gated by all three present.
- `docker-compose.yml` (nas): pass `JELLYFIN_URL=http://jellyfin:8096`,
  `JELLYFIN_API_KEY=${API_KEY_JELLYFIN}`, `JELLYFIN_USER_ID=${JELLYFIN_USER_ID}` into the
  `lidarr-bulk` service.
- `.env.example` + `README.md`: document the three vars and that the feature is hidden when unset.

## Error handling summary

| Failure | Behavior |
| --- | --- |
| Spotify not configured | 503 |
| Spotify not connected / refresh fails | 401 |
| Jellyfin not configured | 503 (button hidden in UI anyway) |
| Jellyfin unreachable / bad token | 502, single error toast |
| Per-track Jellyfin search error | Track counted as skipped, run continues |
| Zero matches | No Jellyfin mutation; modal reports `0 of M` |

## Testing

Unit tests (in `tests/`, matching existing convention):
- `normalizeTitle` / `normalizeArtist` — punctuation, case, `feat.`, parenthetical/remaster noise.
- `pickBestMatch` — exact match, artist mismatch → null, multiple candidates → best, empty → null.
- `trackDetailsFromItems` — skips local/episode/incomplete rows, preserves order, keeps duplicates.

Network functions and the endpoint orchestration stay untested (consistent with spotify.ts).

## Out of scope (YAGNI)

- Syncing playlist order/contents on Spotify changes (one-shot recreate only).
- Matching by ISRC or duration tie-breaking (title + artist is sufficient for v1).
- Streaming per-track progress (inline synchronous run chosen).
- Configurable match strictness in the UI.

## Open items for the plan

- How the UI learns Jellyfin is enabled: extend `/api/spotify/status` payload with a
  `jellyfin: boolean` capability flag, or add a tiny `/api/jellyfin/status`. (Plan to pick;
  leaning toward extending the existing status call to avoid an extra round-trip.)
