# Hardening the slskd → Lidarr → Jellyfin pipeline — report

**2026-09-04.** Written to be readable by someone who was not there. Its job is to stop
the next person re-litigating what was proved here.

The brief was: make every stage _prove_ it worked, make every failure _alert_, and delete
every moving part a native feature can replace. One rule governed it —

> **A green tick is not evidence. An HTTP 2xx is not evidence. A log line that says
> "sent" is not evidence.** Evidence is a second, independent observation that the
> intended state change actually happened.

Nine commits, `aeb1720..e813957`. Every gate green: `make check` 51 assertions / 0
warnings, `make verify-runtime` exit 0, 601 pytest, ruff clean, `pnpm lint` clean (it had
69 errors at the start), legacy harness 19/19.

---

## PROVEN — observed effects, with the observation

### 1. The bridge advanced its cursor past history it never fetched, and exited 0

The worst defect found. `fetch_history` stopped after `MAX_HISTORY_PAGES`, printed a
WARNING to **stderr**, returned the newest 2000 records, dispatched them, advanced the
cursor to the newest of them, and returned **0**. `cron_job.py` treats 0 as success.

Demonstrated with a cursor of `2026-07-01`: 2000 records fetched, oldest
`2026-09-01T11:56:37Z`, 68 folders dispatched, **two months of history skipped
permanently**, exit 0.

### 2. …and that made a safety mechanism expire into the bug it guarded against

Now failure mode **#8, "the expiring guard"**, in `docs/music-pipeline-integration.md` §7.

An earlier fix made an unmappable folder **exit 2 with the cursor held**, so the album is
retried instead of skipped. But holding makes the cursor age. Past 2000 records the held
records fall outside the fetch window, so `unmapped` computes as **empty**, the run
"succeeds", the cursor jumps forward and **the alert stops**. The longer the guard held,
the closer it came to releasing itself.

Its tell is that there is no tell. Failure #2 at least left `outside` lines to grep; here
the records are never fetched, so nothing in any log mentions them.

> The general form, worth carrying to other systems: **ask of every hold, retry ceiling,
> backoff and cooldown whether the thing it is holding for can fall outside the window
> that would detect it.**

### 3. Four state-file failures were byte-identical to a healthy run

Absent, empty, truncated, and future-schema all printed `nothing to report` and exited 0 —
then **saved a 30-minute lookback over the real cursor**. And `save_cursor` used a bare
`write_text`, which _manufactures_ the truncated case on a crash. Two defects composing
into a complete data-loss chain the repo already had every ingredient for.

Each is now a distinct exit 2 naming what it found. Creating a cursor requires an explicit
`--since-min`/`--bootstrap`, which cron never passes.

### 4. The cursor was a timestamp, and timestamps here are not unique

**125 distinct dates across 600 live history records; one second shared by 22.** The
`date <= cursor` test skipped every record in the cursor's own second, including ones
written after the cursor was taken. History `id` is unique and monotonic (0 inversions
over 600) and `sortKey=id` is supported, so paging and the cursor are now id-based.
Live cursor migrated `date=2026-09-04T07:57:19Z → id=848483`.

### 5. slskd's `retention.files.*` is configured and inert

| Clock              | Threshold | Reality                                       |
| ------------------ | --------- | --------------------------------------------- |
| `files.complete`   | 14.0 d    | **75 dirs older**, oldest **23.8 d**, 1.63 GB |
| `files.incomplete` | 30.0 d    | **1299 dirs**, oldest **77.4 d**              |

Zero occurrences of "retention" in the container log across a full startup and 19 h.
`retention.transfers.*` demonstrably _does_ work (DB down to 24 rows, all
`Queued, Remotely`).

**This inverted the plan.** Phase 4.1 proposed retiring `slskd_complete_sweep.py` in
favour of native retention. It is the **only thing reclaiming disk**, so that would have
removed the sole working reaper and left a growing orphan pile behind a config that reads
fine. Nothing was deleted. Draft upstream issue at `docs/upstream/`.

### 6. Deletes do propagate — and the reason makes the whole stack legible

The bridge sends a hardcoded `UpdateType: "Modified"` for every event including
`trackFileDeleted`, so "Modified for a path that no longer exists" was a plausible silent
no-op. Tested with a synthetic album under an existing artist:

```
[11:34:52.487] [INF] LibraryMonitor: "Kraftwerk" ... will be refreshed.
[11:34:52.620] [INF] LibraryManager: Removing item, Type: "MusicAlbum", Path: ".../ZZ Bridge Delete Probe"
```

133 ms apart; album count returned to 15273 exactly.

**The attribution is clean because `EnableRealtimeMonitor` is `false` on every Jellyfin
library** — checked specifically to kill the inotify confound. Jellyfin is not watching
the filesystem, so nothing but the POST could have done it.

That `false` is worth more than the test: **the bridge and the weekly scan are the entire
surface between disk and Jellyfin.** It is now the first thing in §1 of the pipeline doc,
because every other section depends on it. It also settles jellyfin#16729 here as
**refuted by configuration, not by version** — re-enabling real-time monitoring puts it
straight back in scope.

### 7. Stage 4 was observable all along

The doc said `LibraryMonitor … will be refreshed` is a Debug line and that verification
was therefore impossible. It is **`[INF]`** and was being logged — including the bridge's
own dispatches at 05:33 and 10:15 that day. The premise (`[DBG]` count is 0) was right;
the conclusion did not follow.

The real caveat is narrower and is now documented: the line appears only when the refresh
**finds a change**, so a correct-but-unchanged path is indistinguishable from a bogus one.
That is fine **only because** the bridge reports exclusively changed folders — a
load-bearing dependency between two sections. If the bridge ever reports unchanged paths,
verification silently degrades.

### 8. The doc pointed at the wrong script for retirement, twice

§5 had `slskd_complete_sweep.py` (deletes _directories_ already imported) and
`slskd_cleanup.py` (deletes _transfer records_) **swapped**. §9 listed one daily library
scan where there are three weekly per-library ones, and four holders of the cleanup flock
where there are five.

Both times the docstrings were right and the prose was wrong. So the tables are now
**generated** from `cron/crontab` plus each script's docstring, gated by `make check`.
The generator independently reproduced the five-holder count.

### 9. A lock-skipped cron run left no trace at all

`flock -n` exits 1 **without running anything**, so the wrapper never started: no log
line, no state update, no output. A skipped run was byte-identical to a clean one — while
five jobs share one lock and one holds it for up to 600 s. All 16 lines migrated to
`cron_job.py --lock`, which records the skip and alerts on three consecutive.

### 10. The sweep arithmetic closes; the doc's number was wrong

The audit reported `15,268 disk vs 15,273 Jellyfin → 0 missing, 0 ghosts`, which cannot
be true — equal differences mean equal sets. Re-run from one pair of normalised sets:

```
raw disk dirs (pre-norm)   : 16208
disk (post-norm, distinct) : 15273
jellyfin albums            : 15273   (items seen 15273, pathless 0)
MISSING 0   GHOSTS 0   IN BOTH 15273
```

Not truncation, not a second library, not null paths. The conclusion was right and one
printed number was not.

---

## INFERRED — consistent with the evidence, not directly observed

- **Why the audit's two numbers disagreed: time skew.** The two halves were almost
  certainly sampled minutes apart with imports landing in between. Consistent with
  everything, but the original run cannot be replayed. The new script asserts the identity
  `len(disk) == len(disk & jf) + len(disk - jf)` and exits 2 rather than printing numbers
  that cannot all be true.
- **`transfers.download.retry` is load-bearing by accident.** `attempts 3, delay 5000,
maxDelay 60000, partial resume` are slskd's _own defaults_ — absent from `slskd.yml`,
  present in the running config. Every measurement of the stuck reaper assumes them.
  Observed; that the reaper's numbers _depend_ on them is inference. Now pinned.
- **The bridge's `flock` was never the constraint.** No evidence it ever contended.

---

## UNVERIFIED — stated because it matters, not because it was checked

- **Why `retention.files.*` is inert.** Plausibly `transfers.succeeded: 1440` destroys the
  DB row 13 days before the file clock fires, leaving the bytes unreferenced. **Unprovable
  without a 14-day wait or a scratch instance, and it changes no action.** Deliberately
  not chased.
- **`failed:` vs `errored:`/`cancelled:` precedence.** All four are set; there are
  currently **zero** records in any of those states, so there is nothing to observe.
- **Whether the 1299 incomplete dirs are dead or resumable.** Not touched.
- **The 5000 ms Soulseek login figure** in §4 of the pipeline doc. Our
  `soulseek.connection.timeout.connect` is explicitly **10000**, so whatever produces that
  number, it is not that setting. Timeboxed and abandoned; the doc claim stands as
  UNVERIFIED.
- **Whether `artistFolderImported` fires here.** Not subscribed, not tested.
- **6.6 GiB of swap in use with 10 GiB RAM free.** Recorded in the baseline, unexplained.

---

## Prompt corrections — six, and what they have in common

The brief that drove this work was wrong six times. Recording them because a report that
launders someone's errors into its own findings passes them to the next reader.

| #   | Claim                                       | Reality                                                                                                                                       |
| --- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Adopt `transfers.download.retry`            | Already active — slskd's built-in defaults, byte-identical to what was proposed                                                               |
| 2   | Adopt Tubifarry's QueueCleaner              | A 2.2.x feature; this box runs **2.1.1.0**. Adopting means a migration that changes the slskd indexer GUID and discards the release blocklist |
| 3   | Add a `--no-alert` mode to `verify-runtime` | Already exists — `VERIFY_NOTIFY ?= 0` at `Makefile:209`                                                                                       |
| 4   | The bridge grabs one `pageSize=1000` page   | It already paged backwards to the cursor. The real defects were the id, atomicity, schema and exhaustion                                      |
| 5   | `UpdateType` is never sent                  | It _is_ sent — hardcoded `"Modified"` for every path. Mapping it per event is an accuracy improvement, not a bug fix                          |
| 6   | A permanent hold means 288 alerts/day       | `cron_job.py` already ladders: `nas-infra` first, `nas-attention` on the second consecutive, 6 h cooldown ≈ 4/day                             |

### The pattern, and a correction to the first attempt at naming it

The first framing was: _the wrong claims were about the code, the right ones about
upstream projects._ That does not survive the data — it was flattered by which errors
happened to belong to whom. The delete-propagation guess and the Serilog-override premise
were both claims about **upstream** behaviour, and both were wrong.

The split that actually holds is **documented surface vs. runtime behaviour**:

- **Surface facts held, every time.** Notification id 6's field list, the
  `EntityHistoryEventType` members, slskd's config keys, `sortKey=id` being supported.
  These are readable, stable, and safe to take on trust.
- **Behaviour claims failed, from every source.** What retention _does_, what a `Modified`
  event _does_ to a deleted path, what level a line is _actually_ logged at, what a
  default _actually_ is. These failed equally for the code (#1, #2, #4) and for upstream
  (#5, deletes, log levels, retention).

**Read a surface; run a behaviour.** That is the reusable lesson, and it is why the two
most valuable things in this session were `GET /api/v0/options` and one synthetic album.

---

## Considered and deferred — with reasons

Not done, deliberately. The pipeline now passes every gate, and the marginal change is
worth less than the risk of touching it.

| Item                                                    | Why deferred                                                                                                                                                                                                                                     |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| slskd **webhooks / scripts** integration                | A real latency win (≤5 min → seconds) but pure addition to a working path. Also carries an RCE surface: `$SLSKD_SCRIPT_DATA` is attacker-controlled from the Soulseek network. Only worth it with the injection-safe handling designed up front. |
| slskd **`/metrics`**                                    | Only useful with a scrape target, and there is none.                                                                                                                                                                                             |
| **`POST /Items/{id}/Refresh`** two-tier dispatch        | Its main argument was that `/Library/Media/Updated` is unverifiable — which turned out to be false (PROVEN #7), and that new-artist scans are expensive, which real-time monitoring being off already bounds.                                    |
| **Tubifarry 2.2.x**                                     | Changes the slskd indexer GUID and silently discards every blocklisted release. A migration to plan, not a bump. ADR-worthy on its own.                                                                                                          |
| **`UpdateType` per event type**                         | Now an accuracy improvement rather than a bug fix, since `Modified` demonstrably works for deletes. Low priority.                                                                                                                                |
| Retiring `slskd_complete_sweep.py` / `slskd_cleanup.py` | **Formally refuted.** See PROVEN #5.                                                                                                                                                                                                             |
| Retiring `slskd_rescan.py` for `shares.cache.retention` | Already set to weekly; the daily cron is redundant _in principle_, but a forced rescan has a different failure profile and nothing is currently broken.                                                                                          |
| `lidarr_backlog_drip`'s fail-open `load_state`          | The **write** is now atomic, which removes the way we could manufacture a corrupt file. The read staying fail-open is documented in a test: unlike the bridge's, an empty cooldown map is recoverable.                                           |
| Filing the slskd issue                                  | Drafted at `docs/upstream/slskd-retention-files-inert.md`; needs a human to submit.                                                                                                                                                              |

---

## What changed

| Area                                    | Change                                                                                                                      |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `lidarr_jellyfin_bridge.py`             | id high-water cursor, atomic versioned state, exhaustion is fatal with an actionable message, four fail-closed state errors |
| `cron_job.py`                           | `--lock` / `--lock-wait` / `--max-skips`; a skipped run is recorded and alerts on three consecutive                         |
| `music_library_sweep.py`                | **new** — both-directions sweep, paged, arithmetic assertion, weekly cron                                                   |
| `check-slskd-effective-config.py`       | **new** — pins 12 effective values including 4 upstream defaults                                                            |
| `check-lidarr-jellyfin-notification.py` | **new** — inverted tripwire on Lidarr#5646                                                                                  |
| `gen_pipeline_tables.py`                | **new** — generates the cron-fleet tables, gated by `make check`                                                            |
| `lidarr_backlog_drip.py`                | atomic state write (Soulseek-ban domain)                                                                                    |
| `jellyfin/logging.json`                 | **new** — pins the log level a future upgrade could move                                                                    |
| `cron/crontab`                          | **new** — the fleet now has a source of truth in the repo                                                                   |
| `docs/`                                 | baseline, three new §7 failure modes, four corrected claims, §10 grew by six, upstream draft, this report                   |
| `.claude/skills/`                       | five skills                                                                                                                 |

§10 "Do not change" **grew by six entries and lost none**.

## Open items

1. File the slskd issue.
2. Inventory the remaining jobs' `--ok-codes` — 26 of 29 still inherit the `0,1` default.
   Each needs a one-line statement of what its exit 1 means.
3. `slskd_rescan.py` vs `shares.cache.retention` — decide and write the ADR.
4. ADRs for: the namespace source of truth, confirmed-effect cursor semantics, the
   QueueCleaner decline, and each native slskd feature evaluated.
