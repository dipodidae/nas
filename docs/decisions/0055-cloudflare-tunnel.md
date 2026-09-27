# ADR-0055 — The public surface moves behind a Cloudflare Tunnel (staged, not yet live)

**Date:** 2026-09-27
**Status:** accepted; **cutover pending** (see "Why it is not live yet")
**Extends:** ADR-0022 (the conf is the mechanism), ADR-0034 (the door)

## Decision

`4eva.me` and `*.4eva.me` will be proxied CNAMEs to the Cloudflare Tunnel
`nas-4eva` (`54dec822-…`), run by the `cloudflared` service in
`compose/infra.yaml`. The tunnel is remotely managed. Its ingress lives in
Cloudflare, not in this repo:

| hostname    | service            | originRequest                                   |
| ----------- | ------------------ | ----------------------------------------------- |
| `4eva.me`   | `https://swag:443` | `originServerName: 4eva.me`, `http2Origin: true` |
| `*.4eva.me` | `https://swag:443` | same                                            |
| (catch-all) | `http_status:404`  |                                                 |

SWAG keeps every job it had: routing, TLS to the origin and the tinyauth door.
After cutover the router forwards nothing on 80/443, and DNS never answers
with the home IP. Today it answers `86.81.35.107`.

## Invariants

- **`cloudflared` is `172.30.0.250`, static.** `swag/site-confs/cloudflared-realip.conf`
  trusts `CF-Connecting-IP` from that address only (`set_real_ip_from` takes no
  hostnames). Without it every visitor is cloudflared. tinyauth's per-IP lockout
  (3 tries per 300 s) would then lock everyone out after one stranger's typos, and nginx logs and
  fail2ban would see one client. Trusting the whole /24 would let any container,
  or a hairpinned LAN client, forge its address.
- **The token is a 0600 file mounted `:ro`** (`secrets/cloudflared-token`), never
  `TUNNEL_TOKEN` in the environment (ADR-0011). The container runs as
  `${PUID}:${PGID}` so it can read it, with no capabilities and a read-only root.
- **Pinned** (`MANUAL_UPDATE_ONLY`). It will be the only way in, and a bad connector
  closes every route at once, including the ones that are never gated.
- **No `autoheal=true`.** `/ready` is false whenever Cloudflare's edge is
  unreachable. A restart cannot fix that, and cloudflared reconnects by itself.

## Zone hardening already applied (`4eva.me`)

These take effect only for proxied records, so they are live and inert until cutover:
SSL **Full (strict)**, minimum TLS **1.2**, Always Use HTTPS, 0-RTT off. The HTML
rewriters are off (Email Obfuscation, Server-Side Excludes, Automatic HTTPS
Rewrites), because no CDN should edit an app's markup. A **cache rule** set to `true → bypass`
means Cloudflare never stores a response, so a protected asset cannot be served
from the edge to someone the door would have refused. **CAA** allows issuance by
`letsencrypt.org` only, and Cloudflare appends its own CAs itself (verified with `dig`).
**DNSSEC** is signed, and is `pending` until the DS record below is added at the registrar:

    4eva.me. 3600 IN DS 2371 13 2 17297B1CB299675E9EABC6E9EE1A36AC4D24231A5BA950F08F5DD0F064904446

## Why it is not live yet

Wiring this up found that the Cloudflare account had been **taken over on
2026-08-05**. A dashboard session as the account owner, from `45.128.99.35`,
created a catch-all dynamic redirect (`true → https://verificator.cc/verify?d=<host>`)
in all five zones. It was live on `ongehoord.org`, whose records are proxied, for
seven weeks. It stayed dormant everywhere else only because those records were
grey-cloud. All five rules were deleted on 2026-09-27.

A tunnel makes Cloudflare the only door, so **whoever holds the account holds
every route, the tinyauth login page included**. Until the account has 2FA, a new
password, revoked sessions and audited tokens, exposing the home IP is the
smaller risk. Cutover is: point both records at `<tunnel-id>.cfargotunnel.com`
(proxied), prove the door with a body-carrying request (ADR-0036 sequel), then
close 80/443 on the router and bind SWAG's `ports:` to the LAN.

## Known costs

- Cloudflare's terms discourage serving video on the Free plan. Jellyfin streams through the
  tunnel anyway, by choice (2026-09-27).
- A request body is capped at 100 MB, so Nextcloud sync clients need `maxChunkSize` of 50 MB or less.
- fail2ban's iptables bans stop working after cutover, because every packet comes from
  cloudflared (which `ignoreip` 172.16/12 already exempts). WAF rules would replace them.
