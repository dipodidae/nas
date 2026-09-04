---
name: verifying-by-effect
description: Use when about to claim any change to this NAS stack worked - an *arr notification, a Jellyfin library update, a config edit, a cron job, a slskd setting. This stack has failed silently at least six times by returning success while doing nothing, so a 2xx, a green Test button and a "sent" log line are not evidence here.
---

# Verifying by effect

## The rule

**A green tick is not evidence. An HTTP 2xx is not evidence. A log line that says
"sent" is not evidence.**

Evidence is a _second, independent_ observation that the intended state change actually
happened — an item that now exists, a `DateCreated` that moved, a row that changed, a
byte count that grew, a file that is gone.

This is not general caution. This stack has six recorded failures of exactly this shape,
and in every one the thing reporting success was reporting it truthfully — it had done
what it was asked; what it was asked was wrong.

## Things in this repo that lie by succeeding

| Surface                                   | What it says   | What is actually true                                                                                                  |
| ----------------------------------------- | -------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `POST /Library/Media/Updated`             | `204`          | `204` for a correct path **and** a path under no library. Jellyfin drops unknown paths without a word.                 |
| Lidarr's Jellyfin connection, Test button | green          | It exercises an Emby _notify_ API Jellyfin does not implement. The connection has never worked (no `mapFrom`/`mapTo`). |
| `GET /api/v1/notification` after a `PUT`  | your new value | `apiKey` comes back **masked as asterisks**. Re-`GET`ting re-masks, so it confirms nothing.                            |
| `.docker-config/*/[app].db`               | the old value  | WAL mode. Copy `*.db*` including `-wal`/`-shm` or a just-saved `1` reads back as `0`.                                  |
| `slskd.yml` on disk                       | your settings  | Not proof the process loaded them, and it omits defaults that are load-bearing. Read `GET /api/v0/options`.            |
| slskd `retention.files.*`                 | configured     | Inert on 0.26.0.0. Files past both thresholds are never removed and nothing logs a word.                               |
| `flock -n` in cron (pre-2026-09-04)       | nothing at all | Exited 1 without running the job. A skipped run was byte-identical to a clean one.                                     |
| A cron job exiting 1                      | "partial"      | `cron_job.py --ok-codes` defaults to `0,1`, so exit 1 **cannot alert**. A real fault must exit 2.                      |

## How to actually verify, per stage

Load `.env` and the venv first: `set -a; . ./.env; set +a; . .venv/bin/activate`.
**Lidarr's API is `v1`.** Sonarr/Radarr are `v3`; hitting `v3` on Lidarr returns a bare
`404` that looks exactly like the service being down.

### Did Jellyfin actually act?

`LibraryMonitor … will be refreshed` is an **`[INF]`** line and is logged today — an
earlier doc claimed it was Debug and unverifiable, which was wrong. But it only appears
when the reported path resolves **and the refresh finds a change**, so a
correct-but-unchanged path is indistinguishable from a bogus one.

```bash
LOG=.docker-config/jellyfin/log/log_$(date +%Y%m%d).log
MARK=$(wc -l < "$LOG")
# ... do the thing ...
tail -n +$MARK "$LOG" | grep -E 'LibraryMonitor|LibraryManager: Removing'
```

For a delete, `LibraryManager: Removing item, …` names the item. The bare
`LibraryMonitor … will be refreshed` line does not attribute anything.

**`EnableRealtimeMonitor` is `false` on every library**, so Jellyfin is not watching the
filesystem. That is what makes the attribution clean — nothing but your POST could have
caused the change. Check it before trusting any such test:

```bash
grep -l EnableRealtimeMonitor .docker-config/jellyfin/data/root/default/*/options.xml \
  | xargs grep -H EnableRealtimeMonitor
```

### Safe live test that touches no real media

Copy one small track into a new folder under an **existing** artist (a brand-new artist
costs a full Music scan), announce it, delete it, announce it again, then confirm the
album count returned to where it started.

### Did the setting stick?

Read it back from the **process**, not the file:

```bash
curl -s -H "X-API-Key: $API_KEY_SLSKD" http://localhost:5030/api/v0/options   # slskd
python scripts/check-slskd-effective-config.py                                # pinned subset
```

For \*arr notifications, confirm in the DB (with `-wal`/`-shm`), never by re-`GET`ting.

### Did the job run?

```bash
grep -E "^--- .* exit=" logs/<job>.log | tail -5
cat logs/cron-state/<job>.json          # last_run, last_success, consecutive_failures
```

A `SKIPPED: lock … is held` line is a real outcome, not noise.

## Before you claim it works

1. Name the second observation you made. If you cannot, you have not verified it.
2. Say which of PROVEN / INFERRED / UNVERIFIED your claim is, and mean it.
3. If you tested a guard, **prove the test can fail** — flip the input and watch it go
   red. A gate test that cannot fail is the same bug in miniature, and it has already
   happened twice in this repo.
