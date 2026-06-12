# Spotify Playlist → Lidarr Album Queue — Design

**Date:** 2026-06-12
**Status:** Approved (brainstorming complete)

## Summary

Add Spotify as a new **album source** to lidarr-bulk. The user authorizes the
app with Spotify once (single-user, persistent OAuth), picks one of their
playlists from a grid, and the app resolves every track's album, dedupes them,
and queues each unique album into Lidarr via the **existing job pipeline** with
one click.

Spotify is purely a source: it emits the same item shape (`{raw, kind:'album',
artist, title}`) that the existing paste/parse and AI-discover paths produce, so
all downstream matching, job execution, review-of-ambiguous-matches, and live
monitoring are unchanged and already tested.

## Decisions (from brainstorming)

- **Auth model:** Single-user, persistent. Connect once; refresh token stored in
  `CONFIG_DIR`. Fits the private homelab.
- **What to queue:** Unique albums, deduped by Spotify album id (N songs from one
  album → 1 album).
- **Review gate:** One-click queue all — resolve and queue immediately using the
  saved `settings.json` defaults. (Ambiguous Lidarr matches still surface in the
  existing JobMonitor as `needsReview`, unchanged.)

## Architecture

### Auth (OAuth Authorization Code, server-side)

New `server/utils/spotify.ts` owns all Spotify concerns:

- Build authorize URL — scopes `playlist-read-private playlist-read-collaborative`.
- Exchange callback `code` → tokens.
- Refresh `access_token` on expiry using the stored `refresh_token`.
- Persist `{access_token, refresh_token, expires_at}` to
  `${CONFIG_DIR}/spotify-token.json` (same dir as `settings.json`).

Endpoints (thin handlers; logic lives in `spotify.ts`):

| Endpoint | Purpose |
|---|---|
| `GET /api/spotify/login` | Redirect to Spotify authorize URL. |
| `GET /api/spotify/callback` | Exchange `code`, store tokens, redirect back to UI. |
| `GET /api/spotify/status` | `{enabled, connected}`. `enabled` = both env keys present (mirrors `ai/status`); `connected` = valid token file present. |
| `POST /api/spotify/disconnect` | Delete the token file. |
| `GET /api/spotify/playlists` | List the user's playlists (paginated). |
| `POST /api/spotify/resolve` | Resolve a playlist → deduped album items + stats. |

### Setup prerequisite (documented, not code)

The redirect URI `https://<lidarr-bulk-host>/api/spotify/callback` must be
registered in the Spotify developer dashboard for the configured client ID. Add
`SPOTIFY_REDIRECT_URI` to `.env.example` and document in README. The existing
`SPOTIFY_API_CLIENT_ID` / `SPOTIFY_API_CLIENT_SECRET` are reused.

## Data flow

### `GET /api/spotify/playlists`

Calls `GET /me/playlists`, follows `next` until exhausted, returns trimmed
`{id, name, trackCount, imageUrl, owner}[]`.

### `POST /api/spotify/resolve` `{playlistId}`

1. Page through `GET /playlists/{id}/tracks` (100/page, follow `next`), collect
   all tracks.
2. For each track read `track.album` (`{id, name, artists[]}`); skip local files
   and null/episode items (no usable album).
3. **Dedupe by Spotify album id.** Track per-album playlist hit count for
   display/logging.
4. Emit items in the existing pipeline shape:
   `{ raw: "<artist> - <album>", kind: 'album', artist, title }`, where artist =
   the album's primary artist.

Returns `{ items, stats: { tracks, skipped, uniqueAlbums } }`.

Matching to Lidarr is **not** re-implemented — items go to
`createJob('album', items, …)`, which already runs `matching.ts` against Lidarr
and flags ambiguous results as `needsReview`.

## Frontend

New `app/components/SpotifyPanel.vue`, surfaced as a tab beside AI Discover. Tab
appears only when `/api/spotify/status` reports `enabled` (same gating as AI).

- **Not connected:** single **"Connect Spotify"** button → `/api/spotify/login`.
- **Connected:** grid of playlist cards (cover, name, track count). Clicking a
  card → `POST /api/spotify/resolve` → immediately `POST /api/jobs` using saved
  `settings.json` defaults (root folder, quality/metadata profile, monitor mode),
  then hands off to the existing `JobMonitor`. A small **"Disconnect"** link is
  available.

## Error handling

- `enabled` false → tab hidden (no key leak; mirrors AI).
- Token expired → transparent refresh in `spotify.ts`. Refresh failure (revoked
  grant) → `connected:false`, UI shows Connect again.
- Spotify upstream errors → surfaced as `502` with a clean message (like
  `ai/suggest`).
- Empty playlist / all-local tracks → friendly "no resolvable albums" message,
  no empty job created.

## Testing (Vitest, matching `tests/` style)

Unit tests for `spotify.ts`:

- Token persistence and refresh logic (mocked fetch).
- Pagination following `next` for playlists and tracks.
- **Dedup/resolve transform** (highest value): playlist JSON fixture → expected
  deduped album items, including skipping local/null tracks and per-album counts.

Endpoints stay thin so logic lives in the testable util, consistent with the
repo's thin-handler / pure-logic convention.

## Out of scope (YAGNI)

- Multi-user / per-session Spotify auth.
- Queuing artists or a per-run album/artist toggle.
- Pre-queue preview/deselect or per-album candidate picker (one-click chosen).
- Liked Songs / saved albums as sources (playlists only for now).
