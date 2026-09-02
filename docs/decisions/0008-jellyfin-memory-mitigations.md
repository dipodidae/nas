# ADR-0008 — Jellyfin's three memory mitigations (all stopgaps)

**Date:** 2026-09-01
**Status:** accepted (containment, not a fix — leak not root-caused)
**Background:** `docs/jellyfin-playback-audit.md` §1 "The memory problem
(separate, real, fixed)" and the heap dumps in `docs/jellyfin-heap-dumps/`

## The problem

Confirmed via `journalctl -k` that **the jellyfin process itself** — not
ffprobe/ffmpeg, verified from the kernel's per-task RSS table — leaks to
**~22–24 GB anon-RSS** and gets killed by a **global (host-wide)** OOM-killer
every ~2–12 h. Being host-wide, it takes down unrelated containers' health
checks as collateral.

## Mitigation 1 — `mem_limit: 10g` / `memswap_limit: 10g`

A stopgap containing the blast radius while the leak is root-caused. Scoped
exception to ADR-0001. `memswap_limit == mem_limit` so it cannot balloon into
host swap and thrash everything else before finally being killed.

## Mitigation 2 — `MALLOC_ARENA_MAX=2`

H3 test: glibc malloc arena fragmentation, matching **dotnet/runtime#122027** —
native RSS growth while the managed heap stays flat, confirmed via heap dump.

glibc sizes arenas from the **host's** online CPU count (16 here), not the
container's cgroup quota, so `mem_limit` alone cannot restrain this. Capping
the arena count trades allocator concurrency for memory. A/B result
before/after is in the audit doc.

**Revert:** delete the line.

## Mitigation 3 — `DOTNET_EnableWriteXorExecute=0`

W^X (write-xor-execute) A/B test for the `memfd:doublemapper` native-memory
contributor — **dotnet/runtime#89776 / #121455** — confirmed present and
growing independently of the arena cap above.

Disables .NET's double-mapping of JIT'd code (write-then-remap-executable) in
favour of a single writable+executable mapping. A real, if small, hardening
tradeoff, accepted here: a LAN-only media server behind SWAG that executes no
untrusted code. A/B result recorded in the audit doc.

**Revert:** delete the line.

## Tried and reverted: the OpenCL tone-mapping mod

It needs `CAP_FOWNER`, which this hardened container correctly does not grant,
and nothing today actually needs HDR tone-mapping. **VPP tone-mapping is the
path to use** if that ever changes — it works today and needs no extra
capabilities.

## Related

Jellyfin is Watchtower-opt-out (ADR-0006) partly because of this: a service
that is slow to stop is exactly the one Watchtower's non-atomic recreate loses.
