# Cleanuparr: what is armed, and what is deliberately not

**Date:** 2026-09-01
**Version:** Cleanuparr 2.10.5
**Related:** `docs/arr-qbittorrent-pollution.md` (the diagnosis this configures against),
commit `13dbdb9` (the one-off cleanup + hardlink restructure it follows).

Cleanuparr had been deployed and healthy for two weeks with **every module
disabled** — the state `arr-qbittorrent-pollution.md` §E describes as "you
already have a janitor and it is switched off". This is the configuration that
switched it on, and, more importantly, the reasoning for the three modules that
stay off.

**None of this configuration lives in this repo.** It is all in Cleanuparr's own
SQLite database (`${CONFIG_DIRECTORY}/cleanuparr/cleanuparr.db`), applied over the
REST API with `API_KEY_CLEANUPARR`. The malware blacklist is a plain file next to
it at `${CONFIG_DIRECTORY}/cleanuparr/blacklist.txt`; it is intentionally *not*
duplicated into the repo, because a version-controlled copy that is not the one
the container reads is worse than no copy at all. Edit it in place — no restart
needed, the module re-reads it each run.

---

## 1. The shape of the problem

Three buckets of junk recur, and only one of them is reachable by Sonarr or
Radarr on their own:

| Bucket | What it is | Who can clean it |
|---|---|---|
| Imported, goal met | seeding finished, \*arr imported it | Sonarr/Radarr — **works today** |
| Import blocked | fake release, `.exe`/`.scr` payload | nobody — removal is gated on a successful import |
| Never imported | dead swarm, stuck on metadata | nobody — the \*arr still hopes to import it |
| Orphaned | series deleted from Sonarr, history rows gone | nobody — no history means no tracking, ever |

Cleanuparr's job here is the bottom three rows. The first row is **not** its job
and is deliberately left to Sonarr, which demonstrably does it.

---

## 2. What is armed

### General

| Setting | Value | Why |
|---|---|---|
| `dryRun` | `false` | live |
| `httpMaxRetries` | `2` (was `0`) | one dropped request should not become a strike |
| `connectivityCheckEnabled` | `true` | **the most important safety setting here** |
| `connectivityCheckUrls` | `http://ntfy:8410/v1/health`, `https://one.one.one.one` | one internal, one external |

Every module below is strike-based, and a strike counter cannot tell "this
torrent is dead" from "the internet is down". Without the connectivity check, a
single ISP outage strikes out every torrent simultaneously and the next few
ticks delete the lot. Both URLs are verified reachable from inside the
container.

### Malware Blocker — every 5 minutes

Sonarr and Radarr only. Lidarr is off: it has **no qBittorrent presence at all**
(its only download client is slskd), so there is nothing for this module to
inspect.

- `blocklistPath: /config/blacklist.txt` — a **local file, not a remote URL**.
  A fetch failure or an upstream edit must never silently change what gets
  deleted on this box.
- `deleteIfAnyFileBlocked: true` — the fake-release pattern always bundles the
  executable *alongside* a decoy video, so per-file blocking would never reach
  it.
- The blacklist covers executable and script extensions only. Archive formats
  (`.rar`, `.zip`, `.7z`) are absent — scene releases use them legitimately.
  `.iso` is absent too: full-disc rips are real content.

### Queue Cleaner — every 5 minutes

| Setting | Value | Why |
|---|---|---|
| `failedImport.maxStrikes` | `3` | 15 minutes of failing, then blocklist + re-search |
| `failedImport.skipIfNotFoundInClient` | **`true`** | **this is what protects Lidarr** — see below |
| `failedImport.patternMode` | `Exclude`, empty list | act on every failed import (an empty `Include` list is rejected by the API) |
| `downloadingMetadataMaxStrikes` | `6` | 30 minutes stuck in `metaDL` |
| Stall rule | 12 strikes (1h), completion `0–99%`, Public, reset on progress | dead swarms |
| Slow rule | <50 KB/s, 24 strikes (2h), completion `0–99%`, ignore >60 GB, ignore during alt-speed | slow swarms |

Both queue rules are bounded to **0–99% completion** so they can only ever act
on an incomplete download. This matters more than it looks: a *completed*
torrent has a download speed of zero, so an unbounded slow rule would strike
every finished torrent on the box.

The action is **blocklist and re-search**, not deletion — the \*arr grabs an
alternative release. That is strictly better than deleting.

### Download Cleaner — hourly

Cron moved from every 5 minutes to hourly; the strike counters here are
per-run, and the UI's own guidance assumes an hourly cadence.

One seeding rule, `Imported – goal met`:

```
categories:        [arr-sonarr, arr-radarr]
privacyType:       Public
maxRatio:          0.6
maxSeedTime:       34 hours
deleteSourceFiles: true
```

Those numbers are **deliberately identical to qBittorrent's existing globals**
(`max_ratio=0.6`, `max_seeding_time=2000` min ≈ 33.3 h). qBittorrent pauses a
torrent at the goal; nothing was ever removing the paused ones except Sonarr,
and Sonarr can only remove what it still has history for. This rule finishes the
job qBittorrent starts, and it is the permanent fix for the orphan bucket:
future series deletions age out on their own.

### Notifications

ntfy at `http://ntfy:8410`, topic **`nas-cleanuparr`** — deliberately *not*
`nas-alerts`, because routine cleanup traffic would drown the failure alerts
that topic exists for. Requires `ntfy access watchdog nas-cleanuparr rw`, which
has been granted.

Subscribed events: failed-import strike, queue item deleted, download cleaned.
Stall and slow strikes are **not** subscribed — they fire dozens at a time and
are visible in the UI's event log anyway.

---

## 3. What is off, and why

### Unlinked Downloads — off

This module deletes torrents whose files have no hardlink outside the download
directory, on the theory that no hardlink means the library copy is gone.

Commit `13dbdb9` fixed hardlinking for Sonarr and Radarr (`${SHARE_DIRECTORY}:/data`
plus root folders at `/data/series` and `/data/movies`). Verified here: new
imports land at `nlink=2`. But the **pre-restructure backlog is still `nlink=1`**
— measured 425 large files at `nlink=1` against 1 at `nlink=2`. Those files were
imported by copy, so their library copy is present and healthy while the
hardlink count says otherwise.

Enabling this module today would therefore flag essentially the entire download
tree and delete a backlog the commit deliberately chose to let age out through
seed goals instead.

**It becomes a valid signal once that backlog has aged out** — when
`find /mnt/drive/downloads/complete -type f -size +50M -links 1 | wc -l` is
near zero. Not before. Lidarr is still on `/music` and still copying, so its
tree never qualifies.

### Orphaned Files — off

Moves files not associated with any active torrent. `/mnt/drive/downloads/complete`
also holds `slskd/`, which is slskd's own tree and has no torrent behind any of
it by definition. Scoping this safely needs `ignoredRootDirs`, and the
pollution audit found no meaningful orphan-file problem to justify the risk.

### Dead Torrents — off, and this one is the interesting result

Dead-torrent triage marks torrents reporting no seeders for N consecutive runs.
It was configured, run once in dry-run, and **switched back off on the
evidence**.

The dry run struck 22 torrents. Every one of them was 100% complete,
`queuedUP`, and seeding toward the goal at ratio 0.15–0.48 against a 0.6 target
— Friends, Fargo, Planet Earth III, South Park. They are bucket A, "working as
intended, leave alone".

The cause is that "no seeders" means *no other seeder is online*, which is the
normal resting state of an old public torrent that you are the last seed of.
**25 of 59 completed torrents on this box are in that state.** A signal that
fires on 42% of healthy torrents is not a signal, and the seeding rule attached
to it would have deleted them, with source files, inside a day.

The discriminating tool for genuinely-dead downloads is the Queue Cleaner's
stall rule, bounded to 0–99% completion, which is armed.

### Seeker — off

Its job is to *find and grab* missing or upgradable content. The problem being
solved here is disk pollution, and it also pushes searches through Prowlarr —
see the search-flood notes in `CLAUDE.md`. Revisit after the cleanup settles.

### Blacklist Sync — off

Needs a curated remote list. The local malware blacklist already covers the one
failure mode actually observed on this box.

---

## 4. Why Lidarr is structurally safe

Lidarr's only download client is **slskd**. It has zero torrents in qBittorrent,
and Cleanuparr only knows about qBittorrent. Its Lidarr queue items are
therefore invisible to Cleanuparr's client view, and
`failedImport.skipIfNotFoundInClient: true` means an item Cleanuparr cannot find
in a download client is skipped rather than struck.

That lane stays owned by the existing scripts —
`lidarr_stuck_download_reaper.py`, `slskd_incomplete_sweep.py`,
`lidarr_backlog_drip.py`. **Do not enable Lidarr in Cleanuparr's modules**, and
do not set `skipIfNotFoundInClient: false`; either would point a deletion engine
at a queue it cannot see.

---

## 5. Why nothing tags torrents from the \*arr side

Worth recording, because it is the obvious thing to reach for and it is not
available.

**Sonarr and Radarr cannot tag torrents.** Their complete qBittorrent client
field list is `host, port, useSsl, urlBase, username, password,
tvCategory/movieCategory, tvImportedCategory/movieImportedCategory,
recent/olderPriority, initialState, sequentialOrder, firstAndLast,
contentLayout`. There is no tag field — not misconfigured, absent.

The only lever is the **post-import category**, and it is left empty on purpose:

1. `auto_tmm=true` on every torrent, and each category has a distinct
   `savePath`. Changing a torrent's category makes qBittorrent physically
   relocate its data — roughly 1.1 TiB of it.
2. Sonarr finds its torrents by its configured category. Move them out and its
   removal logic loses sight of them, which is exactly how the orphan bucket
   forms. Sonarr's removal currently works; handing that to Cleanuparr would
   trade a working mechanism for a new one.

Cleanuparr's own "change category instead of delete" option depends on that same
post-import category, so it stays off too. Where Cleanuparr needs to mark a
torrent it uses a **tag** (`useTag: true`), which moves no data.

qBittorrent's three unclaimed categories — `arr-lidarr`, `arr-slskd`,
`prowlarr` — were deleted as part of this change. All three held zero torrents;
`arr-lidarr` predated Lidarr's move to slskd.

---

## 6. Verification

```bash
K=$(grep -E '^API_KEY_CLEANUPARR=' .env | cut -d= -f2-)

# what is armed
for e in general queue_cleaner malware_blocker download_cleaner seeker; do
  curl -s -H "X-Api-Key: $K" "http://127.0.0.1:11011/api/configuration/$e" | python3 -m json.tool
done

# job schedules
curl -s -H "X-Api-Key: $K" http://127.0.0.1:11011/api/jobs | python3 -m json.tool

# run one now instead of waiting for cron
curl -s -X POST -H "X-Api-Key: $K" http://127.0.0.1:11011/api/jobs/QueueCleaner/trigger

# what it did — check isDryRun on every event before believing a count
curl -s -H "X-Api-Key: $K" "http://127.0.0.1:11011/api/events?pageSize=50" | python3 -m json.tool
```

**Before changing any rule, set `dryRun: true`, trigger the job, and read the
events.** That is not ceremony — it is what caught the dead-torrent rule above,
which looked entirely reasonable on paper and would have deleted 22 healthy
torrents.

---

## 7. Known risks

- **The slow rule may strike queued downloads.** With `max_active_uploads=10`
  and queueing on, a `queuedDL` torrent sits at 0 KB/s because it is waiting for
  a slot, not because the swarm is slow. The 0–99% completion bound does not
  exclude it. Watch the first day of `SlowSpeedStrike` events; if queued
  torrents are accruing strikes, drop the rule.
- **The stall rule acts on a large existing backlog.** It blocklists and
  re-searches rather than deletes, but it will produce a burst of Prowlarr
  searches over its first hour.
- **`deleteIfAnyFileBlocked: true` is aggressive by design.** A legitimate
  release carrying one stray blacklisted file loses the whole torrent.
- **The seeding rule has never actually fired.** No torrent currently meets the
  0.6 / 34 h goal — qBittorrent pauses at the goal and Sonarr reaps those first
  — so the dry run produced no `DownloadCleaned` events and there is no observed
  behaviour for it, only stored configuration.
- **Six torrents were deleted live during this work**, at 18:45, all with
  `reason=DownloadingMetadata`. They were stuck in `metaDL` and held no data on
  disk; Sonarr re-searched. This happened in the window between arming the Queue
  Cleaner and enabling dry-run, and is the intended behaviour of that module —
  recorded here because it was not an approved-and-then-executed deletion.
