---
name: nas-music-pipeline
description: Use when touching anything on the slskd -> Lidarr -> Navidrome music path - the reaper scripts, Lidarr's indexer or download client, slskd config, Navidrome's library or playlists, or any path that contains /music. Jellyfin no longer holds music. Two namespaces for one directory and a list of things that must not change.
---

# The music pipeline

`docs/music-pipeline-integration.md` is the full spec. This is what you must know before
editing anything on the path.

## The one fact everything depends on

**Jellyfin has no music library (ADR-0051, 2026-09-16). Navidrome owns music.**
Everything below that mentions Jellyfin and music is history; `lidarr_jellyfin_bridge.py`,
`music_library_sweep.py`, `jellyfin_image_backfill.py` and `jellyfin_nfo_dates.py` are
**deleted**, along with their four cron jobs.

**What replaced them:** Navidrome's own filesystem watcher, its hourly
`ND_SCANNER_SCHEDULE`, and Lidarr's `Subsonic` connector firing `/rest/startScan` on
import (ADR-0049). Three independent triggers where there was one bridge.
`make verify-runtime` now asserts music stays *out* of Jellyfin.

## Two namespaces for one directory

`/mnt/drive/music` on the host is mounted into three containers under three paths. Almost
every silent failure here is a path from one namespace handed to something that only
understands another.

| Container | Sees music as                                                                              |
| --------- | ------------------------------------------------------------------------------------------ |
| Lidarr    | `/data/music` (root folder) — and `/music` in history written before the 2026-09-02 repath |
| slskd     | `/music` — **no `/data` mount at all**                                                     |
| Navidrome | `/music` (read-only) — plus playlists at `/music/Playlists/*.m3u8` (ADR-0050)              |

Jellyfin's `/data/movies/music` was the third and is retired. Note Navidrome and slskd
agree on `/music`, which is convenient and also a trap: they are different mounts of the
same host directory, so a path from one is only accidentally valid in the other.

**Blast radius of a path migration is every consumer that stored the prefix.** The
2026-09-02 repath was verified exhaustively _inside_ Lidarr and every check passed; none
asked what else had the old prefix compiled in. Two things broke for a day.

## Do not change without asking

- Jellyfin's `${SHARE_DIRECTORY}:/data/movies` mapping (ADR-0016) — still load-bearing
  for movies and series, even though nothing indexes music under it any more
- `useFallbackSearch` / `useTrackFallback` on Lidarr indexer id 4 — both `False`.
  Either one on turns one search into 4–15 and earns a **30-minute Soulseek ban**.
  They live in Lidarr's SQLite DB, so a config restore reintroduces them silently.
- slskd's healthcheck must stay **Soulseek-login-independent**. A login-aware healthcheck
  on the autoheal path is a permanent restart spiral; the only cure for a ghost session is
  to leave slskd **down 15–30 min**, then cold start.
- Lidarr's Subsonic connector: `updateLibrary` on, and only `onReleaseImport`/`onUpgrade`/
  `onRename` on — the other triggers are `Notify()`-only and inert (ADR-0049).
- Lidarr in any Cleanuparr module — never. Its only client is slskd, which Cleanuparr
  cannot see.
- **`slskd_complete_sweep.py` must not be retired** in favour of slskd `retention` — the
  file half of retention is inert, and this script is the only thing reclaiming disk.

## Two scripts whose names mislead

- `slskd_complete_sweep.py` (`:22`) deletes **directories** already imported into `/music`.
- `slskd_cleanup.py` (`:37`) deletes **transfer records** and orphan incomplete dirs.

The doc had these swapped twice. Trust the docstrings.

## Normal-looking states that are not problems

- All transfers `Queued, Remotely` — a peer's upload queue. Waiting hours is routine.
- `albumImportIncomplete` — usually a genuinely partial release on Soulseek.
- `downloadFailed` reading `"Manually marked as failed"` — that is the reaper doing its
  job. Never read the `downloadFailed` count without checking the message field.
- `slskd_complete_sweep.py` logging `deleted 0/0 dirs` — it is declining (dirs not fully
  imported), not idling.
- `slskd_cleanup.py` logging `nothing to clean` — retention now reaps records first.
- The bridge logging `nothing to report` while the cursor advances — normal when the only
  new records are non-file events. It is a fault signal **only** if file imports happened
  in that window. Cross-check `/api/v1/history` before concluding anything.

## Quick verification

```bash
set -a; . ./.env; set +a; . .venv/bin/activate
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://localhost:8686/api/v1/health      # expect []
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://localhost:8686/api/v1/rootfolder  # /data/music
curl -s -H "X-API-Key: $API_KEY_SLSKD" http://localhost:5030/api/v0/application \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["server"]["state"])'  # Connected, LoggedIn

python scripts/check-lidarr-navidrome-notification.py   # the import -> scan trigger
python scripts/check-jellyfin-no-music.py               # music must stay OUT of Jellyfin
curl -s "http://localhost:4533/rest/getScanStatus.view?u=$NAVIDROME_LIDARR_USER\
&p=$NAVIDROME_LIDARR_PASSWORD&v=1.16.1&c=probe&f=json"   # Navidrome's own view
make check && make verify-runtime          # verify-runtime only pushes with VERIFY_NOTIFY=1
```

**Lidarr's API is `v1`.** `v3` returns a bare `404` that looks like the service being down.

**A Navidrome full scan survives a restart.** `full_scan_in_progress` on its `library`
row is what relaunches it, not the `LastScanType` property — which Navidrome rewrites
back to `full` on boot. A full re-read of 168k tracks takes ~2 h and blocks every other
scan with `already scanning`. Back up the DB before clearing that column (ADR-0050).
