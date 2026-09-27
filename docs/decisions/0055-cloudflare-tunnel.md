# ADR-0055 — The public surface is a Cloudflare Tunnel

**Date:** 2026-09-27
**Status:** accepted; **live since 2026-09-27** (cutover held for a few hours; see "The account takeover")
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
DNS never answers with the home IP. **SWAG publishes on `127.0.0.1:443` only**
(no port 80 at all), so nothing on the home IP answers 80/443 even if a router
port-forward is left open. The origin cannot be reached around Cloudflare.
`make check` fails if 80 or 443 is published publicly again (they were removed
from `PUBLIC_PORT_ALLOWLIST`). HTTP→HTTPS is Cloudflare's Always Use HTTPS.

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

## Zone hardening (`4eva.me`)

SSL **Full (strict)**, minimum TLS **1.2**, Always Use HTTPS, 0-RTT off. The HTML
rewriters are off (Email Obfuscation, Server-Side Excludes, Automatic HTTPS
Rewrites), because no CDN should edit an app's markup. A **cache rule** set to `true → bypass`
means Cloudflare never stores a response, so a protected asset cannot be served
from the edge to someone the door would have refused. **CAA** allows issuance by
`letsencrypt.org` only, and Cloudflare appends its own CAs itself (verified with `dig`).
**DNSSEC** is signed, and is `pending` until the DS record below is added at the registrar:

    4eva.me. 3600 IN DS 2371 13 2 17297B1CB299675E9EABC6E9EE1A36AC4D24231A5BA950F08F5DD0F064904446

## The account takeover

Wiring this up found that the Cloudflare account had been **taken over on
2026-08-05**. A dashboard session as the account owner, from `45.128.99.35`,
created a catch-all dynamic redirect (`true → https://verificator.cc/verify?d=<host>`)
in all five zones. It was live on `ongehoord.org`, whose records are proxied, for
seven weeks. It stayed dormant everywhere else only because those records were
grey-cloud. All five rules were deleted on 2026-09-27.

A tunnel makes Cloudflare the only door, so **whoever holds the account holds
every route, the tinyauth login page included**. Until the account has 2FA, a new
password, revoked sessions and audited tokens, exposing the home IP is the
smaller risk. The cutover waited until the account had 2FA (verified through the
API on 2026-09-27), then went: one canary hostname first (`sonarr`, where the door, a
body-carrying POST and the real client IP in SWAG's log were all proven through the
tunnel), then the apex and the wildcard, then SWAG went to loopback.

**Browser Integrity Check is off** for `jellyfin`, `nextcloud`, `ntfy` and every
`/api*` and `/rest*` path (a configuration rule). TV, phone and sync clients and the \*arr apps are not
browsers, and a challenge page is a silent outage for them.

**Diagnostic trap.** After the flip, this host's systemd-resolved kept stale answers,
some of them IPv6-only, and this host has no IPv6 route. So `check-door-live.sh`
reported routes as `000` while every public resolver was already correct.
`resolvectl flush-caches` fixed it. Check `dig @1.1.1.1` before believing a `000`.

## Known costs

- Cloudflare's terms discourage serving video on the Free plan. Jellyfin streams through the
  tunnel anyway, by choice (2026-09-27).
- A request body is capped at 100 MB. Nextcloud's web uploader is set to 50 MB chunks
  (`occ config:app:set files max_chunk_size --value 52428800`). Desktop clients keep
  their own `maxChunkSize` in `nextcloud.cfg`, which must be 50 MB or less.
- fail2ban's iptables bans stop working after cutover, because every packet comes from
  cloudflared (which `ignoreip` 172.16/12 already exempts). WAF rules would replace them.
