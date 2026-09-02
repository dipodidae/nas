# ADR-0001 — The hardening baseline

**Date:** 2026-09-02 (records a standing convention, not a new change)
**Status:** accepted

## Decision

Every service gets, unless there is a recorded reason not to:

- `security_opt: [no-new-privileges:true]`
- `cap_drop: [ALL]` plus a selective `cap_add`
- a WebUI published on `127.0.0.1` only — the public surface is SWAG, plus the
  P2P ports (qBittorrent 6881, slskd 50300) and Jellyfin's LAN ports
  (8096, 8920, 7359, 1900)
- a `curl -f` / `wget --spider` healthcheck
- capped container logs: `json-file`, `max-size: 10m`, `max-file: "2"`
- `tmpfs` for `/tmp` where the image supports it

The standard linuxserver.io capability set is `CHOWN`, `SETUID`, `SETGID`,
`DAC_OVERRIDE` — what the LSIO root init needs to chown `/config` and drop to
`abc`. The app runs unprivileged afterwards.

Enforced mechanically by `scripts/check-invariants.sh` (`make check`).

## Pi-era resource limits were removed

This stack previously ran on a Raspberry Pi and carried `mem_limit`,
`mem_reservation`, `cpus`, `blkio_config` and `ulimit` tuning throughout. All of
it was removed for the MS01 host (32 GB RAM). **Do not reintroduce these
globally.**

Two scoped exceptions exist, both stopgaps containing a leak rather than
capacity planning, and both documented: qBittorrent (ADR-0007) and Jellyfin
(ADR-0008). Where a `mem_limit` exists, `memswap_limit` must equal it, so the
container cannot balloon into host swap and thrash everything else before
finally being killed.

## Recorded exceptions to the baseline

| Service | Exception | Reason |
|---|---|---|
| qbittorrent | `+ KILL` | ADR-0004 |
| swag | `+ NET_BIND_SERVICE` (listed first) | binds :80/:443 |
| nextcloud | `+ NET_BIND_SERVICE` | bundled nginx binds :443 in-container |
| nextcloud | logging 25m/3 | far chattier than anything else here |
| jellyseerr | `+ FOWNER` | init chowns files it does not own in `/app/config` |
| lingarr | LSIO set minus `DAC_OVERRIDE` | does not need permission bypass |
| lidarr-bulk | LSIO capabilities without PUID/PGID | not an LSIO image; entrypoint chowns then `su-exec`s to `node` |
| playlist-generator, its db | **no `cap_drop`** | known gap, ADR-0018 |
