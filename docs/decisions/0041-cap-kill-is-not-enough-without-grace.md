# ADR-0041 — CAP_KILL delivers the signal; the grace period is what lets the process act on it

**Date:** 2026-09-15
**Status:** accepted
**Amends:** ADR-0035 — whose conclusion ("jellyfin needs `CAP_KILL`") is correct but
incomplete, and whose fix did not actually produce a clean stop
**Same shape as:** ADR-0004, ADR-0021 — the ungraceful-stop family

## What was broken

ADR-0035 recorded that `jellyfin` stopped badly — **10.5s, exit 137, no shutdown line in
its own log, and a 4.2 MB un-checkpointed SQLite WAL left behind on every stop** — and
attributed it to a missing `CAP_KILL`, because s6-overlay runs as root and must signal
`jellyfin` running as `abc` (uid 1000). The cap was granted and `make check` was taught to
assert it.

On 2026-09-15, stopping `jellyfin` to back it up before the 12.0 → 12.1 upgrade, **with
`CAP_KILL` present and asserted**, produced:

```
>>> graceful stop: 10.542598422s
>>> exit code: 137
-rw-r--r-- 1 tom tom 1417224192 jellyfin.db
-rw-r--r-- 1 tom tom    2785280 jellyfin.db-shm
-rw-r--r-- 1 tom tom    6336592 jellyfin.db-wal
```

Byte for byte the symptom ADR-0035 says was fixed. Jellyfin's own log shows it *began* the
shutdown and never finished:

```
[14:01:06] [INF] Emby.Server.Implementations.Session.SessionManager: Sending shutdown notifications
   ... FinishedAt 14:01:16, exit 137 — exactly ten seconds later
```

## The cause

`jellyfin` declares no `stop_grace_period`, so **Docker's default of 10s** applies. The cap
and the grace period do two different jobs:

- **`CAP_KILL`** lets s6 deliver `SIGTERM` across the uid boundary at all. Without it the
  signal is refused with `EPERM` and the process never learns it should stop.
- **`stop_grace_period`** is how long Docker waits before `SIGKILL` after that signal is
  delivered. Without headroom the process learns it should stop and is then killed partway
  through doing so.

ADR-0035 fixed the first and left the second at a default that a 1.4 GB SQLite database
cannot close inside. The observable outcome is identical to having neither, which is why
the regression was invisible: the two causes share one symptom.

## Why it stayed hidden

Every signal said fine. `docker compose stop` returns 0 on a SIGKILL. The container's next
start is clean, because SQLite recovers the WAL on open. `make check` passed — it asserted
the cap, which was genuinely present. Nothing reads the exit code, and the only durable
trace is a `-wal` file that looks unremarkable next to a database.

The damage is in the backups, not the service: **anything copying `jellyfin.db` without
`-wal`/`-shm` was silently stale**, which is the exact failure ADR-0021 exists to prevent.

## The fix

`stop_grace_period: 120s` on `jellyfin`. Measured immediately after, same container, same
database:

| | stop time | exit | WAL after stop | shutdown line in log |
|---|---|---|---|---|
| CAP_KILL, no grace period | 10.5s | **137** | 6.3 MB left | absent (truncated mid-shutdown) |
| CAP_KILL + `120s` | **3.7s** | **0** | **removed** | `Disposing CoreAppHost` present |

It needs **less** than the default once it is allowed to finish — 3.7s. The 10s ceiling was
not close; it was landing inside the dispose sequence.

## The guard

`make check` grows `cap-kill-needs-grace` (ADR-0041): a service that holds `CAP_KILL` **and
owns a store that checkpoints on close** (`jellyfin`, `qbittorrent`) must declare an
explicit `stop_grace_period` **strictly greater than** Docker's 10s default. Setting it to
exactly `10s` fails too — it buys nothing over setting nothing, and reads like a decision.

`swag` and `playlist-generator` also hold `CAP_KILL`, for retiring nginx workers (ADR-0021),
and own no such store. They **warn** rather than fail: nginx retires workers well inside
10s, but a long-draining connection is killed rather than waited for.

Both faults were proved to fail the check before the fix was committed — the missing key and
the value-equals-default case.

## The general lesson

**A capability grant is a permission, not an outcome.** ADR-0035 verified that the cap was
present; it did not re-measure the stop it was granted to fix. When an ADR's remedy is a
permission, the assertion belongs on the *effect* — exit code 0 and no `-wal` — and
`make check` cannot see either. `make verify-runtime` is where that lives.

The second-order lesson is about ADRs themselves: this one's evidence was quoted
confidently enough ("measured 10.5s and exit 137") that the same numbers appearing again
read as history rather than as a live reading. Re-measure before trusting a fix you did not
watch land.
