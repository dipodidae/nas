# ADR-0004 — qBittorrent requires `CAP_KILL`

**Date:** 2026-09-01
**Status:** accepted
**Background:** `docs/qbittorrent-crash-fix.md` §"Root cause" and
§C "Ungraceful shutdown — real, and the actual root cause"

## The finding

The recurring "qbit crashes and won't come back" cycle was **one** root cause
with several symptoms, and it was a Linux capability problem.

s6-overlay runs as root and has to signal `qbittorrent-nox`, which runs as
`abc` (uid 1000). Signalling a process owned by a different uid requires
`CAP_KILL` — and `cap_drop: ALL` had removed it. Every SIGTERM was refused with
`EPERM`, s6 never forwarded the stop, and Docker SIGKILLed at the end of
`stop_grace_period` **every single time**.

## Measured on this box, 2026-09-01

|                                   | without `KILL` | with `KILL`            |
| --------------------------------- | -------------- | ---------------------- |
| `docker compose stop qbittorrent` | **120.3s**     | **6.2s**               |
| "Saving resume data completed"    | never logged   | logged                 |
| lockfile + ipc-socket after stop  | left behind    | removed by qbit itself |

Re-measure with `make measure-qbittorrent-stop` as the torrent count grows;
~6s was at 128 torrents. Raise `stop_grace_period` only if the measured time
approaches it.

## Corrects an earlier note

An older note in this repo said the crash cycle was _not_ a permissions
problem. **It was** — a Linux capability one, not a file-ownership one. The
stale lockfile was only the symptom: because every stop was a hard kill, qbit
orphaned its `lockfile` and `ipc-socket` every time. With `CAP_KILL` it removes
both itself, so the stale-lock path is now the exception rather than the rule.

## Decision

`cap_add: [KILL]` on qbittorrent, on top of the LSIO set. **Do not remove it.**

`FOWNER` and `FSETID` were tested and are **not** needed — `KILL` alone is
sufficient. They are deliberately not granted, and the invariant checker
asserts their absence so a future "let's just add capabilities until it works"
pass cannot quietly widen the grant.

## Recurrence tell

If the s6 tight loop ever returns, the signature is **rapidly incrementing PIDs
in `qbittorrent.log`** while the container reports `Up (unhealthy)`.

## Backstops that remain

- `stop_grace_period: 120s` — now generous headroom, not a timeout that gets hit
- `qbittorrent/custom-cont-init.d/01-clear-stale-lockfile.sh` — ADR-0005
