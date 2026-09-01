# Jellyfin playback audit — Fargo stutter (2026-08-31 → 2026-09-01)

> **Revision history** (kept per standing instructions — corrections earn
> their space):
>
> - **Pass 1**: enabled VAAPI hardware transcoding, declared it "the fix."
>   Wrong — the session never encoded video at all (`-codec:v:0 copy`,
>   direct stream; only DTS audio was transcoded). VAAPI accelerates neither.
> - **Pass 2**: found a real, recurring OOM-kill (kernel killing the
>   `jellyfin` process at ~22-24GB anon-RSS every few hours) but stopped at
>   "activity-proportional, mechanism unknown."
> - **Pass 3**: proposed H1 — a single Lidarr write cascading into a broad
>   Jellyfin real-time-monitor over-reaction, matching jellyfin/jellyfin
>   #16729. Applied mitigations (real-time monitoring off on all 3
>   libraries, NFO saving off).
> - **Pass 4**: ran the controlled experiment pass 3 proposed. It did not
>   reproduce #16729. Withdrew the #16729 attribution.
> - **Pass 5**: the corrected memory sampler caught a real, large
>   memory-growth event live. Two heap dumps showed the managed GC heap
>   flat at ~226-235MB while `anon`-RSS swung 217MB → 1.46GB → 573MB — the
>   growth is overwhelmingly native/unmanaged memory, not a managed leak.
> - **Pass 6**: tested H3 (glibc malloc arena fragmentation,
>   dotnet/runtime#122027) with an A/B: `MALLOC_ARENA_MAX=2` cut peak memory
>   by ~25% and finished the identical load *faster*. Confirmed the
>   `memfd:doublemapper` lead as a genuinely separate contributor.
> - **Pass 7**: closed out the standing task list — W^X A/B (kept),
>   reverse-proxy hygiene, heap dump housekeeping, prototyped the AAC
>   fallback track. Reported Sonarr's import notification as verified and
>   Radarr's/Lidarr's as "blocked on stalled downloads, not a config
>   failure."
> - **Pass 8 (this revision)**: **corrects pass 7's central conclusion.**
>   The *arr → Jellyfin "Update Library" notifications were not merely
>   unverified for Radarr and Lidarr — they were *broken for all three*, in
>   a way pass 7's verification method could not have detected, and the
>   Radarr/Lidarr "blocked on downloads" framing was wrong (both were
>   forceable in minutes). Details in §3.1. Also: built the alerting that
>   did not exist, moved library scanning to per-library cron and disabled
>   the global task, converted the first AAC batch, fixed Bazarr's failing
>   providers, and found two further silent failures nobody had noticed
>   (§3.3).
> - **Pass 9 (this revision)**: closed the three loose ends pass 8 left and
>   reversed one of its own recommendations. Restarted `autoheal` after
>   establishing *why* it was stopped (and fixed two timeout defects that
>   would have made it harmful). Generalised the two silent-failure findings
>   into a mechanism rather than two more fixes: every cron job is now wrapped
>   so both its failures *and its silences* are pushed, and the crontab itself
>   is linted continuously. Added the off-box heartbeat. And on the AAC
>   rollout, Tom reversed direction on the evidence pass 8 gathered — see
>   §3.6.

---

## 1. Root cause (confirmed, holds)

Two independent native-memory contributors, both mitigated in pass 6/7 and
both still holding as of this pass:

1. **glibc malloc arena fragmentation** (dotnet/runtime#122027) — confirmed
   via A/B with the "Scan Media Library" task as load generator.
   `MALLOC_ARENA_MAX=2` cut peak memory from >2.08GB (still climbing, scan
   incomplete after 23.8min) to 1.56GB (scan complete in 10.8min) — faster
   *and* smaller, no downside found. glibc sizes arena count from the
   *host's* online CPU count (16 here), not the container's cgroup quota,
   so `mem_limit` alone cannot restrain it.
2. **`memfd:doublemapper` accumulation** — .NET's W^X double-mapping for
   JIT'd code (dotnet/runtime#89776 / #121455). Over 1,000 mappings at a
   fresh restart, growing independently of the arena cap.
   `DOTNET_EnableWriteXorExecute=0` eliminates them entirely (0 throughout
   vs. growing past 1,368) at no time cost.

**Still holding.** The sampler at the end of this pass reads
`anon=455.8MB  arena_regions=1  doublemapper=0`, and `journalctl -k` shows
the last kernel OOM kill of the `jellyfin` process at **2026-09-01 05:34:58**
(anon-rss 23,483,348 kB) — *before* the fixes went in. None since.

**Ruled out; do not revisit without new evidence:** hardware transcoding
(the session direct-streamed), ffprobe memory (jellyfin/jellyfin#16048 — no
ffprobe process in the kernel's per-task table at any of the five kills, and
the fix predates the installed `jellyfin-ffmpeg7 7.1.4-3`), the
real-time-monitor cascade regression (#16729 — tested to 20 simultaneous
writes, refreshes were correctly scoped 1:1), and a managed .NET heap leak
(two heap dumps, managed heap flat at 226-235MB while `anon` swung 217MB →
1.46GB → 573MB).

**Two measurement lessons that cost time and are still true:**
`memory.current` / `mem_peak` include page cache — a 7.71GB `mem_peak` was
observed that was almost entirely benign page cache from plugin activity.
The OOM-relevant figure is **`anon`**. And retention per event is not a
stable constant: three identical single-item tests retained 191, 72 and
62 MB. Use the library scan as a load generator when you need
reproducibility.

---

## 2. Consolidated change log — everything live, all passes

| # | Change | Where | Revert |
|---|---|---|---|
| 1 | `devices: [/dev/dri:/dev/dri]`, `group_add: ["991"]` (host `render` GID), VAAPI hardware transcoding enabled | `docker-compose.yml` (jellyfin) | Remove `devices`/`group_add`, set `HardwareAccelerationType: none` via Jellyfin API |
| 2 | Jellyfin encoding config: `HardwareDecodingCodecs` corrected to match real `vainfo` output, `AllowHevcEncoding`/`AllowAv1Encoding`/low-power-encoder settings off | Jellyfin `/System/Configuration/encoding` | `POST` prior JSON from `docs/jellyfin-config-backups/` |
| 3 | SWAG `jellyfin.subdomain.conf`: `proxy_buffering off`, `proxy_request_buffering off` | `.docker-config/swag/nginx/proxy-confs/jellyfin.subdomain.conf` | Restore `.bak-20260901-133132` |
| 4 | Jellyfin `KnownProxies`: `[] → ["172.30.0.0/24"]` | Jellyfin `/System/Configuration/network` | `POST` with `KnownProxies: []` |
| 5 | `mem_limit: 10g` / `memswap_limit: 10g` | `docker-compose.yml` (jellyfin) | Delete both lines |
| 6 | Real-time monitoring off on all 3 libraries | Jellyfin `/Library/VirtualFolders/LibraryOptions` (×3) | Flip `EnableRealtimeMonitor` back to `true`, then **restart the container** — the API-only toggle does not apply live |
| 7 | NFO metadata saving off (Music, TV Shows) | Jellyfin `/Library/VirtualFolders/LibraryOptions` (×2) | Flip `SaveLocalMetadata` back to `true` |
| 8 | OpenCL Docker Mod tried, reverted (needs `CAP_FOWNER`, declined) | — | Already reverted |
| 9 | `scripts/jellyfin_mem_sample.py` (v3) + per-minute cron | `scripts/`, crontab | Remove cron line, delete script |
| 10 | `MALLOC_ARENA_MAX=2` | `docker-compose.yml` (jellyfin) | Delete the line |
| 11 | `DOTNET_EnableWriteXorExecute=0` | `docker-compose.yml` (jellyfin) | Delete the line |
| 12 | Dedicated Jellyfin API key for *arr integrations (`API_KEY_JELLYFIN_ARR`, Jellyfin key name `arr-integrations`) | `.env` | Delete the var; revoke in Jellyfin Dashboard → API Keys |
| 13 | Sonarr → Jellyfin "Update Library" notification | Sonarr `/api/v3/notification` id 1 | Delete the "Jellyfin" connection in Sonarr |
| 14 | Radarr → Jellyfin "Update Library" notification | Radarr `/api/v3/notification` id 2 | Delete the "Jellyfin" connection in Radarr |
| 15 | Lidarr → Jellyfin "Update Library" notification | Lidarr `/api/v1/notification` id 6 | Delete the "Jellyfin" connection in Lidarr |
| 16 | qBittorrent stale-lockfile recovery (found crashed, blocking all *arr downloads) | Host filesystem + `docker start` | N/A — repair, not new behaviour |
| 17 | SWAG: scoped `api_key`/`apikey` redaction in Jellyfin's access log | `.docker-config/swag/nginx/site-confs/00-jellyfin-log-redact.conf` + one `access_log` line in `jellyfin.subdomain.conf` | Delete both, `nginx -t && nginx -s reload` |
| 18 | Heap dumps moved off `/tmp` | `/mnt/drive/backups/jellyfin-heap-dumps/` | Delete the `.gcdump` files there |
| **19** | **Removed duplicate Jellyfin connections.** Radarr and Lidarr each had *two* — a long-standing "Emby / Jellyfin" (id 1) plus the one pass 7 added. The id-1 definitions never fired at all (verified: two connections, one inbound POST) | Radarr/Lidarr `notification` id 1, deleted | Re-add via each app's UI; pre-delete JSON is not in the repo (it contained masked keys and was worthless for restore) |
| **20** | **`mapFrom`/`mapTo` set on Sonarr's and Radarr's Jellyfin connection** (`/tv` → `/data/movies/series`, `/movies` → `/data/movies/movies`). Without this the notification is a silent no-op for anything Jellyfin does not already know — see §3.1 | Sonarr `notification/1`, Radarr `notification/2` | Blank both fields in each app's Connect settings |
| **21** | **`scripts/lidarr_jellyfin_bridge.py` + cron `2-59/5`** — the same path mapping for Lidarr, which exposes no `mapFrom`/`mapTo` fields | `scripts/`, crontab, `logs/lidarr_jellyfin_bridge.json` | Remove the cron line; delete the script and its state file |
| **22** | **Global "Scan Media Library" task disabled** — trigger list emptied (was `IntervalTrigger` 432000000000 ticks = every 12h) | Jellyfin `POST /ScheduledTasks/7738148ffcd07979c7ceb148e06b3aed/Triggers` with `[]`; persisted at `.docker-config/jellyfin/ScheduledTasks/7738148f-….js` | `POST` the same endpoint with `[{"Type":"IntervalTrigger","IntervalTicks":432000000000}]` |
| **23** | **`scripts/jellyfin_library_scan.py` + three cron entries** — Movies Fri 05:05, TV Shows Sat 05:05, Music Sun 05:05 (Music deliberately after `album_art.py` at 04:45) | `scripts/`, crontab | Remove the three cron lines and re-enable row 22 |
| **24** | **`ntfy` service added to the stack** — self-hosted, `auth-default-access=deny-all`, user `watchdog` with `rw` on topic `nas-alerts`, published at `ntfy.${PUBLIC_DOMAIN}` | `docker-compose.yml`, `.docker-config/swag/nginx/proxy-confs/ntfy.subdomain.conf`, `${CONFIG_DIRECTORY}/ntfy` | `docker compose stop ntfy && docker compose rm -f ntfy`; delete the service block, the vhost, and the config dir |
| **25** | **`scripts/stack_watchdog.py` + cron `*/5`** — container health/exit/missing/flapping, Jellyfin `anon` threshold, kernel OOM watcher, ntfy delivery | `scripts/`, crontab, `logs/stack_watchdog.json` | Remove the cron line; delete the script and its state file |
| **26** | **`NAS_ALERT_WEBHOOK` / `NAS_ALERT_USER` / `NAS_ALERT_PASSWORD`** added to `.env` (and placeholders to `.env.example`) | `.env` | Delete the three lines |
| **27** | **`media_ops_status.py` cron repaired** — the line ran `.venv/bin/python` with no `cd`, so from cron's `$HOME` it resolved to a path that does not exist. The ops dashboard had been serving 2026-06-10 data ever since | crontab | Not worth reverting — without the `cd /home/tom/nas &&` prefix the job simply does not run |
| **28** | **Bazarr: `addic7ed` and `opensubtitlescom` removed from `enabled_providers`** — both in permanent `AuthenticationError`. Credentials left in place, so re-enabling is a UI toggle | Bazarr `POST /api/system/settings`; `.docker-config/bazarr/config/config.yaml` | Re-add both names to `enabled_providers`; backup at `docs/jellyfin-config-backups/bazarr-config-*-pre-provider-disable.yaml` |
| **29** | **SWAG `default.conf`: load-order comment** above the inline `include /config/nginx/proxy-confs/*.subdomain.conf;` | `.docker-config/swag/nginx/site-confs/default.conf` | Restore `default.conf.bak-20260901-*` |
| **30** | **AAC fallback audio, first batch (Fargo Season 1, 10 files)** + `scripts/aac_fallback_track.py` | `${SHARE_DIRECTORY}/series/Fargo/Season 1`; originals at `${SHARE_DIRECTORY}/backups/aac-remux-originals/` | `mv` each original back over the converted file, then run `jellyfin_library_scan.py --library "TV Shows"` |
| **31** | Jellyfin request logging (`.docker-config/jellyfin/logging.json`) — added for §3.1, **fully reverted**: file deleted and the container restarted 2026-09-01 20:42, confirmed no further `Request starting` lines | `.docker-config/jellyfin/logging.json` | Already reverted; §4.1 says how to re-add it |
| **32** | `scripts/jellyfin_mem_sample.py`: `datetime.timezone` → `datetime.UTC` (the file's only `ruff` failure) | `scripts/jellyfin_mem_sample.py` | Cosmetic; revert with `git checkout` once committed |
| **33** | **`autoheal` restarted.** Stopped since 2026-07-29; cause established as collateral from a bare `docker compose stop` (shell history), not a decision about autoheal — its own log shows it had never restarted anything | `docker compose up -d autoheal` | `docker compose stop autoheal` |
| **34** | **`AUTOHEAL_DEFAULT_STOP_TIMEOUT=150`** (was the image default of 10s). autoheal ignores compose's `stop_grace_period`; a 10s SIGTERM→SIGKILL on qbittorrent is the documented stale-lockfile trigger | `docker-compose.yml` (autoheal) | Delete the line (reverts to 10s) |
| **35** | **`CURL_TIMEOUT=180`** (was 30). Must exceed the stop timeout, or autoheal logs a spurious failure and re-issues the restart every interval on top of the one in flight — measured, three overlapping requests | `docker-compose.yml` (autoheal) | Set back to 30 |
| **36** | **`scripts/cron_job.py`** + all 23 scheduled jobs rewritten to use it. Pushes fatal exits; records `logs/cron-state/<job>.json` so the watchdog can alert on silence | crontab, `scripts/`, `logs/cron-state/` | Restore the crontab from `docs/jellyfin-config-backups/crontab-2026-09-01-pre-cron-wrapper.txt` |
| **37** | **`scripts/heartbeat.py`** + cron `*/10` + `NAS_HEARTBEAT_URL` in `.env`/`.env.example`. Off-box dead-man's switch; the URL is still empty pending an account | crontab, `scripts/`, `.env` | Remove the cron line and the env var |
| **38** | **Watchdog gained four checks**: `autoheal` health, off-box heartbeat configured, crontab lint, wrapped-cron-job freshness | `scripts/stack_watchdog.py` | Delete the four `check_*` calls in `main()` |
| **39** | **`media_ops_status` cron no longer discards output** — it redirected to `/dev/null`, which is part of why three months of failure left nothing to find. Now `>> logs/media_ops_status.log` | crontab | Restore `>/dev/null 2>&1` (do not) |
| **40** | **AAC disposition flip, 5 files** (Planet Earth S01 E01/E02/E04/E07/E09) — already had a stereo AAC track, only the default flag moved. Lossless, no re-encode, no size change | `${SHARE_DIRECTORY}/series/Planet Earth/Season 1`; originals under `backups/aac-remux-originals/` | `mv` each original back, then `jellyfin_library_scan.py --library "TV Shows"` |
| **41** | **`aac_fallback_track.py` gained auto-detected flip mode + `--flip-only`** — a file that already has a browser-safe track only needs the flag moved, not an encode | `scripts/aac_fallback_track.py` | n/a, additive |
| **42** | **`.sudo-pwd` added to `.gitignore`** — it was untracked and unignored in the repo root, one `git add -A` from a sudo password in the history | `.gitignore` | Remove the line (do not) |
| **43** | **Bazarr config backup moved out of the repo** to `/mnt/drive/backups/nas-config-backups/` (mode 600) — it carries live provider passwords, the Bazarr API key and the flask secret, and was staged for commit | `docs/jellyfin-config-backups/` → `/mnt/drive/backups/nas-config-backups/` | Move it back (do not) |
| **46** | **Nine services wired to ntfy**, split across two topics by signal: `nas-alerts` (act on it) and `nas-media` (nice to know). Sonarr/Radarr/Lidarr/Prowlarr via their native Ntfy connection, Jellyseerr via its webhook agent, Bazarr via Apprise, Watchtower via shoutrrr, plus the host watchdog. Every one verified by real delivery, not by a Test button returning 200 | each app's own config; `nas-media` topic; `NTFY_ARR_*` in `.env` | Delete the `ntfy — *` connections in each *arr, disable Jellyseerr's webhook and Bazarr's `ntfy` notifier, drop `WATCHTOWER_NOTIFICATION_URL` |
| **45** | **ntfy hardened properly**: `NTFY_ENABLE_LOGIN=true` (the web UI could not authenticate at all on a deny-all server), Web Push enabled with a VAPID keypair, and three least-privilege accounts replacing one shared read-write login — `watchdog` write-only, `phone` read-only, `admin` for the UI. `NTFY_UPSTREAM_BASE_URL` deliberately **not** set: it exists only to wake iOS via ntfy.sh's APNs relay and would send a hash of every topic off-box; Android needs no relay | `docker-compose.yml` (ntfy), `.env`, ntfy `user.db` | Drop the new env lines and `docker exec ntfy ntfy access watchdog nas-alerts rw` to go back to one shared account |
| **44** | **`ruff check scripts` backlog cleared** — 137 findings across 10 pre-existing files, 132 auto-fixed, 3 by hand. CI's lint gate is green | `scripts/` (10 files) | `git revert` the style commit |

---

## 3. This pass, item by item

### 3.1 — *arr → Jellyfin notifications: all three now verified, and all three were broken

Pass 7 called Sonarr "verified" on the strength of a `LibraryMonitor …
will be refreshed` line ~60s after a real import. That reasoning was sound
but the evidence was one line short, and the missing line was the one that
mattered.

**The verification method, done properly.** `LibraryMonitor` emits that
line whether the refresh came from the filesystem watcher or from an
external call, so it cannot by itself attribute the refresh. The fix is to
capture the *inbound request*, which Jellyfin will log if ASP.NET Core's
hosting diagnostics are turned up. Full technique in the runbook (§4.1).
The resulting evidence is a **two-line chain**, and both lines are
necessary:

```text
[19:06:17.083] Microsoft.AspNetCore.Hosting.Diagnostics: Request starting
    "HTTP/1.1" "POST" "http"://"jellyfin:8096""""/Library/Media/Updated""" - "application/json" 110
[19:07:17.086] Emby.Server.Implementations.IO.LibraryMonitor:
    "Furious" ("/data/movies/series/Furious") will be refreshed.
```

The `Host` header (`jellyfin:8096`, not `localhost:8096`) identifies it as
an internal *arr call rather than anything of mine, and the ~60s gap is
LibraryMonitor's debounce.

**What that exposed: the notification was reaching Jellyfin and doing
nothing.** Radarr's very first forced import produced a clean `POST … → 204`
— and no refresh, and no movie in Jellyfin. The cause is a path-namespace
mismatch:

| App sees | Jellyfin sees |
|---|---|
| `/tv/…` | `/data/movies/series/…` |
| `/movies/…` | `/data/movies/movies/…` |
| `/music/…` | `/data/movies/music/…` |

`POST /Library/Media/Updated` hands each reported path to LibraryMonitor,
which resolves it to the library containing it. A path under no library is
dropped — **and the endpoint still answers 204**. That 204 is what made the
breakage invisible for as long as it existed.

Proven directly, same endpoint, same server, one variable:

```text
POST {"Updates":[{"Path":"/music/Bathory/1988 - Blood Fire Death"}]}
  -> 204, no LibraryMonitor line, Jellyfin keeps the stale metadata
POST {"Updates":[{"Path":"/data/movies/music/Bathory/1988 - Blood Fire Death"}]}
  -> 204, 'LibraryMonitor: "Blood Fire Death" … will be refreshed', updated
```

Why Sonarr *looked* fine in pass 7: the MediaBrowser connection first
queries `GET /Items?…` to find the item in Jellyfin, and when it finds one
it reports **Jellyfin's own path** for it. Furious already existed in
Jellyfin, so its path came back correct. A brand-new series would have
fallen through to the `/tv/…` path and silently done nothing — which is
precisely the case a library scan is supposed to stop being needed for.

**The fixes:**

- **Sonarr, Radarr** — set `mapFrom`/`mapTo` on the connection (change-log
  rows 20). Radarr re-tested immediately after: `POST` → 204 →
  `LibraryMonitor: "movies" ("/data/movies/movies") will be refreshed` →
  the test movie appeared. Sonarr re-tested after its change (the chain
  quoted above) so a working thing was not left unverified after edits.
- **Lidarr** — its MediaBrowser connection has **no `mapFrom`/`mapTo`
  fields at all** (its field list is host/port/useSsl/urlBase/apiKey/notify/
  updateLibrary and nothing else), so there is no in-Lidarr fix.
  `scripts/lidarr_jellyfin_bridge.py` does the mapping outside Lidarr: poll
  Lidarr's history for file events, take each album folder, translate the
  prefix, report it. First live run reported 9 changed album folders and
  Jellyfin refreshed all 9 within a second, targeted per album rather than
  re-walking the whole Music library.

**How Radarr and Lidarr were forced** (pass 7 said this was blocked on
stalled downloads; it was not):

- *Radarr* — added a disposable movie, generated a 3-second test file in
  the download folder, ran a real `ManualImport`, which fired the same
  `downloadFolderImported` path as an organic import. Movie, file and
  Jellyfin entry all removed afterwards; verified gone.
- *Lidarr* — a `RenameFiles` command on a single file Lidarr itself wanted
  renamed (`09 - [untitled].mp3` → `09 - The Winds of Mayhem (Outro).mp3`,
  a title it had learned from MusicBrainz). Same `Update()` code path as an
  import, no content modified, no disposable artist added — which mattered,
  because adding one risks the monitor-sweep/backlog-drip interaction that
  has caused slskd search-flood bans before.

**Also confirmed: why the Test button lies.** Radarr's log carries exactly
two `Warn|MediaBrowserProxy|Unable to send notification to Emby` lines from
pass 7's two Test presses. The `Test()` implementation calls the Emby-style
*notify* API — which Jellyfin does not implement — **unconditionally, even
with `notify: false`**, and never touches the library-update path. So a
green Test proves the host is reachable and nothing else.

**Incidental finding, not acted on:** Lidarr wants to rename
`/music/Blue Öyster Cult/1977 - Spectres/04 - Searchin’ for Celine.mp3` to
`04 - Searchin' for Celine.mp3` — and **both files already exist** in that
folder (smart-quote and ASCII duplicates, two pairs). Running Lidarr's
"Rename Files" on that artist would overwrite one with the other. Left
alone; flagged in §5.

### 3.2 — Library scanning moved to per-library cron

Jellyfin's "Scan Media Library" (`RefreshLibrary`) is a single global task —
there is no per-library scheduling — and it was running **every 12 hours**
across all three libraries. It is also the largest remaining memory event on
the box (1.56GB peak `anon` with the fixes in place).

Replaced with `scripts/jellyfin_library_scan.py`, which drives
`POST /Items/{virtualFolderItemId}/Refresh?metadataRefreshMode=Default&
imageRefreshMode=Default&replaceAllMetadata=false&replaceAllImages=false&
recursive=true` — the same call Jellyfin's own per-library "Scan library"
button makes. Verified it is a real scan, not a metadata-only touch: a file
copied straight onto disk was invisible to Jellyfin (`TotalRecordCount: 0`),
one run of the script made it appear at the right path.

Cadence, one library per day so two scans never overlap:

| Library | When | Why that cadence |
|---|---|---|
| Movies | Fri 05:05 | Radarr verified + path-mapped; scan is a safety net |
| TV Shows | Sat 05:05 | Sonarr verified + path-mapped; same |
| Music | Sun 05:05 | Lidarr covered by the bridge — but `album_art.py` writes `folder.jpg` straight to disk at Sun 04:45 where no *arr ever reports it, so this scan is what gets new cover art in |

**No `--wait` in the script, deliberately.** Verified on 10.11.11: a
per-item refresh does *not* drive the `RefreshLibrary` task's state (it
stays `Idle` throughout, polled every 3s for 30s) and `BaseItemDto` exposes
no refresh-progress field. There is no honest REST signal to poll, so the
script does not pretend to have one.

### 3.3 — Alerting: built, and it immediately had things to say

`scripts/stack_watchdog.py`, cron `*/5`, four checks:

1. **Every compose service** exists, is running and is not `unhealthy` —
   compared against `docker compose config --services`, so a service
   defined but never created is caught too.
2. **Restart churn** — a climbing `RestartCount` between runs, i.e.
   flapping even when the container happens to be "up" at check time.
3. **Jellyfin `anon`-RSS** ≥ 4GB (healthy is 0.4–1.3GB, the heaviest
   routine event peaked at 1.56GB, the cgroup limit is 10GB — so there is
   room to react before the kernel does). A stale (>15 min) or
   `SAMPLE_FAILED` sampler is itself an alert.
4. **Kernel OOM kills** from `journalctl -k` (`dmesg` is not readable
   unprivileged here, under `kernel.dmesg_restrict`).

**Delivery: ntfy, self-hosted.** One line of justification, as asked: a
plain `POST <topic-url>` with a text body is the entire publish contract, it
needs no server-side application or token setup before the first alert can
land, and this repo already spoke it (`SLSKD_ALERT_WEBHOOK` in
`slskd_login_watch.py`) — Gotify would require standing up a server *and*
minting per-application tokens before anything could be sent. Self-hosted
rather than ntfy.sh so alert contents never leave the box; only the phone's
subscription egresses, through SWAG. **Stated coverage limit:** the
watchdog and ntfy run on the same host, so neither can tell you the host
itself is down. That needs an off-box heartbeat and was deliberately not
built.

Verified end-to-end, not just "it returned 200": authenticated publish lands
in ntfy's cache with title/priority/tags intact, anonymous publish **and**
anonymous read are both refused `403`, and the same message is readable
through SWAG at `https://ntfy.<PUBLIC_DOMAIN>/nas-alerts` with the
credentials. The 48h message cache means an alert raised while the phone is
offline is still delivered on reconnect. Negative-tested too: forcing
`--jellyfin-anon-mb 1` raises the memory alert, and seeding the state cursor
to 05:00 today made it find the real 05:34:58 Jellyfin OOM kill in the
kernel log. 13 unit tests cover the container/memory/OOM logic against
synthetic input, since the healthy cases cannot be provoked safely.

**Two silent failures it found in its first hour, neither of which anyone
knew about:**

- **`autoheal` had not been running since 2026-07-29.** Its last startup
  banner before today is `2026-07-29T17:20`. For over a month nothing was
  restarting unhealthy containers. This is the exact case check 1 exists
  for, and the exact case a "watch the containers that are running" monitor
  would have missed.

  > **Reconciled by `nas-c0`, 2026-09-01 (supersedes the reading above).**
  > **autoheal is running again** — restarted during the qBittorrent work; do
  > not treat starting it as an open action.
  >
  > The cause was *not* an explicit stop. `docker inspect` at 18:20 read
  > `Status=exited ExitCode=6 RestartCount=5 Policy=on-failure:5`. That is a
  > capped-out crash loop, not an administrative stop, and the `RestartCount:
  > 0` / `unless-stopped` premise does not hold: `/usr/local/sbin/nas-restart-guard.sh`
  > was overwriting the compose file's `unless-stopped` with `on-failure:5`
  > every 15 minutes via `docker update`. autoheal failed five times, exhausted
  > that budget, and Docker gave up permanently.
  >
  > The distinction matters because it changes the fix. An explicit stop needs
  > no policy change; a capped-out loop needs the cap gone. The guard now
  > enforces `unless-stopped` instead, and both `autoheal` and `watchtower`
  > read `unless-stopped:0` as of 19:40. Origin of the cap, for the record:
  > `/home/tom/fix-nas-all.sh` installed it on 2026-06-15 as blast-radius
  > control for a watchtower crash loop that the *same script run* had already
  > fixed by disabling `WATCHTOWER_ROLLING_RESTART`. Background in
  > `docs/qbittorrent-crash-fix.md`.
- **The `media_ops_status.py` cron had been dead since 2026-06-10.** Its
  crontab line ran `.venv/bin/python …` with no `cd`, so from cron's
  `$HOME` it resolved to a path that does not exist. `ops-status.json` —
  the file behind the dashboard at `4eva.me/ops.html` — had an mtime of
  **Jun 10 11:06**. The dashboard has been showing June data for three
  months. Fixed (change-log row 27) and verified writing current data.

While fixing that, every crontab line was audited for the same class of
bug; no others were found.

**Not done here: the qBittorrent stale-lockfile automation.** A second
Claude Code session (`nas-c0`) was working in this repo concurrently and was
actively testing exactly that — it committed
`9c13b9e fix(qbittorrent): grant CAP_KILL so shutdown is actually graceful`
mid-pass. Scope was divided by message rather than both of us editing the
same compose file and cycling the same container; the qBittorrent item is
that session's. The qBittorrent flapping visible in `journalctl` between
18:10 and 18:22 is that session's testing, not an incident.

### 3.4 — AAC fallback audio: Fargo Season 1

Batch of ten, as specified. Fargo Season 1 is exactly 10 DTS-only episodes
(45.8GB), and it is the series the whole investigation started from.

Pass 7's lesson applied: **adding the track is not enough**, because
Jellyfin's StreamBuilder evaluates the *default* stream. Measured against
the live server with a Chrome device profile, on S01E01:

```text
before: DirectPlay=False DirectStream=False   audio: [dts 6ch DEFAULT]
after:  DirectPlay=True  DirectStream=True    audio: [dts 6ch, aac 2ch DEFAULT]
        TranscodingReasons=None
```

**Batch result — all ten, verified against Jellyfin's own decision engine:**

```text
E01..E10  DirectPlay=True  DirectStream=True  TranscodingReasons=None
          audio: [dts 6ch, aac 2ch DEFAULT]   (10/10)
originals 45.78 GB -> converted 46.83 GB   (+1.04 GB, +2.3%)
```

Originals preserved at
`${SHARE_DIRECTORY}/backups/aac-remux-originals/series/Fargo/Season 1/`,
all ten, untouched. Re-running the script over the same folder now reports
`skipped` for every file, so the idempotency check works.

`scripts/aac_fallback_track.py` does both halves — appends a stereo AAC
encode of the first audio track and moves the default disposition onto it —
copies every other stream untouched (`-map 0 -c copy`, so video, the
original DTS, subtitles, chapters and attachments are byte-identical),
stages the output *outside* the library (a half-written `*.mkv` in the media
folder is something Jellyfin or Sonarr could pick up mid-conversion),
re-probes the staged file before touching the original, and only then moves
the original to `${SHARE_DIRECTORY}/backups/aac-remux-originals/` with its
relative path preserved. Files whose default audio is already browser-safe
are skipped, so re-running is idempotent. ~2 minutes per episode.

**The trade-off, stated plainly because it is a real cost:** making the
stereo AAC track default means a client that *can* handle DTS 5.1 — the
living-room TV app — now gets stereo unless the viewer picks the surround
track. The DTS stream is still there, byte-identical and first in the file,
just no longer flagged default. There is no way to have both: the default
flag is exactly what Jellyfin's decision turns on.

**Scope for later is much larger than "DTS", and the briefing's framing
understated it.** Surveying every item's *default* audio stream against what
a browser can Direct Play:

| | already fine | disposition flip only | needs an AAC track |
|---|---|---|---|
| Movies | 10 | 0 | **13** |
| Episodes | 139 | 5 | **933** |

DTS is only 164 of those — the bulk is **AC3 and E-AC3**, which browsers
cannot Direct Play either. So this is not a Fargo-shaped problem with a
small tail; converting the whole library would be ~946 files at ~2 min each,
around 32 hours of wall clock. The 5 disposition-flip-only episodes are
nearly free and worth doing regardless.

### 3.5 — Small items

- **Bazarr.** Both failing providers had credentials configured; neither
  was fixable from `.env` (no credentials there for either). The log gives
  two different causes: `opensubtitlescom` → `'Login failed'` (the stored
  username/password is rejected — commonly an opensubtitles**.com** account
  vs a legacy .org one), `addic7ed` → `'cookies not valid anymore'` (the
  stored `__cf_bm` Cloudflare cookie dates from 2026-05-24 and those live
  ~30 minutes). Both removed from `enabled_providers`; the remaining eight
  all report `Good`. Credentials deliberately left in place so re-enabling
  is a toggle once Tom has fresh ones.
- **SWAG `default.conf`.** Comment added above the inline
  `include …/*.subdomain.conf;` explaining the alphabetical load-order
  constraint and why `00-jellyfin-log-redact.conf` is named that way. It
  also documents the *other* way that file bites, which cost time again
  this pass: `proxy.conf` already sets `proxy_http_version 1.1` and 240s
  `proxy_read/send/connect_timeout`, so re-declaring any of them in a
  vhost is `nginx: [emerg] … directive is duplicate`, not an override.

### 3.6 — Pass 9: the three loose ends, and one reversal

**`autoheal` — why it was stopped, then restarted.** Established before
touching it, because starting a supervisor that had been deliberately silenced
would reintroduce whatever silenced it. Three lines of evidence say it was not
a decision about autoheal at all:

* its **entire** log is 42 lines, all startup banners — in five weeks of
  running it never restarted anything, so it cannot have been stopped for
  misbehaving;
* shell history contains a bare `docker compose stop` (which stops every
  service) followed by `docker compose start qbittorrent slskd` — only those
  two came back. The dates fit: last autoheal banner 2026-07-29T17:20;
* nothing in git log, `AGENTS.md`, `CLAUDE.md` or `docs/` says to keep it
  down, and `CLAUDE.md` actively documents it as part of the design.

It supervises only `qbittorrent` and `slskd`, and slskd's healthcheck is the
Soulseek-*independent* web-UI spider `CLAUDE.md` mandates — so the documented
restart-spiral hazard does not apply. Started.

**Two defects found while proving it works**, both of the same shape as the
`WATCHTOWER_TIMEOUT` fix already in the compose file:

1. `AUTOHEAL_DEFAULT_STOP_TIMEOUT` was the image default of **10s**. autoheal
   ignores compose's `stop_grace_period` entirely, so a restart of qbittorrent
   would SIGKILL it after 10s — precisely the ungraceful kill that leaves the
   stale lockfile. Set to 150s.
2. With that raised, a deliberately-unhealthy probe container showed
   `Restarting container … failed` every 30s. That is `CURL_TIMEOUT=30`
   cutting off a call that blocks for the whole stop timeout. The restart
   *does* still complete — the probe restarted at exactly t+150s — but
   autoheal believed it had failed and fired **three overlapping restart
   requests** in the meantime. For qbittorrent that is the pile-up that
   creates the very lockfile it is trying to recover from. `CURL_TIMEOUT=180`.

With both fixed, the probe was detected and restarted cleanly with no failure
lines. Also verified in passing, because the env listing suggests otherwise:
`dockerproxy` has `ALLOW_RESTARTS=0`, yet `POST /containers/<id>/restart`
through it returns **204**. `POST=1` is what actually gates it in this build.
Behaviour beats the env var; the compose comment claiming "CONTAINERS + POST is
all autoheal needs" is correct.

**Closing the class, not the two instances.** `autoheal` dead a month,
`media_ops_status.py` dead three months — both "a thing that should be running
isn't", and auditing the crontab for one specific bug only covers jobs that
fail to *start*. Three mechanisms now, not two more fixes:

* **`scripts/cron_job.py`** wraps all 23 jobs. A fatal exit pushes an ntfy
  alert with the job name and stderr tail. Every run records
  `logs/cron-state/<job>.json`, and the watchdog alerts when a job has not
  *succeeded* within the window its own cron line declares — that is the half
  that catches a job producing nothing at all. `--register` seeds the file so
  a never-yet-run job is watched from the moment it is scheduled.
  The exit-code contract matters: 1 means *partial* in this repo and several
  scripts report real findings that way, so `--ok-codes` defaults to `0,1`.
  Treating non-zero as failure would have alarmed constantly and been turned
  off within a week.
* **A crontab lint in the watchdog**, run every 5 minutes: a line using a
  relative path without `cd /home/tom/nas`, or naming a script that does not
  exist, is now a critical alert. Verified against the historical bad line —
  it flags it — and against the live crontab, which is clean.
* **`scripts/heartbeat.py`**, `*/10`, for the failure nothing on this host can
  report: the host. It pings an off-box dead-man's switch, and pings `/fail`
  instead if `stack_watchdog` has itself gone quiet, so a live box with dead
  monitoring cannot keep the light green — the same circular gap that hid
  `autoheal`.

Verified: hc-ping.com is reachable from this box; a wrong ping URL exits 2 with
`HTTP 400 — is the ping URL right?`; a DNS failure exits 2; a real reachable
endpoint returns 0. **Still needs an account** — creating the check is not
something a script can do, so `NAS_HEARTBEAT_URL` is empty and the watchdog
raises a standing `heartbeat:unconfigured` warning until it is set. That nag is
deliberate: this is the one remaining hole in coverage and it should keep
saying so.

Seven crontab lines lost their `cd /home/tom/nas` while I was writing passes 8
and 9 — three in pass 8, four in pass 9 — and the audit caught every one. That
is the argument for the lint, made at my own expense: this bug is easy to
introduce, invisible once introduced, and I introduced it seven times in an
afternoon while actively thinking about it.

**AAC: rollout declined, and the survey is why.** Pass 8 gathered the numbers
to justify a rollout; the numbers argued against one. 946 files need
conversion, mostly **AC3 and E-AC3** rather than DTS — so the real finding is
not "some files have awkward audio" but "most of this library cannot be Direct
Played *by a browser*", and native clients handle all of it untouched. 32
hours of conversion and a permanent stereo-default cost on every file, versus
using the app. Tom's call, and the right one.

Done instead: **the 5 disposition-flip-only episodes** (Planet Earth S01
E01/E02/E04/E07/E09). Those already carried a stereo AAC track and were
transcoding purely because DTS held the default flag. No encode, no size
change, no trade-off — `aac_fallback_track.py` now detects this case
automatically and `--flip-only` restricts a run to it. All five verified
`DirectPlay: False → True` through the live StreamBuilder.

**The 5.1 question, answered — and the answer is no.** If a 5.1 AAC default
track direct-played, future conversions could keep surround and the
stereo-default cost would vanish. Built one (Fargo S01E01, DTS 5.1 → AAC 5.1
448k, flagged default) and asked the real StreamBuilder. The first result was
`DirectPlay: True`, which is close to meaningless: my device profile placed no
channel constraint on direct play. Re-tested with the `CodecProfile` that
jellyfin-web actually builds from the browser's reported output channels:

```text
client reports max 2 channels -> DirectPlay=False DirectStream=False
client reports max 6 channels -> DirectPlay=True  DirectStream=True
```

So a 5.1 AAC default **transcodes on any browser whose audio output is
stereo** — which is the entire population this exercise exists to serve. It
does not remove the trade-off, it relocates it. A listening test would not have
found this, because on a stereo device the decision never reaches Chrome's
decoder at all; the file is transcoded server-side first. Stereo AAC as default
stands. Fargo S01E01 was restored to the stereo version and re-verified.

---

## 4. Runbook

### 4.1 Prove an *arr's "Update Library" actually works

Do not trust the Test button (§3.1) and do not trust a `LibraryMonitor`
line alone. Capture the inbound request.

1. Write `.docker-config/jellyfin/logging.json` — a copy of
   `logging.default.json` with one extra line in the `Override` block:

   ```json
   "Microsoft.AspNetCore.Hosting.Diagnostics": "Information",
   ```

   (`Microsoft.AspNetCore.Routing.EndpointMiddleware` at `Information` too
   if you also want the controller name.)
2. `docker restart jellyfin`. **The file is not hot-reloaded** — verified;
   without a restart nothing changes.
3. Force a real import. Radarr/Sonarr: `ManualImport` of a file already on
   disk. Lidarr: a `RenameFiles` command on a file Lidarr already wants
   renamed (`GET /api/v1/rename?artistId=N` lists them — **check the target
   name does not already exist**, see §3.1).
4. Watch for the two-line chain in
   `.docker-config/jellyfin/log/log_<date>.log`:

   ```text
   Request starting "HTTP/1.1" "POST" "http"://"jellyfin:8096""""/Library/Media/Updated"""
   … ~60s later …
   LibraryMonitor: "X" ("/data/movies/…") will be refreshed.
   ```

   The `Host` is `jellyfin:8096` for an internal *arr call. **If the first
   line appears and the second does not, the notification is a no-op** —
   check the path mapping (§3.1). Lidarr posts to the legacy
   `/mediabrowser/Library/Media/Updated` prefix; Jellyfin accepts it.
5. `rm .docker-config/jellyfin/logging.json && docker restart jellyfin`.
   Leaving it on is cheap — measured at 5,658 bytes/min, ~2 MB over nine
   hours, mostly healthchecks — so if a restart is inconvenient, deleting
   the file and letting the next restart pick it up is fine.

### 4.2 Take a Jellyfin heap dump without extra capabilities

`CAP_SYS_PTRACE` and `CAP_FOWNER` are deliberately not granted. Copy the
tooling into the container's own filesystem instead of attaching from
outside: `docker cp` `dotnet-gcdump` into `/tmp` inside the container, run
it there against PID 1, `docker cp` the `.gcdump` back out. Full dumps live
at `/mnt/drive/backups/jellyfin-heap-dumps/`; committed text summaries are
in `docs/jellyfin-heap-dumps/`.

### 4.3 Read the memory sampler correctly

`logs/jellyfin-mem.log`, one line per minute. **`anon` is the only number
that predicts an OOM kill.** `mem_current` and `mem_peak` include page
cache and read high for entirely benign reasons — a 7.71GB `mem_peak` was
observed that was almost all page cache. `arena_regions` and `doublemapper`
should read ~1 and 0 respectively; if `doublemapper` starts climbing, the
`DOTNET_EnableWriteXorExecute=0` env var has been lost.

### 4.4 Alerting

- Test delivery: `python scripts/stack_watchdog.py --self-test`.
- See what it would say without notifying: `--dry-run`.
- **Phone setup (Android):** install the ntfy app → *Settings → Manage users →
  Add user*, server `https://ntfy.<PUBLIC_DOMAIN>`, username/password from
  `NTFY_PHONE_USER` / `NTFY_PHONE_PASSWORD` in `.env`. Then *Subscribe to
  topic* → `nas-alerts`, and switch the server from *ntfy.sh* to
  *Use another server* → the same URL. DNS and the wildcard cert already cover
  the subdomain.
- **Browser:** open `https://ntfy.<PUBLIC_DOMAIN>`, sign in as
  `NTFY_PHONE_USER`, subscribe to `nas-alerts` and allow notifications when
  prompted. Web Push is enabled, so it notifies with no tab open.
- **Three accounts, deliberately not one.** `watchdog` publishes and cannot
  read (a leak of this box's `.env` exposes no alert history); `phone` reads
  and cannot publish (the credential on a phone, backed up to Google, cannot
  inject fake alerts); `admin` is for the web UI and user management. All nine
  permission boundaries were verified — see the change log.
- Silence a service the watchdog should not care about: add
  `--ignore <service>` to the cron line.

### 4.5 Which service notifies what

Two topics, and the split is the whole point — routing informational events at
the alerts topic would bury the failures the alerting exists to surface.

| source | topic | fires on |
|---|---|---|
| `stack_watchdog.py` | `nas-alerts` | container down/unhealthy/missing, restart churn, Jellyfin memory, kernel OOM, autoheal, crontab lint, stale cron jobs |
| `cron_job.py` | `nas-alerts` | any wrapped cron job exiting fatal |
| Sonarr / Radarr | `nas-alerts` | health issue, health restored, manual interaction required |
| Sonarr / Radarr | `nas-media` | import complete, upgrade |
| Lidarr | `nas-alerts` | health, **download failure, import failure** |
| Prowlarr | `nas-alerts` | health issue / restored |
| Jellyseerr | `nas-alerts` | request pending, request failed, issue created |
| Bazarr | `nas-media` | subtitle events |
| Watchtower | `nas-alerts` | image updates and failures, one digest per run |

**Lidarr publishes nothing to `nas-media` on purpose.** It does hundreds of
music imports a day; those on a phone would drown everything else.

Adding another service: publish to `http://ntfy:8410` from inside
`nas-network` with the write-only `NTFY_ARR_*` credential. If the service
cannot set an `Authorization` header, ntfy takes
`?auth=<base64url("Basic "+base64(user:pass))>` as a query parameter, or
`user:pass@host` URL userinfo — both verified against this deny-all instance.

**Not wired, deliberately:** Jellyfin (no webhook plugin installed, and its
health is already covered far better by the watchdog — memory, OOM and
container state); qBittorrent (its "run program on completion" would duplicate
Sonarr/Radarr's import notifications); slskd, Nextcloud, recyclarr (no useful
notification surface).

### 4.6 Why is this transcoding? Usually: use a native client

For anything whose default audio is AC3, E-AC3, DTS or TrueHD — which is most
of this library — a browser cannot Direct Play it and Jellyfin will remux the
container and convert the audio. **Jellyfin Media Player, Findroid and Infuse
all play those untouched.** That is the answer for the general case; converting
files is the exception, for something specific that gets watched in a browser
regularly.

If you do want to convert a batch:

```bash
. .venv/bin/activate
python scripts/aac_fallback_track.py --root "$SHARE_DIRECTORY/series/X"          # dry run
python scripts/aac_fallback_track.py --root ... --flip-only --apply             # free ones only
python scripts/aac_fallback_track.py --root ... --limit 10 --apply              # with encodes
python scripts/jellyfin_library_scan.py --library "TV Shows"                    # then this
```

Do not reach for a 5.1 AAC track to avoid the stereo-default trade-off — it
transcodes on any stereo-output browser anyway (§3.6).

### 4.7 Adding a new cron job

Wrap it, or it can die silently like two before it:

```
*/5 * * * * /usr/bin/flock -n /tmp/nas-<job>.lock /usr/bin/env bash -c "cd /home/tom/nas && \
  . .venv/bin/activate && python scripts/cron_job.py --name <job> --max-age-min <N> -- \
  python scripts/<job>.py >> logs/<job>.log 2>&1"
```

Rules, all of which have bitten: `cd /home/tom/nas` before any relative path;
the wrapper goes **inside** `flock`; `--max-age-min` should be roughly three
times the interval; use `--ok-codes 0` only for commands that do not follow
this repo's 0/1/2 contract. Then `python scripts/cron_job.py --name <job>
--max-age-min <N> --register` so it is watched before its first run. The
watchdog lints the crontab every 5 minutes and will complain if you get the
`cd` wrong.

### 4.8 Scan one library by hand

```bash
cd /home/tom/nas && . .venv/bin/activate
python scripts/jellyfin_library_scan.py --list
python scripts/jellyfin_library_scan.py --library "TV Shows"
```

Returns as soon as Jellyfin accepts the job; the scan itself runs in the
background with no pollable progress (§3.2).

---

## 5. Needs Tom

Short, and this is the whole list.

1. **Create the healthchecks.io check** and put its ping URL in
   `NAS_HEARTBEAT_URL`. Period 10 min, grace 20 min. Everything else about the
   off-box heartbeat is built and verified — this is an account action no
   script can do, and it is the one remaining hole in alerting coverage. The
   watchdog raises a standing `heartbeat:unconfigured` warning until it is set.
2. **Subscribe a phone to ntfy**: `https://ntfy.<PUBLIC_DOMAIN>/nas-alerts`,
   credentials `NAS_ALERT_USER` / `NAS_ALERT_PASSWORD` from `.env`. DNS and the
   wildcard cert already cover it. Server-side is verified end to end
   (including a real container failure and its recovery); nothing has actually
   buzzed a phone yet.
3. **Bazarr credentials** — a working opensubtitles.com login and a fresh
   addic7ed Cloudflare cookie. Both providers stay disabled until then;
   credentials are still stored, so re-enabling is a toggle.
4. **Blue Öyster Cult duplicate tracks** — `/music/Blue Öyster Cult/1977 -
   Spectres/` holds two copies each of tracks 04 and 08, differing only in
   apostrophe character. Lidarr's pending rename would overwrite one with the
   other. Noted in `AGENTS.md` so a future session does not hit it blind, but
   only you can pick which copy to keep.
5. **`.sudo-pwd`** is now gitignored, but it is still a plaintext sudo password
   in the repo root. Worth deleting or moving somewhere with 600 permissions.

**Closed since pass 8:** the AAC rollout (declined; 5 free flips done, tool
kept for future small batches), the 5.1 question (answered — it transcodes on
stereo-output browsers, §3.6), autoheal (running, and two timeout defects
fixed), the cron-silence class (wrapped + linted), and the ruff backlog (green).

**The owed Jellyfin restart is done**, and it confirmed both things it was
owed for. It no longer happens by itself — jellyfin's watchtower label was
removed on 2026-09-01 — and someone was mid-film for ~40 minutes, which was not
worth interrupting to save 2 MB of log, so it was deferred until playback
actually ended (20:42) rather than forced:

```text
RefreshLibrary triggers after restart: []          <- survived, PASS
"Request starting" lines: 6761 -> 6761 over 90s    <- request logging off
jellyfin 10.11.11, healthy, anon=318.8MB, arena_regions=0, doublemapper=0
```

The trigger result is the one that mattered: a persisted `[]` on disk was good
evidence but not proof, and the whole point of emptying it was that the scan is
the largest memory event left.
