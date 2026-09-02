# ADR-0019 — No VPN: P2P egresses over the home IP

**Date:** 2026-07-27 (VPN removed); recorded 2026-09-02
**Status:** accepted

## Decision

`slskd` and `qbittorrent` are plain `nas-network` members and egress over the
host's home IP directly. The gluetun WireGuard sidecar (AirVPN) that used to
tunnel both was **removed on 2026-07-27**, together with `vpn-configs/` and the
`WIREGUARD_*` / `AIRVPN_*` entries in `.env`.

## Consequences

Each service publishes its own ports, and **inbound P2P requires router port
forwarding** — there is no VPN remote-port-forward doing it any more:

- qBittorrent `6881/tcp` + `6881/udp` (BitTorrent), `127.0.0.1:8080` (WebUI)
- slskd `50300/tcp` (Soulseek), `127.0.0.1:5030` (WebUI)

Lidarr, Sonarr, SWAG and qui resolve `qbittorrent:8080` / `slskd:5030` as
ordinary service DNS.

## Historical caveat, if Soulseek logins start timing out

**slskd on the home IP is what got the IP soft-blocked by slsknet before** —
the tunnel was originally introduced for exactly that reason. If login timeouts
become persistent rather than transient, this is the first thing to suspect,
and it is a different problem from the ghost-session spiral in ADR-0009.

## Unaffected

**Prowlarr was never tunneled.** Its Cloudflare-protected indexers still route
through `byparr` (FlareSolverr-compatible), targeted by the `cloudflare` tag in
Prowlarr.

## If a VPN is ever reintroduced

See ADR-0005: putting qbittorrent behind `network_mode: service:<vpn>` makes it
inherit the sidecar's network namespace **and hostname**, which silently
disables qbittorrent's `hostname: qbittorrent` line and the lockfile staleness
check that depends on it. Pin the hostname on the VPN service instead.
