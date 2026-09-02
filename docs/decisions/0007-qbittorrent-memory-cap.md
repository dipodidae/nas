# ADR-0007 — qBittorrent `mem_limit: 4g`

**Date:** 2026-09-01
**Status:** accepted (backstop)
**Background:** `docs/qbittorrent-crash-fix.md` §B "libtorrent memory — real
and severe"

## Why a scoped exception to ADR-0001

libtorrent 2.x mmaps torrent data and the kernel keeps those pages as page
cache. With OS cache enabled this container's cgroup peaked at **21.1 GB**
(journalctl, container scope, 2026-09-01) and contributed to host-wide OOM
kills.

## The real fix, and why the cap is still here

The real fix is `Session\DiskIOReadMode` / `DiskIOWriteMode = DisableOSCache`
in `qBittorrent.conf`, set 2026-09-01.

`mem_limit: 4g` is the **backstop if that is ever reverted through the WebUI** —
which is a thing a person can do by accident. Steady state is ~1.2 GB, so 4g is
roughly 3x headroom.

## Invariant

`memswap_limit == mem_limit`, so it cannot balloon into host swap and thrash
everything else first. Checked by `scripts/check-invariants.sh` for every
service that sets `mem_limit`.

## `DiskIOType` — measured 2026-09-02, and deliberately NOT flipped

**Status: question closed.** `DisableOSCache` plus `mem_limit: 4g` is the
accepted position. `Session\DiskIOType` stays at its default.

`DisableOSCache` is the mitigation, not the removal: libtorrent 2.x still mmaps
torrent data on the default `DiskIOType`, and the kernel still accounts those
pages to the cgroup. The open question was whether switching to the
POSIX-compliant backend — which removes the mmap mechanism, at a throughput
cost — was worth taking.

### The measurement

Two readings of the cgroup's own accounting, an hour apart, plus its pressure
counters:

```
                    17:1x        18:3x
anon              33.9 MB      39.9 MB     the process itself
file               4.22 GB      4.21 GB    page cache charged to this cgroup
file_mapped        3.06 GB      2.24 GB    of which mmap'd
memory.current     4.29 GB      4.29 GB    pinned at the 4g limit

# memory.events, over ~6.5 h uptime, restarts=0, OOMKilled=false
max        40277     times the cgroup hit its limit
oom            0
oom_kill       0
```

### What that says

The cgroup sits at its ceiling permanently — page cache expands to fill whatever
it is given, which is normal — and it has hit that ceiling **40,277 times while
OOM-killing exactly zero times.** Every reclaim succeeded. `file_mapped` falling
from 3.06 GB to 2.24 GB between the two readings is that reclaim happening.

So the mmap'd pages are **reclaimable page cache, not a leak**, and the resident
process is tiny (~40 MB of anon against gigabytes of file). The 21.1 GB peak was
unbounded growth in the _absence_ of a limit; with the limit it is bounded,
healthy, and costs nothing.

Switching `DiskIOType` would therefore remove a mechanism that is not doing harm,
in exchange for a real throughput cost. **Not worth taking.** There was also no
meaningful throughput to measure against on the day (0 KiB/s down, 93 KiB/s up,
3 of 37 torrents active), so a before/after would have compared two idle states
and proved nothing.

### If this is ever revisited

The number that would reopen it is `oom_kill` becoming non-zero, or
`memory.current` staying pinned while `anon` — not `file` — is what grows. Raise
`mem_limit` or take the POSIX backend then; until one of those happens, the cap
is doing its job.

`scripts/check-invariants.sh` reports `disk_io_type` as a **pass** carrying this
decision rather than a standing warning, so the position is visible without
nagging. `memswap_limit == mem_limit` remains the hard invariant.
