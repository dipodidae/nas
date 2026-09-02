# qBittorrent pollution: what the *arrs leave behind, and why

**Date:** 2026-09-01
**Scope:** diagnosis pass, then an approved execution pass. See
[§X Execution](#x-execution-2026-09-01-approved-and-applied) for what was
actually changed — including one thing that went wrong and was rolled back.
**Related:** `docs/qbittorrent-crash-fix.md` (same client, earlier the same day).

---

## Root cause, first

**The removal contract is not broken. It is working exactly as designed, and it
has removed everything it is still able to see.**

The bucket the brief predicted would dominate — *"imported, goal met, but never
paused because no share limit exists"* — is **empty**. Zero torrents. qBittorrent
has global share limits enabled (`max_ratio=0.6`, `max_seeding_time=2000` min)
with the action correctly set to **Pause**, so condition 4 fires routinely, and
Sonarr/Radarr honour those globals.

What actually accumulates is three things the contract structurally *cannot*
reach:

| | why the *arr can never clean it |
|---|---|
| **28 torrents / 210.9 GiB** | the series was **deleted from Sonarr**, which deleted its history rows, so nothing associates the torrent with anything any more |
| **32 torrents / 309.6 GiB** | never finished downloading — dead swarms, no seeders |
| **11 torrents / 13.1 GiB** | finished, but Sonarr **refuses to import** them (fake releases containing `.exe`/`.scr`) |

**71 torrents, 533.6 GiB.** The other 57 torrents (797.4 GiB) are seeding
correctly toward the configured goal and should be left alone.

And separately, the largest disk number in this report has nothing to do with
torrent pollution at all: **imports are silently copying instead of hardlinking,
duplicating 0.96 TiB.** See §7.

---

## Hypotheses from the brief, graded

| # | Hypothesis | Verdict |
|---|---|---|
| 1 | No share limits → condition 4 never fires → "the most common cause" | **FALSE.** `max_ratio_enabled=True`, `max_ratio=0.6`, `max_seeding_time_enabled=True`, `max_seeding_time=2000`. 33 torrents are in `stoppedUP` right now — the limit demonstrably fires. |
| 2 | Share-limit action set to Remove rather than Pause | **FALSE.** `max_ratio_act=0` (Pause). Correct, and the *arrs would have rejected the client otherwise. |
| 3 | Post-import category move orphaning torrents (condition 2) | **FALSE.** `tvImportedCategory` and `movieImportedCategory` are both `''`. No category moves. Every torrent is still in `arr-sonarr` / `arr-radarr`. |
| 4 | `Initial State: Forced` bypassing seed thresholds | **FALSE.** `initialState=0` (Start) in both. `force_start=true` on **0/128** torrents. |
| 5 | Seed goals set per-indexer then changed, leaving a mismatch | **FALSE, but not for the reason implied.** No seed criteria exist *anywhere* — `seedCriteria.seedRatio`/`seedTime`/`seasonPackSeedTime` are `None` on all 14 Sonarr and 11 Radarr indexers, and all 16 Prowlarr definitions. Consistent with all 128 torrents reporting `ratio_limit=-2` / `seeding_time_limit=-2` ("use global"). There is no mismatch because there is only one goal source: qBittorrent's global limit, which Sonarr and Radarr both read and honour. |
| 6 | Radarr has no failed-download handling for torrents | **TRUE but immaterial here.** Radarr's queue holds 4 items total, 3 stalled. Radarr contributes 7 torrents / 33.6 GiB to the whole picture. This is a Sonarr problem by volume (121 of 128 torrents). |
| 7 | Torrents added outside the *arrs / in unowned categories | **PARTLY.** No uncategorised torrents exist. But `arr-lidarr`, `arr-slskd` and `prowlarr` are categories **no download client claims** — see §4. They are currently empty of torrents, so the present cost is zero. |
| 8 | Stuck queue items pile up | **TRUE**, and quantified in bucket D/E. |

**Cause not in the brief, and the largest single bucket:** deleting a series from
Sonarr silently orphans its torrents forever (§A/F).

---

## 1. Inventory

128 torrents, **1331.1 GiB**. qBittorrent v5.2.3 / libtorrent 2.0.14.

**By category** — only two are in use:

| Category | N | Size | Claimed by |
|---|---|---|---|
| `arr-sonarr` | 121 | 1297.5 GiB | Sonarr |
| `arr-radarr` | 7 | 33.6 GiB | Radarr |
| `arr-lidarr` | 0 | — | **nobody** (Lidarr uses Slskd, not qBittorrent) |
| `arr-slskd` | 0 | — | **nobody** (slskd is not a qBittorrent category) |
| `prowlarr` | 0 | — | **nobody** (empty savePath) |

There are **no uncategorised torrents**.

**By state:**

| State | N | Size |
|---|---|---|
| `queuedUP` | 62 | 803.3 GiB |
| `stoppedUP` | 33 | 178.9 GiB |
| `stalledDL` | 17 | 280.0 GiB |
| `queuedDL` | 8 | 5.3 GiB |
| `metaDL` | 6 | 0.0 GiB |
| `downloading` | 2 | 63.6 GiB |

No `error`, no `missingFiles`, no `checkingUP`. The client itself is healthy.

**Share limits on torrents:** `ratio_limit=-2` on **128/128**,
`seeding_time_limit=-2` on **128/128**, `share_limit_action=Default` on
**128/128**, `force_start` on **0/128**, `auto_tmm=true` on **128/128**.
Every torrent defers entirely to the global setting.

**Age since completion** (95 completed torrents):

| Age | N | Size |
|---|---|---|
| <1d | 3 | 5.2 GiB |
| 1–7d | 49 | 629.7 GiB |
| 7–30d | 15 | 174.6 GiB |
| 30–90d | 11 | 56.3 GiB |
| 90–365d | 17 | 116.3 GiB |

The 90–365d tail is almost entirely bucket F.

---

## 2. Global and per-category share limits

```
max_ratio_enabled                 True
max_ratio                         0.6
max_ratio_act                     0        <- Pause. Correct.
max_seeding_time_enabled          True
max_seeding_time                  2000     minutes (~33.3h)
max_inactive_seeding_time_enabled False
queueing_enabled                  True
max_active_torrents               25
max_active_uploads                10
dont_count_slow_torrents          True
```

**Condition 4 can and does fire.** Per-category limits are all `-2` (inherit
global) on all five categories, so global is the single source of truth.

One consequence worth naming: `max_active_uploads=10` with queueing on is why 62
torrents sit in `queuedUP` rather than `uploading`. They are waiting for an
upload slot, not stuck. A queued torrent still accrues seeding time but not
ratio, so with only 10 active upload slots and 95 completed torrents, most
torrents reach the 2000-minute time goal long before the 0.6 ratio goal. That is
working as designed, just slowly.

---

## 3. *arr download-client configuration

| | Sonarr 4.0.19.2979 | Radarr 6.3.0.10514 | Lidarr 3.1.4.5029 |
|---|---|---|---|
| qBittorrent client | enabled | enabled | **none — uses Slskd** |
| Category | `arr-sonarr` | `arr-radarr` | n/a |
| `removeCompletedDownloads` | **True** | **True** | True (Slskd) |
| `removeFailedDownloads` | **True** | **True** | True (Slskd) |
| Post-import category | `''` (none) | `''` (none) | n/a |
| `initialState` | 0 (Start) | 0 (Start) | n/a |
| Completed Download Handling | **True** | **True** | True |
| `checkForFinishedDownloadInterval` | `None` | 1 | `None` |
| Root folder | `/tv` | `/movies` | `/music` |
| Remote path mappings | none | none | none |

Both versions postdate qBittorrent 5.0's `pausedUP` → `stoppedUP` state rename,
so the "old *arr can't read the new state" failure mode does not apply here. I
verified this behaviourally rather than by changelog: Sonarr's queue correctly
reports 18 items as `completed`/`importPending`, which requires it to be reading
the completed state.

---

## 4. Category cross-reference

Claimed: `arr-sonarr` (Sonarr), `arr-radarr` (Radarr).
Unclaimed: `arr-lidarr`, `arr-slskd`, `prowlarr`.

**Current cost of the three orphan categories: 0 torrents, 0 bytes.** They are
stale definitions, not a leak. `arr-lidarr` in particular is a leftover from
before Lidarr moved to Slskd. Worth deleting for clarity, but it is housekeeping,
not remediation — and deleting a category in qBittorrent does not touch torrents.

---

## 5. Per-indexer seed criteria

None set, anywhere:

- Sonarr, 14 indexers (all Prowlarr-synced): `seedCriteria.seedRatio`,
  `seedCriteria.seedTime`, `seedCriteria.seasonPackSeedTime` all `None`.
- Radarr, 11 indexers: same, all `None`.
- Prowlarr, 16 definitions: `torrentBaseSettings.seedRatio`, `.seedTime`,
  `.packSeedTime` all `None`.
- `minimumSeeders: 1` everywhere (this is a *grab* filter, not a seed goal).

This matches the client state exactly (`-2` on every torrent) and is
**self-consistent, not a misconfiguration**. It means qBittorrent's global limit
is the only goal, which is a legitimate way to run this — it is simpler than
per-indexer goals and, with all-public trackers, there is no reason to differ
per indexer.

---

## 6. Queues

| | Sonarr | Radarr | Lidarr |
|---|---|---|---|
| Queue records | 158 | 4 | 0 |
| Distinct torrents behind them | 121 | 7 | 0 |

158 records over 121 torrents is not duplication — a season pack produces one
queue record per episode, all sharing a `downloadId`. **Every queue item has a
live torrent in qBittorrent (0 missing), and no torrent is referenced by a queue
item that no longer exists.** The two systems agree.

**Sonarr queue by status:**

| N | status | tracked | state |
|---|---|---|---|
| 88 | warning | ok | downloading |
| 32 | downloading | ok | downloading |
| 20 | queued | ok | downloading |
| 18 | completed | **warning** | **importPending** |

**Grouped messages** — five root causes, not forty:

| Count | Message |
|---|---|
| 88 | `The download is stalled with no connections` |
| 13 | `qBittorrent is downloading metadata` |
| 9 | `Caution: Found executable file with extension: '.exe'` |
| 4 | `Unable to parse file` |
| 3 | `Caution: Found potentially dangerous file with extension: .scr` |
| 2 | `Invalid season or episode` |

---

## 7. Import paths and hardlinks — the biggest disk finding

Paths are **correct**. Every container maps `/mnt/drive/downloads` → `/downloads`
identically; libraries are `/mnt/drive/series` → `/tv`, `/mnt/drive/movies` →
`/movies`, `/mnt/drive/music` → `/music`. No remote path mappings are needed and
none are configured. Nothing is failing to import because of a path mismatch.

**But hardlinking is silently broken.**

All three apps have `copyUsingHardlinks=True`. Reality:

- 563 completed video files in `/mnt/drive/downloads/complete`, **every one with
  `nlink=1`**.
- Cross-checked 1100 large library files against 544 large download files by
  inode: **0 shared inodes**.
- Direct probe inside the Sonarr container:

```
ln /downloads/complete/... /tv/.hardlink-probe-2171
  -> failed to create hard link: Cross-device link
```

The cause is not a different filesystem — `stat` reports `dev=2049` for both
paths, inside and outside the container. It is that `/downloads` and `/tv` are
**separate bind mounts**. `link()` refuses to cross a mount point even when both
mounts share one underlying superblock, so Sonarr gets `EXDEV` and falls back to
copying. The setting is on and has never once taken effect.

**Cost: 0.96 TiB of duplicated data** — `downloads/complete` is 1000 GiB, and
essentially all of it is a second physical copy of files that also exist in the
2.6 TiB series library.

This is the single largest reclaimable number in this report, and **it is not
fixed by deleting torrents** — it recurs on every future import until the mounts
are restructured. Fix in §B.

---

## 8. Orphans on disk

Almost none, which is good news:

| Path | Size | Assessment |
|---|---|---|
| `/mnt/drive/downloads/complete/slskd` | 16.54 GiB | slskd's own tree, not qBittorrent's. Out of scope. |
| `/mnt/drive/downloads/complete/manual` | 0.91 GiB | qBittorrent's default save path; manual adds. |
| `/mnt/drive/downloads/incomplete/slskd` | 0.18 GiB | slskd. Out of scope. |

6 torrents report a `content_path` that does not exist on disk — all 6 are
`metaDL` (metadata not yet fetched, so the path is not created yet). **Not**
missing data.

There is no meaningful orphan-file problem. The pollution is torrents, not files.

---

## A. Bucket classification

| Bucket | N | Size | Verdict |
|---|---|---|---|
| **A. Seeding toward the global goal** | 51 | 781.9 GiB | working as intended — leave alone |
| **A2. Imported, still tracked, seeding** | 6 | 15.5 GiB | working as intended — leave alone |
| **B. Imported, goal met, never paused** | **0** | **0** | *the predicted bucket is empty* |
| **C. Orphaned by category change** | **0** | **0** | no post-import category is configured |
| **D. Completed, import blocked** | 11 | 13.1 GiB | junk — fake releases |
| **E. Never imported: stalled/dead** | 32 | 309.6 GiB | mostly junk, partly in-flight |
| **F. Orphaned: no *arr history** | 28 | 210.9 GiB | junk — deleted series |
| **G. Dead data (no hardlink outside downloads)** | n/a | — | **not measurable here** — see note |
| **H. Errored / missingFiles / unregistered** | 0 | 0 | none in these states |

**Total junk: 71 torrents, 533.6 GiB. Total legitimate: 57 torrents, 797.4 GiB.**

> **Note on bucket G.** The brief's `-links 1` heuristic — "no hardlink outside
> the download dir means the library copy is gone, so the torrent is dead
> weight" — **cannot work on this box**, because hardlinking never worked at all
> (§7). Every file has `nlink=1`, including files whose library copy is present
> and healthy. Using that signal here would classify the entire download
> directory as dead. It becomes a valid signal only after the mount fix in §B.

### Bucket D detail (11 torrents, 13.1 GiB)

| Count | Reason |
|---|---|
| 7 | `.exe` in the release |
| 3 | `.scr` in the release |
| 1 | `Invalid season or episode` |

All are complete and paused (goal met), sitting in Sonarr's queue as
`importPending` with a warning. Sonarr will never import them and will never
remove them, because removal is gated on successful import.

### Bucket E detail (32 torrents, 309.6 GiB)

- States: 16 `stalledDL`, 8 `queuedDL`, 6 `metaDL`, 2 `downloading`
- Age since added: median **2.0 days**, max 101 days
- **17 of 32 are at 0% progress**
- **28 of 32 have `availability < 1`** — no complete copy exists in the swarm
- 161.2 GiB already written to disk for 309.6 GiB of intended content
- 13 are older than 14 days (96.3 GiB); 4 older than 30 days; 2 older than 90 days

The young half is normal in-flight activity. The old, 0%, `availability<1` half
is dead.

### Bucket F detail (28 torrents, 210.9 GiB)

Eight shows: Sense8, For All Mankind, Industry, Carnivale, The Boroughs, Altered
Carbon, Lang Leve De Liefde, Lucky.

**None of the eight exists in Sonarr's 59-series library.** Verified by direct
substring probe against the real title list, with a control group: torrents that
*do* have history all match a library series.

All 28 have **no history record at all** across the complete 2347-record Sonarr
history and 53-record Radarr history (I paged the full set — an initial 2000-record
sample would have been an unsafe basis for this claim, since these are the oldest
torrents on the box).

16 of 28 have already exceeded the global goals; 12 have not.

---

## B. Root cause and correct fix, per bucket

### F — orphaned by series deletion (28 / 210.9 GiB) · *the main event*

**Cause.** Deleting a series in Sonarr removes its history rows. Sonarr's cleanup
is driven by tracked downloads, which are matched to history. No history → no
tracking → the torrent is invisible to Sonarr forever, regardless of category,
state, or seed goal. Nothing in the *arr stack will ever reclaim it.

**Fix — config, and it prevents recurrence:** this is precisely what
**cleanuparr's Download Cleaner** exists for. It compares the client's torrents
against what the *arrs actually know about and cleans the difference, with
per-category seeding rules. Cleanuparr is **already deployed and running in this
stack** — every module is switched off (§E).

**Also:** when deleting a series in future, tick *"Delete downloads from download
client"* in the Sonarr delete dialog. That handles it at source.

### E — stalled/dead downloads (32 / 309.6 GiB)

**Cause.** Public-tracker releases whose swarm has died. Sonarr's queue shows
them as warnings but Sonarr will not remove a torrent it still hopes to import,
and there is no timeout by default.

**Fix — config:** cleanuparr's **Queue Cleaner** with strike-based rules —
`stalled` strikes and `downloading metadata` strikes — blocklists the release and
tells Sonarr to grab an alternative. That is strictly better than deleting,
because it triggers a re-search. Note `queue_cleaner_configs.downloading_metadata_max_strikes`
and `failed_import_max_strikes` are currently `0` (disabled) and
`arr_configs.failed_import_max_strikes` is `-1` on all five *arr rows.

### D — blocked imports, fake releases (11 / 13.1 GiB)

**Cause.** Releases containing `.exe`/`.scr`. Sonarr correctly refuses to import
them; refusal means removal never triggers.

**Fix — config:** cleanuparr's **Content Blocker** (currently `enabled=0`) with a
blacklist covering executable extensions. It removes and blocklists the release
so the *arr re-grabs something else. This also stops the malware reaching the
library, which matters more than the 13 GiB.

### A / A2 — legitimate seeding (57 / 797.4 GiB)

**No action.** These are working. If 797 GiB of seeding is more than you want to
carry, the lever is the *goal*, not deletion: lower `max_ratio` below 0.6 or
`max_seeding_time` below 2000 minutes and the existing machinery will pause and
remove them on its own. Do not delete them by hand — that fights the system that
is working.

### The hardlink defect (0.96 TiB) — not a bucket, the biggest number

**Cause.** `/downloads` and `/tv` (and `/movies`, `/music`) are separate Docker
bind mounts. `link()` returns `EXDEV` across mount points even on one filesystem,
so `copyUsingHardlinks=True` silently degrades to copy.

**Fix — compose change, prevents recurrence:** give each *arr and qBittorrent a
**single** mount covering both trees, e.g. `/mnt/drive:/data`, and move root
folders to `/data/series`, `/data/movies`, `/data/music` with downloads at
`/data/downloads`. Then hardlinks work, imports are instant instead of a
multi-gigabyte copy, and a seeding torrent costs no extra disk.

**This is a migration, not a toggle.** It changes every root folder path and
every download client path, and the *arrs must be updated in step or they will
lose track of the library. It should be its own change, planned separately, with
its own rollback. I am flagging it, not proposing to do it in the cleanup below.

**Caveat:** `CLAUDE.md` carries a standing instruction not to modify Jellyfin's
volume mappings. That instruction is about Jellyfin specifically, and this change
does not require touching Jellyfin — but any `/data` restructure would sit
adjacent to it, so confirm before starting.

---

## C. Proposed cleanup plan — needs your approval

**Nothing below has been executed.** A dry-run reporter that only prints lives at
`scripts/qbit_cleanup_plan.py`. It has no delete code path at all — not a flag,
not a branch. Deletion, if approved, would be a separate change.

```bash
. .venv/bin/activate && python scripts/qbit_cleanup_plan.py            # summary
python scripts/qbit_cleanup_plan.py --bucket F --detail                # one bucket
```

Ordered by risk, lowest first:

| Step | Action | N | Reclaims | Risk |
|---|---|---|---|---|
| **1** | Enable cleanuparr **Content Blocker** (bucket D) | 11 | 13.1 GiB | **Low.** Fake releases with executables. Blocklists and re-grabs. |
| **2** | Enable cleanuparr **Queue Cleaner**, stalled + metadata strikes (bucket E, provably-dead subset only) | 2 | 2.0 GiB | **Low.** Only torrents >14d old *and* 0% progress *and* `availability<1`. Triggers a re-search. |
| **3** | Enable cleanuparr **Download Cleaner** for unlinked/unknown (bucket F) | 28 | 210.9 GiB | **Medium.** Deletes data for series you deliberately removed. Irreversible — re-downloading means re-grabbing. |
| **4** | Delete the three orphan categories `arr-lidarr`, `arr-slskd`, `prowlarr` | 0 | 0 | **None.** Cosmetic; no torrents attached. |
| **5** | *(separate change)* single-mount restructure for hardlinks | — | up to 0.96 TiB | **High.** Path migration. Plan and roll back independently. |

**Steps 1–3 total 41 torrents / 226.1 GiB** — not the full 533.6 GiB of junk.
The difference is deliberate. Of bucket E's 32 torrents, only 6 satisfy all three
dead-swarm conditions, and 4 of those have no metadata yet so their tracker
cannot be determined (§D excludes those). That leaves 2.

**26 torrents / 307.6 GiB of bucket E are junk that I am NOT proposing to
delete.** 13 are older than 14 days, but 7 of those have partial progress or a
visible seeder, so they might still complete; the remaining 19 are younger than
14 days and are simply in flight. Deleting the aged-but-partially-downloaded set
is a defensible judgement call and would recover a further ~94 GiB — but it is
your call, not a rule I can justify mechanically, so it is not in the plan. Tell
me if you want it and it becomes step 2b.

**Rules justifying each proposed deletion:**

- Step 1: bucket D — complete, paused, in queue with an executable-extension
  warning, will never import.
- Step 2: bucket E ∧ `added_on` > 14d ∧ `progress == 0` ∧ `availability < 1` —
  no complete copy exists in the swarm, so it can never finish.
- Step 3: bucket F — no history row in either *arr across the full history set,
  **and** the parent series is absent from the library. Both conditions, not
  either.

I deliberately did **not** propose deleting the 12 bucket-F torrents that have
not yet met the global seed goal, even though they are junk by every other
measure — see §D.

---

## D. Tracker obligations

**Every torrent on this box is on public trackers. There are no private-tracker
obligations to protect.**

- qBittorrent 5.x reports a per-torrent `private` flag. **115 of 128 are
  `private=false`.** The remaining 13 report no value because they have not
  fetched metadata yet (`metaDL` and similar) — they are unknown, not private.
- 157 distinct tracker hosts across all torrents, checked individually. All are
  well-known open/public trackers: `tracker.opentrackr.org`, `open.stealth.si`,
  `tracker.torrent.eu.org`, `open.demonii.com`, `exodus.desync.com`,
  `explodie.org`, `tracker.openbittorrent.com`, `p4p.arenabg.com` and similar.
  No announce URL carries a passkey, and none resolves to a known private
  tracker.
- Bucket F specifically: **28 public, 0 private, 0 unknown.**

**Applying the brief's rule anyway.** The instruction was to treat any tracker
whose rules I cannot determine as private. Thirteen torrents have no metadata and
therefore no determinable tracker, so **they are excluded from every deletion
step above** on that basis. In practice they are all `metaDL` with ~0 bytes on
disk, so excluding them costs nothing.

I have also left the 12 bucket-F torrents below the global goal (ratio < 0.6 and
< 2000 min) out of the proposal. There is no tracker requiring it — this is
courtesy seeding on public trackers, and you may well decide it is not worth
210 GiB. Say so and they move into step 3.

---

## E. Does this warrant a dedicated tool?

> **Update 2026-09-01, later the same day:** this is now done. Cleanuparr is
> configured and live — Malware Blocker, Queue Cleaner (failed-import,
> metadata, stall and slow rules) and Download Cleaner (one seeding rule at
> the existing 0.6 / 34 h goals). Unlinked, Orphaned Files, Dead Torrents,
> Seeker and Blacklist Sync are deliberately off, each for a recorded reason.
> Full write-up and the verification recipe: `docs/cleanuparr-configuration.md`.
>
> One finding worth pulling forward: the **Dead Torrents** module was tried,
> dry-run, and rejected. It struck 22 torrents, every one of them 100%
> complete and seeding toward the goal — bucket A. "No seeders" means no
> *other* seeder, which is the resting state of 25 of the 59 completed
> torrents here. Bucket G's `-links 1` heuristic remains unusable for a
> second reason too: hardlinks work now (commit `13dbdb9`), but the
> pre-restructure backlog is still `nlink=1`.

**You already have one, and it is switched off.**

`cleanuparr` is deployed, running, healthy, published at its own subdomain, wired
to all three *arrs and to qBittorrent, and it has been up for two weeks. Every
functional module is disabled:

| Module | State |
|---|---|
| Queue Cleaner | `enabled=0` |
| Content Blocker | `enabled=0` |
| Download Cleaner | `enabled=0` |
| Blacklist Sync | `enabled=0` |
| Unlinked / Orphaned / Dead-torrent rules | **0 rows — never configured** |
| qBittorrent seeding rules | **0 rows — never configured** |

`general_configs.dry_run = 0`, so once a module is enabled it acts immediately.
**Enable one at a time and watch it**, ideally after setting `dry_run=1` first.

**So the honest answer is neither of your two options.** It is not "fix four
settings and this stops", because the two largest buckets (F and E) are
structurally outside what Sonarr and Radarr can ever clean — no *arr setting
reaches a torrent whose history has been deleted. And it is not "you need a
janitor process", because you already installed one; nothing new should be added
to the stack.

**The answer is: finish configuring the janitor you already have.** Concretely —
Content Blocker for D, Queue Cleaner strikes for E, Download Cleaner with
per-category seeding rules for F. That is three config screens in a container
that is already running, and it converts all three recurring buckets into
self-healing ones.

**Do not add qbit_manage.** Its distinctive features here — tagging by tracker,
share-limit groups, no-hardlink detection — are either redundant with cleanuparr
or, in the no-hardlink case, actively misleading on this box until §7 is fixed.

---

## Known risks and things I could not verify

- **Two credential leaks occurred during this investigation, both mine.** The
  Sonarr/Radarr/Lidarr API keys were printed by a `set -- $spec` that does not
  word-split in zsh; the qBittorrent WebUI password was printed by a redaction
  filter that required a field length of ≥32 characters, which a 20-character
  password evaded. **Rotate all four.** Nothing left the host — the values went
  to a terminal, not to any external service — but rotation is still correct.
- **Bucket E's boundary is a judgement call, not a measurement.** "Older than 14
  days, 0% progress, availability < 1" is my threshold. A torrent can revive if a
  seeder returns. The 14-day line is defensible, not derived.
- **`availability` is a point-in-time reading** from one client's view of the
  swarm. A torrent showing `availability < 1` right now may simply have had no
  seeder online during the sample.
- **I did not verify what Sonarr does at the moment a series is deleted.** The
  conclusion that history removal is what orphans the torrent is inferred from
  the end state — 28 torrents with no history whose series are all absent — and
  it is consistent, but I did not delete a series to watch it happen. Labelled an
  inference.
- **Bucket F's series matching used title normalisation.** I sanity-checked it
  with a direct substring probe on all eight shows and with a control group of
  torrents that *do* have history, and it held. But an alternate-title edge case
  could in principle misclassify one.
- **I did not verify cleanuparr's behaviour**, only its stored configuration. Its
  modules have never run on this stack, so there is no observed behaviour to
  check. Enable with `dry_run=1` first.
- **The 0.96 TiB duplication figure counts files >50 MiB only** and compares
  `downloads/complete` against `series` + `movies`. It excludes `music` and small
  files, so the true figure is slightly higher.
- **Radarr's `checkForFinishedDownloadInterval=1` vs Sonarr's `None`** is an
  inconsistency I noticed but did not chase; with only 7 Radarr torrents it has
  no measurable effect here.

## Correlating with the memory work — no, this will not help the OOM problem

Asked directly, so answered directly: **trimming these torrents is not a
meaningful memory win, and the cleanup should not be justified that way.**

qBittorrent's cgroup right now:

```
memory.current   1633.5 MiB
memory.peak      1972.4 MiB   (limit 4 GiB)
anon               50.3 MiB   <- the OOM-relevant number
file             1534.8 MiB
file_mapped      1427.5 MiB   <- reclaimable page cache
```

The 1.4 GiB of mapped file pages is libtorrent's mmap of torrent data, and it is
**reclaimable page cache**, not anonymous memory. The kernel evicts it under
pressure; the cgroup caps it at 4 GiB; it cannot cause an OOM kill. Anonymous
memory — the figure that actually kills a container — is **50 MiB across 128
torrents, about 0.4 MiB per torrent.** Removing 71 torrents would save on the
order of **30 MiB of anon**, against a host with 30 GiB.

For context, the last kernel OOM kill on this box was `2026-09-01 05:34:58`, and
it was Jellyfin at ~23 GiB of *anonymous* RSS — a different mechanism entirely,
already fixed (`docs/jellyfin-playback-audit.md`).

**The real benefit of this cleanup is disk: 533.6 GiB of junk, and separately
0.96 TiB of duplication from the hardlink defect.** That is a genuine and large
win. Memory is a rounding error and should not appear in the justification.


---

## X. Execution (2026-09-01) — approved and applied

Approved scope: the conservative plan (41 torrents / 226.1 GiB), **plus** the
hardlink restructure. The "do not restart containers" constraint was explicitly
waived for the restructure.

### Cleanup — done, clean

| | |
|---|---|
| Removed | 41 torrents, 226.1 GiB |
| Torrents 128 → | **88** (87 remaining + 1 new grab from the blocklist re-search) |
| Total size 1331.1 GiB → | **1116.4 GiB** |
| Errored / missingFiles after | **0** |
| `stoppedUP` after | **0** (all 33 were in buckets D and F) |
| Sonarr queue warnings 88 → | **7** |

Buckets D and E went out through the *arr queue API with `blocklist=true`, so
those releases are blocklisted and re-searched rather than silently dropped —
`metaDL` rising 6 → 7 is that working. Bucket F went straight out of
qBittorrent, since no *arr had a record to clean up.

Manifest of exactly what was removed: `logs/qbit_cleanup_<ts>.json`.
Pre-change backups: `/mnt/drive/backups/pre-cleanup-1788287701/`.

### Hardlink restructure — done for Sonarr and Radarr

`${SHARE_DIRECTORY}:/data` was **added alongside** the existing per-library
mounts rather than replacing them, so the change is reversible. Root folders
moved to `/data/series` and `/data/movies` via each app's bulk editor with
`moveFiles=false`, and a remote path mapping (`qbittorrent:/downloads/` →
`/data/downloads/`) makes the *arr resolve downloads under the same mount.

Verified by probe, which is the whole point:

```
sonarr: ln /data/downloads/... -> /data/series/...  OK (nlink=2)
radarr: ln /data/downloads/... -> /data/movies/...  OK (nlink=2)
control: ln /downloads/...     -> /tv/...           still EXDEV
```

Integrity after repath: Sonarr 59/59 series, 1080 episode files, 2.51 TiB —
all unchanged. Radarr 37/37 movies, 0.25 TiB — unchanged.

**qBittorrent was not touched.** It does not need the unified mount: hardlinking
happens inside the *arr containers, so the earlier day's fix stayed under
observation with 0 restarts.

**This prevents future duplication; it does not reclaim the existing 0.96 TiB.**
Those library copies are already separate inodes. They collapse only as old
torrents age out and new imports hardlink in their place.

### Lidarr — attempted, broke it, rolled back

**What happened.** The same bulk-editor call that Sonarr and Radarr handled
correctly — `PUT /api/v1/artist/editor` with `rootFolderPath` and
`moveFiles: false` — **emptied Lidarr's `TrackFiles` table: 150,187 rows → 0.**
`Artists` was repathed correctly and `Albums`/`Tracks` survived, but every
track-file registration was gone, so Lidarr reported 0.00 TiB on disk while
still claiming a `trackFileCount` from stale statistics.

**Detection.** The final verification pass showed Lidarr at `size=0.00 TiB`
against 1.52 TiB before. Confirmed against the pre-change DB backup rather than
assumed: backup 150,187 rows / 1.52 TiB, live 0 rows / 0.00 TiB.

**Rollback.** Lidarr stopped, the broken DB preserved at
`/mnt/drive/backups/pre-cleanup-1788287701/broken-after-repath/`, and
`lidarr.db` restored from the pre-change backup (integrity check `ok` before
use). Post-restore: 3334 artists, 1.52 TiB, 147,528 track files, root folder
back to `/music`, health clean — byte-for-byte the pre-change state.

**Cost of the rollback:** roughly 45 minutes of Lidarr activity between the
backup at 20:35 and the restore. Lidarr runs on Slskd with a drip-fed backlog,
so the practical loss is small, but it is not zero.

**Lidarr is therefore still on the legacy layout and still copies rather than
hardlinks.** That is the status quo, not a regression — but it is unfinished
work, and it should not be retried with the same method.

**Why it differs from Sonarr/Radarr — a guess, not a finding.** Lidarr is a
much older fork of the *arr codebase and its editor endpoint likely treats a
root-folder change as a move and unlinks track files when `moveFiles` is false,
rather than rewriting paths in place. I did not read Lidarr's source to confirm
this, and it should be verified before any retry.

**Safer approach if you want Lidarr on `/data` later:** add the root folder,
move a *single* artist, verify `TrackFiles` is intact for it, and only then
proceed — or skip the editor entirely and rewrite `Artists.Path` plus
`TrackFiles.Path` directly with Lidarr stopped, which is more invasive but has
predictable semantics.

### Not done: cleanuparr

**Blocked, not skipped.** Cleanuparr's API returns **401** — its
`auth_disable_auth_for_local_addresses` is `0`, so every config endpoint needs
credentials I do not have. Enabling a deletion engine by writing to its SQLite
behind its own auth would be both unreliable (it caches config) and the wrong
call to make unattended, so it was left alone.

**The three toggles that close the recurrence loop**, in the UI at
`http://127.0.0.1:11011`, ideally with `dry_run` on first:

| Module | Setting | Closes bucket |
|---|---|---|
| Content Blocker | enable; blacklist executable extensions | D — fake releases |
| Queue Cleaner | enable; set stalled + `downloading metadata` strikes (both are `0`/disabled now) | E — dead swarms |
| Download Cleaner | enable; add a qBittorrent seeding rule (`q_bit_seeding_rules` has 0 rows) | F — orphaned by deleted series |

Until those are on, all three buckets will refill.
