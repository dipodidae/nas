# ADR-0043 — AdGuard Home binds one address, and the host does not use it

**Date:** 2026-09-15
**Status:** accepted
**Same shape as:** ADR-0040 — a published port that is not simply "free"

## Context

A network-wide DNS filter is useful for every device in the house, and for the phone over
WireGuard. AdGuard Home is the obvious candidate. It is the first service in this stack
whose job is _infrastructure for other machines_ rather than an app this box serves, and
that changes three things: which address it binds, whether this host trusts it, and what
its healthcheck can honestly claim.

## Decisions

### 1. It binds `${ADGUARD_DNS_BIND_IP}:53`, never `0.0.0.0:53`

`systemd-resolved` is active on this host and holds the stub listener on `127.0.0.53:53`.
Measured 2026-09-15, with the image already pulled:

```
$ docker run --rm -p 53:53/udp <image> true
failed to bind host port 0.0.0.0:53/udp: address already in use

$ docker run --rm -p 192.168.2.56:53:53/udp <image> true    # BIND OK
$ docker run --rm -p 10.231.245.1:53:53/udp <image> true    # BIND OK
```

Upstream's documented answer is to disable `DNSStubListener` in
`/etc/systemd/resolved.conf.d/`. That is a host file outside this repo — precisely the
ADR-0040 hazard, where a rebuild silently loses it and `make check` cannot see it. Binding
a specific address needs no host change at all, so that is what we do.

### 2. WireGuard peers use the SAME address; there is no `10.231.245.1` binding

The tempting second binding is rejected. A specific-IP bind requires the address to
**exist** when Docker binds it, and the two units have no ordering between them:

```
wg-quick@wg0:   Before = multi-user.target shutdown.target
                After  = ... network-online.target
docker.service: After  = network-online.target
```

Both merely wait for the network and then race. If Docker won, AdGuard would fail with
`cannot assign requested address` — and since AdGuard is the _house's_ resolver, a slow VPN
interface would present as "the internet is down" on every device. ADR-0040's second trap
then compounds it: after a failed start, `docker compose up -d` only _starts_ the created
container without rebuilding its sandbox, reporting `Started` for a container attached to
no network.

Peers reach `192.168.2.56` over the tunnel instead. Verified on this host:

```
ip_forward   = 1
INPUT policy = ACCEPT  (no rules, no ufw)
rp_filter    = 2 on wg0 (loose)
```

A packet from `10.231.245.2` to `192.168.2.56` arrives on `wg0` addressed to a **local**
address, so it never touches FORWARD — it goes to INPUT, which accepts. The cost is that
each peer's own config needs `DNS = 192.168.2.56` and an `AllowedIPs` covering the LAN;
WireGuard has no server-side DNS push, so that is client-side by design and lives outside
this repo.

### 3. This host does NOT resolve through AdGuard

`/etc/resolv.conf` stays on systemd-resolved pointing upstream. If the host resolved
through AdGuard, an AdGuard outage would leave `docker compose pull` unable to resolve the
registry needed to fix it — a bootstrap deadlock whose only exit is a physical console.
The filtering is for clients, not for the box that runs it.

### 4. The healthcheck probes the admin UI, and `autoheal` is why

Measured: **AdGuard does not bind `:53` at all until the setup wizard has been completed.**
A DNS-based healthcheck would therefore report unhealthy on a fresh install — and with
`autoheal=true` that means being restarted every ~30s while you are trying to complete the
wizard, which is unfinishable. The web port answers in both states (302 to `/install.html`
before setup, 302 to `/login` after) and busybox wget follows the redirect.

So the healthcheck claims "the process is serving HTTP" and **nothing more**. That `:53`
actually answers is a runtime fact and belongs in `make verify-runtime`, which queries it
for real. Same honesty as the `streamystats-jobs` `/proc/net/tcp` probe.

### 5. Consequences recorded

- **`NET_BIND_SERVICE` is required.** `:53` is privileged and `cap_drop: ALL` removes the
  capability even from a root process. Without it the DNS listener never binds and only the
  admin UI comes up — which still passes the healthcheck.
- **The admin UI publishes on host port `3053`, not `3000`.** `lidarr-bulk` already holds
  `127.0.0.1:3000`; compose renders the collision without complaint and the second
  container to start simply fails to bind, looking broken rather than conflicted
  (ADR-0023). `make check`'s `port-collision` caught this during the change. The
  **container** port stays 3000, which is what the proxy-conf and healthcheck target.
- **Keep the admin port at 3000 inside the container — this happened, within the hour.**
  The setup wizard offers to move the admin interface and its own suggestion is **80**.
  Accepting it wrote `address: 0.0.0.0:80` into `AdGuardHome.yaml`, and
  `adguardhome.4eva.me` began returning **502**:

  ```
  inside the container   :  LISTEN :53, LISTEN :80   (nothing on 3000)
  swag -> adguardhome:3000 -> 000     swag -> adguardhome:80 -> 302
  nginx error.log: connect() failed (111: Connection refused)
                   while connecting to upstream, upstream: "http://172.30.0.35:3000/"
  ```

  Note the shape: a **502**, not a 500 — the tinyauth door worked fine and the _app_ was
  unreachable behind it. The healthcheck did catch it honestly (`FailingStreak: 1` within a
  minute), but a healthcheck that can never pass plus `autoheal=true` is a **restart loop a
  restart cannot fix** — the ADR-0009/ADR-0026 slskd shape, reached through configuration
  rather than through a login handshake.

  The port must be **the same before and after setup**, because the wizard itself is meant
  to be completed through SWAG behind the door, and AdGuard always serves the wizard on 3000. So 3000 is the only value that works in both states, and 80 is not a valid
  alternative here even though it is upstream's post-setup default.

  `make verify-runtime` now reads `http.address` out of the live container and fails,
  naming this cause, if it is ever not 3000.

- **The config dirs stay `root:root`.** AdGuard runs as root and is _not_ given a `user:`
  override, so Docker's default bind-mount creation is correct. It is deliberately absent
  from `make bootstrap`'s chown loop — the ADR-0023 `scrutiny` case. A `tom`-owned dir would
  be _worse_ than root-owned here: with `cap_drop: ALL` the root process has no
  `DAC_OVERRIDE`, so it falls through to the "other" permission bits and cannot write.
- **The tag is pinned and manual-update-only.** AdGuard rewrites `AdGuardHome.yaml` to a
  newer `schema_version` on first start after an upgrade and ships no downgrade path, so
  the old tag will not read what the new one wrote. Copy
  `${CONFIG_DIRECTORY}/adguardhome/conf` before bumping.
- **The route is `protect`** with no path-scoped exemption. AdGuard's entire HTTP surface
  is its admin UI, its `/control/*` API is that same surface by another name, and nothing
  off-box consumes it. ADR-0034.
