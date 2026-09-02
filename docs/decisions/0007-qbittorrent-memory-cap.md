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

## `DiskIOType` — measured 2026-09-02, not yet flipped

`DisableOSCache` is the **mitigation**, not the removal. libtorrent 2.x still
mmaps torrent data on the default `Session\DiskIOType`, and the kernel still
accounts those pages to the cgroup. `Session\DiskIOType` does not appear in
`qBittorrent.conf` at all, i.e. it is at its default; the live API agrees
(`disk_io_type = 0`).

First reading of the cgroup's own accounting, taken with the settings as they
stand (16 active torrents):

```
# /sys/fs/cgroup/system.slice/docker-<qbittorrent>.scope/memory.stat
anon           33.9 MB     the process itself
file            4.22 GB    page cache charged to this cgroup
file_mapped     3.06 GB    of which mmap'd -- 73% of the page cache
memory.current  4.29 GB    pinned at the 4g limit
```

So the hypothesis holds on its first half: **the mmap'd pages are the bulk of
the footprint, and the resident process is not** — 33.9 MB of anon against
3.06 GB of mapped file. The cgroup sits at its limit and reclaims continuously
rather than growing, which is `mem_limit` doing exactly the backstop job it was
added for.

The second half is untested: switching `DiskIOType` to the POSIX-compliant
backend removes the mmap mechanism but has a throughput cost, and that cost has
not been measured here. **The setting is therefore left at its default.** The
decision rule if it is revisited: flip only if a before/after over a full day
shows `file_mapped` falling by most of that 3.06 GB *and* no drop in observed
download/seed throughput. `scripts/check-invariants.sh` reports `disk_io_type`
as a **warning**, not a rule, so the open question stays visible without
blocking `make check`.
