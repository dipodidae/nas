---
name: nas-music-pipeline
description: Use when touching anything on the slskd -> Lidarr -> Jellyfin music path - the bridge, the reaper scripts, Lidarr's indexer or download client, slskd config, Jellyfin's music library, or any path that contains /music. Three namespaces for one directory and a list of things that must not change.
---

# The music pipeline

`docs/music-pipeline-integration.md` is the full spec. This is what you must know before
editing anything on the path.

## The one fact everything depends on

**`EnableRealtimeMonitor` is `false` on every Jellyfin library.** Jellyfin is not watching
the filesystem. Nothing appears in Jellyfin because it showed up on disk.

**`scripts/lidarr_jellyfin_bridge.py` and the weekly per-library scan are the entire
surface between disk and Jellyfin.** If both miss a change, nothing else catches it.

## Three namespaces for one directory

`/mnt/drive/music` on the host is mounted into three containers under three paths. Almost
every silent failure here is a path from one namespace handed to something that only
understands another.

| Container | Sees music as                                                                              |
| --------- | ------------------------------------------------------------------------------------------ |
| Lidarr    | `/data/music` (root folder) — and `/music` in history written before the 2026-09-02 repath |
| slskd     | `/music` — **no `/data` mount at all**                                                     |
| Jellyfin  | `/data/movies/music` (read-only; the `:/data/movies` name is deliberate)                   |

The bridge maps both Lidarr spellings, longest-root-first, so a broad `/data` cannot
swallow `/data/music`.

**Blast radius of a path migration is every consumer that stored the prefix.** The
2026-09-02 repath was verified exhaustively _inside_ Lidarr and every check passed; none
asked what else had the old prefix compiled in. Two things broke for a day.

## Do not change without asking

- Jellyfin's `${SHARE_DIRECTORY}:/data/movies:ro` mapping (ADR-0016)
- `useFallbackSearch` / `useTrackFallback` on Lidarr indexer id 4 — both `False`.
  Either one on turns one search into 4–15 and earns a **30-minute Soulseek ban**.
  They live in Lidarr's SQLite DB, so a config restore reintroduces them silently.
- slskd's healthcheck must stay **Soulseek-login-independent**. A login-aware healthcheck
  on the autoheal path is a permanent restart spiral; the only cure for a ghost session is
  to leave slskd **down 15–30 min**, then cold start.
- Lidarr's `onArtistDelete` / `onAlbumDelete` — `False` until its connection gains
  `mapFrom`/`mapTo` (tripwired by `check-lidarr-jellyfin-notification.py`).
- Lidarr in any Cleanuparr module — never. Its only client is slskd, which Cleanuparr
  cannot see.
- The bridge's **exit 2 on an unmappable path** and **exit 2 on `HistoryExhausted`**.
- The bridge's cursor is a history **`id`**, not a date, and its state file is written
  with `os.replace`.
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

python scripts/check-lidarr-bridge-root.py
python scripts/lidarr_jellyfin_bridge.py --dry-run --since-min 2880 --state /tmp/probe.json
python scripts/music_library_sweep.py      # both directions, ~90s
make check && make verify-runtime          # verify-runtime only pushes with VERIFY_NOTIFY=1
```

**Lidarr's API is `v1`.** `v3` returns a bare `404` that looks like the service being down.

`--since-min` is ignored if the state file already has a cursor — point `--state` at a
throwaway path or you will always see `nothing to report`.
