# Pipeline baseline — 2026-09-04

Ground truth captured **before any change**, per Phase 0. Every figure here is a
recorded observation with the command that produced it. Later claims of the form
"upstream fixed X" must name a version relative to this table.

Captured on branch `harden/pipeline`, from `main` @ `aeb1720`.

---

## 1. Versions and digests

`docker inspect -f '{{index .RepoDigests 0}}'` on each running container's image:

| Service | Tag | Digest | Image built |
| --- | --- | --- | --- |
| lidarr | `lscr.io/linuxserver/lidarr:nightly` | `sha256:24322462f93e00da24c4734aad243d294255d0d4c99e6d4f1ff93d587c1798b6` | 2026-09-02 |
| slskd | `slskd/slskd:latest` | `sha256:ecd4026d4f8fb504e2cc55323efa2c1f5b56d20d3686b018249cc36b48ea17a6` | 2026-07-19 |
| jellyfin | `lscr.io/linuxserver/jellyfin:10.11.11ubu2604-ls47` | `sha256:438e44330078e6b1a810fdec9dc0f4773e6595edb137c5eb4417a516da4c7f0e` | 2026-09-01 |
| autoheal | `willfarrell/autoheal:latest` | `sha256:515e6fa8a610eb7dcfea39280a56ec7770917f9f6e30b7371f9eb3228bae26d0` | 2026-09-02 |

Application versions, from each service's own API:

| Component | Version | Notes |
| --- | --- | --- |
| **Jellyfin** | **10.11.11** | `GET /System/Info`. Within the range of the open full-rescan bug (#16729, reported vs 10.11.8). |
| **Lidarr** | **3.1.5.5056** (`packageVersion 3.1.5.5056-ls213`) | `GET /api/v1/system/status`. Branch **`nightly`**, `isNetCore: true`, runtime **.NET 8.0.27**. Plugin support present. |
| **Tubifarry** | **2.1.1.0** | `GET /api/v1/system/plugins`. Built against `Lidarr.Core 2.1.1`, target `.NETCoreApp v8.0`. |
| **slskd** | **0.26.0.0** (`0.26.0.0+e42a525d`) | `GET /api/v0/application`. `isUpdateAvailable: false`. |

**Two version facts that matter for later phases:**

1. Tubifarry reports `installedVersion: 2.1.1.0`, `availableVersion: 2.1.1.0`, but
   `updateAvailable: **true**` — self-contradictory. We are **not** on the 2.2.x line, so
   any 2.2.x-only feature (the built-in QueueCleaner of Phase 4.6) **is not present on this
   box**. Upgrading to 2.2.x is also the release that changes the slskd indexer GUID, which
   silently discards every blocklisted release. That is a migration, not a bump.
2. Jellyfin is **10.11.11**, not 10.11.8. The full-rescan-on-new-item bug must be measured
   here rather than assumed either way.

## 2. Host and limits

```
$ free -h
               total        used        free      shared  buff/cache   available
Mem:            30Gi        11Gi        10Gi       421Mi       9.4Gi        19Gi
Swap:           15Gi       6.6Gi       9.1Gi
```

Jellyfin is capped at `mem_limit: 10g` / `memswap_limit: 10g` (ADR-0008). Host total is
**30 GiB**, so the doc's "4.19 GB peak" event is ~14% of RAM and well under the cap; the
4 GB figure is a `stack_watchdog` alert threshold, not a system limit.

Note **6.6 GiB of swap is in use** with 10 GiB free RAM. Recorded, not yet explained.

## 3. Lidarr configuration

### Root folder — `GET /api/v1/rootfolder`

```json
[{ "id": 1, "name": "Music", "path": "/data/music", "accessible": true,
   "freeSpace": 4231986286592, "totalSpace": 9920821608448 }]
```

Exactly one root, `/data/music`. Matches the bridge's `DEFAULT_MAP_FROM`. `GET /api/v1/health` → `[]`.

### Indexer id 4 — `Slskd (Soulseek)` / `SlskdIndexer` / `SlskdSettings`

`enableAutomaticSearch=True  enableInteractiveSearch=True  enableRss=True  priority=25  tags=[]`

```
baseUrl                  = 'http://slskd:5030'      onlyAudioFiles           = True
externalUrl              = ''                       includeFileExtensions    = []
apiKey                   = <redacted>               earlyReleaseLimit        = None
fileLimit                = 10000                    maximumPeerQueueLength   = 100
minimumPeerUploadSpeed   = 0                        minimumResponseFileCount = 1
trackCountFilter         = 0                        responseLimit            = 100
timeoutInSeconds         = 10                       appendYear               = False
normalizedSeach          = False                    handleVolumeVariations   = False
useFallbackSearch        = False   <-- LOAD-BEARING useTrackFallback         = False  <-- LOAD-BEARING
minimumResults           = 0                        ignoreListPath           = ''
searchTemplates          = ''                       maxGrabsPerUser          = 3
grabLimitInterval        = 24                       maxQueuedPerUser         = 0
concurrentSearchLimit    = 1                        maxRetryAttempts         = 3
```

Both ban-risk flags confirmed `False`. (`normalizedSeach` is upstream's typo, not ours.)

### Download client id 2 — `Slskd` / `SlskdClient`

`enable=True  priority=1  removeCompletedDownloads=True  removeFailedDownloads=True`

```
baseUrl   = 'http://slskd:5030'   timeout               = 60     retryAttempts = 3
host      = 'slskd'               cleanStaleDirectories = True   inclusive     = False
apiKey    = <redacted>            isRemotePath          = False
```

### Notification id 6 — `Jellyfin` / `MediaBrowser` / `MediaBrowserSettings`

**Field names — this is the §5.3 tripwire baseline:**

```
['host', 'port', 'useSsl', 'urlBase', 'apiKey', 'notify', 'updateLibrary']
```

**No `mapFrom`, no `mapTo`.** §6.2 of the pipeline doc is **CONFIRMED against the live
instance**: Lidarr has no way to translate its own root spelling, so its Jellyfin
connection cannot work regardless of how it is configured.

Event toggles, with the capability flag beside each:

| Toggle | Value | `supportsOn…` |
| --- | --- | --- |
| `onReleaseImport` | **True** | True |
| `onRename` | **True** | True |
| `onTrackRetag` | **True** | True |
| `onUpgrade` | **True** | True |
| `onArtistDelete` | **False** (deliberate, §10) | True |
| `onAlbumDelete` | **False** (deliberate, §10) | True |
| `onArtistAdd` | False | True |
| `onGrab` | False | True |
| `onHealthIssue` / `onHealthRestored` | False | True |
| `onApplicationUpdate` | False | True |
| `onDownloadFailure` / `onImportFailure` | False | **False** (unsupported) |

The four `True` toggles are firing into the dead connection described above — they cost a
wasted HTTP call per import and nothing else.

## 4. Jellyfin configuration

### Libraries — `GET /Library/VirtualFolders`

| Name | CollectionType | ItemId | Location |
| --- | --- | --- | --- |
| Music | `music` | `7e64e319657a9516ec78490da03edccb` | `/data/movies/music` |
| Movies | `movies` | `f137a2dd21bbc1b99aa5c0f6bf02a805` | `/data/movies/movies` |
| TV Shows | `tvshows` | `767bffe4f11c93ef34b805451a696a4e` | `/data/movies/series` |

**Exactly one music library, one location.** This eliminates "a second music library" as an
explanation for the §1.1 count discrepancy before it is investigated.

### Logging

```
$ ls .docker-config/jellyfin/config/logging*.json
  -> NO logging*.json present
$ grep -c '\[DBG\]' .docker-config/jellyfin/log/log_20260904.log
  0            ( [INF] lines: 55 )
$ docker inspect -f '{{.State.StartedAt}}' jellyfin
  2026-09-02T15:08:30Z
```

**No `logging.json` exists at all.** The pipeline doc's §8.3 claim that stage 4 is
unobservable is **CONFIRMED**: zero Debug lines are emitted, so a correct and a bogus
`POST /Library/Media/Updated` are genuinely indistinguishable from the log.

This also settles the Phase 3.1 hot-reload gotcha in the worst direction: because the file
does not exist, creating it **requires exactly one Jellyfin restart** to take effect.

## 5. slskd configuration

Full `slskd.yml` is in the repo at `.docker-config/slskd/slskd.yml` (5638 bytes, last
modified 2026-09-02). The settings that Phase 4 proposed to adopt are recorded here because
**most of them are already in place**:

| Phase 4 proposal | Actual state on this box |
| --- | --- |
| 4.1 `retention:` for transfers and files | **ALREADY SET.** `search: 1440`; download `succeeded 1440 / errored 10080 / cancelled 60 / failed 10080`; `files.complete: 20160`, `files.incomplete: 43200`; `logs: 180`. |
| 4.2 `shares.cache.retention` | **ALREADY SET** to `10080` (weekly), with `storage_mode: disk` and `workers: 4`. The comment records that a memory cache caused a 45-min unbound-port window and an autoheal restart loop on 2026-09-02. |
| 4.3 `transfers.download.destination.permissions.mode` | **ALREADY SET** to `644`. |
| 4.3 `transfers.download.retry` | **ABSENT.** Genuinely available. |
| 4.4 `integrations.webhooks` / `integrations.scripts` | **BOTH EMPTY** (`{}`). Genuinely available. |
| 4.7 `metrics.enabled` | **`false`.** Genuinely available; `/metrics` route configured, auth enabled. |

Other values captured for later reference:

```yaml
directories: { incomplete: /downloads/incomplete/slskd, downloads: /downloads/complete/slskd }
shares.directories: [ /music ]
transfers.download: { slots: 500, speed_limit: 2147483647 }
throttling.search.incoming: { concurrency: 10, circuit_breaker: 500, response_file_limit: 500 }
soulseek.connection.timeout: { connect: 10000, inactivity: 15000, transfer: 30000 }
soulseek: { address: vps.slsknet.org, port: 2271, listen_port: 50300, diagnostic_level: Info }
filters.search.request: [ '^.{1,2}$', '^(\.?pdf|\.?docx|\.?xlsx|\.?doc|\.?xls)$' ]
```

**`soulseek.connection.timeout.connect` is `10000`, not 5000.** This is the first hard
evidence bearing on `[VERIFY]` 1.3 — see §7 below.

Live server state, `GET /api/v0/application`:

```
server.state = "Connected, LoggedIn"
```

## 6. The cron fleet — `--ok-codes` audit

28 active `cron_job.py`-wrapped entries. Classified by whether they declare ok-codes:

| Declares `--ok-codes` | Count | Jobs |
| --- | --- | --- |
| **Explicit** | **2** | `docker-prune` (`0`), `verify-runtime` (`0`) |
| **Implicit `0,1`** | **26** | everything else, including **`lidarr-jellyfin-bridge`** |

The Phase 5.1 premise is **CONFIRMED**: 26 of 28 jobs inherit a default that swallows
exit 1, and the stage-4 bridge is one of them. `cron_job.py`'s own docstring states the
default exists because `stack_watchdog.py` and `media_ops_status.py` legitimately exit 1 —
so the default is not arbitrary, but it is applied far beyond the two jobs that need it.

**Correction to the pipeline doc §9:** the `flock /tmp/nas-tubifarry-cleanup.lock` is held by
**five** jobs, not four — `:07`, `:22`, `:37`, `:52`, **and `05:30 process-soulseek-imports`**
(`flock -w 600`). `:52` uses `flock -w 120`; `:07`/`:22`/`:37` use `flock -n`.

**Correction to the pipeline doc §9:** `jellyfin_library_scan.py` is **not** a daily `05:05`
job. It is three weekly per-library jobs: Movies Fri 05:05, TV Sat 05:05, **Music Sun 05:05**.
The Music scan is deliberately after the 04:45 `album_art.py` cover backfill.

## 7. Baseline verification results

| Check | Command | Result |
| --- | --- | --- |
| Compose invariants | `make check` | **PASS** — "invariants hold: 49 assertions over 32 services, 0 warning(s)" |
| Containers | `docker compose ps` | 32/32 up; all with healthchecks report healthy |
| Lidarr health | `GET /api/v1/health` | `[]` |
| slskd Soulseek login | `GET /api/v0/application` | `Connected, LoggedIn` |
| Bridge cursor | `logs/lidarr_jellyfin_bridge.json` | `{"cursor": "2026-09-04T07:57:19Z"}` |

`make verify-runtime` was **not** run during baseline capture. It is gated behind
`VERIFY_NOTIFY ?= 0` (Makefile:209) and only pushes when `VERIFY_NOTIFY=1`, which the 06:15
cron line sets — so the Phase-level worry that it "cries wolf" on manual runs is
**already addressed upstream of this work**. No `--no-alert` flag needs adding.

## 8. Bridge implementation baseline

`scripts/lidarr_jellyfin_bridge.py`, 348 lines.

```python
DEFAULT_MAP_FROM = ("/data/music", "/music")   # longest wins
DEFAULT_MAP_TO   = "/data/movies/music"
FILE_EVENTS      = {"trackFileImported", "trackFileRenamed",
                    "trackFileRetagged", "trackFileDeleted"}
PATH_FIELDS      = ("importedPath", "path", "sourcePath")
HISTORY_PAGE_SIZE = 200
MAX_HISTORY_PAGES = 10
DEFAULT_FIRST_RUN_MINUTES = 30
```

State of the Phase 3 proposals against the code as it stands:

| Phase 3 item | Actual state |
| --- | --- |
| 3.4 "page until caught up" | **ALREADY DONE.** `fetch_history` pages backwards until it passes the cursor, bounded by `MAX_HISTORY_PAGES=10` (2000 records). The "single `pageSize=1000` grab" the prompt describes is not this code. |
| 3.4 use `id` not `date` | **OPEN.** Cursor is an ISO date (`2026-09-04T07:57:19Z`) and `changed_folders` advances it with `max(cursor, date)`. |
| 3.4 atomic cursor write | **OPEN.** `save_cursor` is a bare `state_path.write_text(...)`. |
| 3.4 `cursor.schema_version` | **OPEN.** State file is `{"cursor": "..."}` only. |
| 3.3 confirm effect before advancing | **OPEN.** Cursor advances on POST success. |
| 3.2 two-tier dispatch | **OPEN.** Single `POST /Library/Media/Updated` batch. |
| 3.2 `UpdateType` field | **OPEN.** Not sent. |
| 3.5 `artistFolderImported` | **OPEN.** Not in `FILE_EVENTS`. |
| Exit 2 on unmappable | **PRESENT** — §10-protected, unchanged. |

## 9. Open `[VERIFY]` items after Phase 0

| # | Claim | Status after baseline |
| --- | --- | --- |
| 1.3 | Soulseek login handshake "hardcoded 5000 ms" | **Partially refuted.** `soulseek.connection.timeout.connect` is explicitly `10000` in our config. Whatever produces the 5000 ms figure, it is **not** this setting. Source still to be found before the claim is rewritten. |
| 3.5 | `artistFolderImported` fires on this setup | **Unresolved.** Needs a history query for the event type. |
| 3.7 | 10.11.x rescans the whole library on any new item | **Unresolved.** We are on 10.11.11; must be measured here. |
| 4.6 | Tubifarry QueueCleaner overlaps `lidarr_queue_unstick.py` | **Refuted for this box.** QueueCleaner is a 2.2.x feature; we run **2.1.1.0**. Not adoptable without a GUID-changing migration. |
| 4.1/4.2/4.3 | slskd native features not yet adopted | **Largely refuted.** `retention`, `shares.cache.retention`, and download permissions are already configured. Only `transfers.download.retry`, webhooks/scripts, and metrics remain unadopted. |

