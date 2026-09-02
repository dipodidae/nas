# ADR-0008 — Jellyfin's memory blowup: not scan-correlated, and contained

**Date:** 2026-09-01, **rewritten 2026-09-02** after measurement
**Status:** accepted — one hypothesis eliminated, mechanism narrowed, no
recurrence in 30 h; `mem_limit` reclassified as defence in depth
**Measurement:** `docs/jellyfin-memory-investigation.md`
**Background:** `docs/jellyfin-playback-audit.md` §1, heap dumps in
`docs/jellyfin-heap-dumps/`

## The problem

The **jellyfin process itself** grew to **22.8–24.0 GB anon-RSS** and was killed
by the **host-wide** OOM killer six times between 2026-08-30 and 2026-09-01.
Being global rather than cgroup-scoped, each kill took unrelated containers'
health checks down with it.

## What changed in this rewrite

The previous version of this record said the leak was "not root-caused" and
described all three mitigations as stopgaps. One named hypothesis has since been
tested and **eliminated**, and the mitigations' status has changed with it.

### Eliminated: ffprobe fan-out during library scans

Upstream (jellyfin/jellyfin#16048, #16549) attributes 10.11.x blowups to ffprobe
fan-out during scans, with parallel-thread caps as the cure. Two independent
findings refute it here:

1. **Every one of the six kills names a single process, `task=jellyfin`,**
   holding 22.8–24.0 GB of anonymous memory. A fan-out is many children with
   modest RSS; the killer takes the largest, so it would eventually have named
   `ffprobe`. `ffprobe` appears nowhere in the kernel log.
2. **Zero of six kills coincide with a scan.** Crons are Fri/Sat/Sun 05:05; the
   kills are Sun 10:15, Sun 22:01, Mon 10:10, Mon 19:40, Mon 21:57 and
   **Tuesday** 05:34 — a day with no scan at all.

A deliberate Movies scan on 2026-09-02 with no playback held anon at
411–423 MB with **zero** ffprobe/ffmpeg children. That run only proves a scan
with nothing to probe is harmless — it was deliberately **not** escalated to a
forced refresh, because `replaceAllMetadata` would re-scrape a library whose
wrong-match incident had been corrected the day before. The kernel evidence
already settles it.

**Consequence: do not cap Jellyfin's scan/extraction concurrency.** It would be
a change with no measured basis, and it would make a future reader believe the
scan path was implicated.

### Remaining mechanism: in-process native growth

One process, 23 GB resident against a **277 GB virtual** address space — a very
large number of mappings rather than one runaway allocation. That is the
signature the two remaining mitigations address.

## Mitigation 1 — `mem_limit: 10g` / `memswap_limit: 10g` → **defence in depth**

Reclassified. Post-mitigation peak anon is **2.20 GB** over 1,870 samples, well
under the 10 g cap, so the cap is no longer doing the work — it is the backstop
that turns a recurrence into a _container_ OOM instead of a host-wide one, which
is the difference between losing Jellyfin and losing every other service's
health check.

Keep it. `memswap_limit == mem_limit` so it cannot balloon into host swap and
thrash everything else before finally being killed. Scoped exception to
ADR-0001, asserted by `make check`.

## Mitigation 2 — `MALLOC_ARENA_MAX=2` → **probable fix**

glibc malloc arena fragmentation, matching **dotnet/runtime#122027**: native RSS
grows while the managed heap stays flat, confirmed via heap dump. glibc sizes
arenas from the **host's** online CPU count (16 here), not the container's cgroup
quota, which is why `mem_limit` alone could never restrain it.

`arena_regions=0` throughout the post-mitigation window.

**Revert:** delete the line. Doing so is how you would A/B this properly, and
also how you would provoke another host-wide OOM cascade — see the honest limits
below.

## Mitigation 3 — `DOTNET_EnableWriteXorExecute=0` → **probable fix**

W^X double-mapping of JIT'd code, **dotnet/runtime#89776 / #121455**, confirmed
present and growing independently of the arena cap. Disables .NET's
write-then-remap-executable double mapping in favour of a single
writable+executable mapping.

A real if small hardening tradeoff, accepted: a LAN-only media server behind
SWAG that executes no untrusted code. `doublemapper=0` throughout the
post-mitigation window.

**Revert:** delete the line.

## Honest limits

The sampler's history begins **after** the mitigations landed, so there is no
pre-mitigation series from this instrument. "The kills stopped when the
mitigations landed" is a strong temporal association — six kills in 43 h, then
zero in 30 h with a 10× lower peak — but not proven causation. Settling it means
reverting a mitigation and waiting for a 24 GB excursion, i.e. deliberately
provoking another host-wide OOM. Not worth the answer.

**What reopens this record:** an OOM kill naming `jellyfin` again, or `anon`
climbing past ~4 GB while `arena_regions` or `doublemapper` go non-zero.
`scripts/stack_watchdog.py` watches the anon threshold and the kernel log, and
alerts if the sampler itself goes stale — a monitor that quietly stops is worse
than none.

## Tried and reverted: the OpenCL tone-mapping mod

It needs `CAP_FOWNER`, which this hardened container correctly does not grant,
and nothing today needs HDR tone-mapping. **VPP tone-mapping is the path to use**
if that changes — it works today and needs no extra capabilities.

## Related

Jellyfin's tag is pinned and updates are `make pull-jellyfin` (ADR-0006). Since
ADR-0025 nothing in the stack can recreate a container at all; `diun` reports
new Jellyfin tags despite the pin (ADR-0024).
