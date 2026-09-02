# ADR-0005 — qBittorrent: pinned tag, stable hostname, proven-stale lock clearing

**Date:** 2026-09-01
**Status:** accepted
**Background:** `docs/qbittorrent-crash-fix.md` §A "Stale lockfile — real, but
not the version bug" and §"The init script's staleness rules"

## Pinned image tag, floor >= 5.2.2

`lscr.io/linuxserver/qbittorrent:5.2.3_v2.0.14-ls473` — pinned, not `:latest`.

qBittorrent 5.2.0 and 5.2.1 carry upstream **#24357**: the `QLockFile` lockfile
records the container hostname, and a Docker recreate (which changes the
hostname to the new container ID) leaves qbit unable to prove the lock is
stale, so it refuses to start. Fixed by **PR #24363** in 5.2.2.

Pinning also keeps Watchtower from silently moving this container onto a
regressed build overnight — see ADR-0006, where Watchtower is separately
opted out.

**To update:** bump the tag deliberately, then `make update-qbittorrent`, and
watch it come back healthy before walking away.

## Stable hostname

`hostname: qbittorrent`. Belt-and-braces for #24357: even on a fixed version
this keeps the lockfile's hostname line meaningful, and it is what the
custom-init staleness check compares against.

**If qbittorrent is ever put behind a VPN sidecar** via
`network_mode: service:<vpn>`, it inherits that container's network namespace
*and* hostname — pin the hostname on the VPN service instead, because this line
would then have no effect. (No VPN today: ADR-0019.)

## The stale-lock init script proves staleness first

`qbittorrent/custom-cont-init.d/01-clear-stale-lockfile.sh` clears a **stale**
single-instance lock at init so an ungraceful kill cannot trap qbit in an s6
crash loop. Since 2026-09-01 it **proves staleness before deleting** — a 0-byte
pre-5.2.0 file, a hostname mismatch, or a dead PID — rather than
unconditionally `rm -f`-ing.

**Do not reintroduce an unconditional delete.** A lock held by a live instance
must be preserved.

This is a backstop for ADR-0004, not a substitute for it.
