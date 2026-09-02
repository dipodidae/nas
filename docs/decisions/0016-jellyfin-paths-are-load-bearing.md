# ADR-0016 — Jellyfin's volume mappings are load-bearing

**Date:** 2026-09-02 (records a standing owner instruction plus what depends on it)
**Status:** accepted
**Background:** `docs/jellyfin-playback-audit.md` §3.1 and §4.1

## The instruction

**Jellyfin's volume mappings are NOT to be changed, per the owner's standing
instruction.** `${SHARE_DIRECTORY}:/data/movies:ro` is intentional even though
it looks misnamed — the whole share is mounted there, so music lives at
`/data/movies/music` and series at `/data/movies/series`.

## Why it is not merely cosmetic

Three separate things are calibrated to that exact path and would break
together:

1. **Every Jellyfin library path** is registered under `/data/movies/...`.
2. **The `mapFrom`/`mapTo` path mapping** on Sonarr's and Radarr's MediaBrowser
   (Jellyfin) connections. Before it was fixed on 2026-09-01, *arr
   "Update Library" calls reached Jellyfin, returned `204`, and did nothing —
   because the *arrs sent `/music/...`-shaped paths Jellyfin dropped.
3. **playlist-generator's `LOCAL_PATH_PREFIX` / `JELLYFIN_PATH_PREFIX` pair**
   (`/music` → `/data/movies/music`), which rewrites local track paths for its
   "Push to Jellyfin" export.

Renaming the mount would require changing all three in lockstep. The cost of
the odd-looking name is much lower than the cost of that.

## The two API-key traps

- `API_KEY_JELLYFIN_ARR` is the `arr-integrations` key, used by the \*arrs'
  MediaBrowser connections. `API_KEY_JELLYFIN` is **Jellyseerr's** key, reused
  by `lidarr-bulk` and `playlist-generator`. **They are not interchangeable.**
- `GET /notification` on Sonarr/Radarr/Lidarr **masks `apiKey` as `**\*\*\***\*`**.
  Write the real key back into the field before the `PUT`, or the connection
  silently gets a literal `**\*\*\*\***`. Confirm the result in the DB rather than
  by re-`GET`ting, which re-masks.
- `.docker-config/*/[app].db` is **WAL-mode**: copying only the `.db` without
  `-wal`/`-shm` reads back stale values and will show a just-saved `1` as `0`.

## Related, and separate

The per-event delete toggles (`onSeriesDelete`, `onMovieDelete`) are a
different mechanism from path mapping: **mapping decides _where_ a call goes,
the toggles decide _whether_ one is made at all.** Both were needed; both are
now on. Lidarr's `onArtistDelete`/`onAlbumDelete` are deliberately left off
because its MediaBrowser connection exposes no `mapFrom`/`mapTo` fields —
deletion there is `scripts/lidarr_jellyfin_bridge.py`'s job. Full detail:
`docs/jellyfin-playback-audit.md` §3.1 and §4.1.
