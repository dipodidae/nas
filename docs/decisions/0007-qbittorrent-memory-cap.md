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
