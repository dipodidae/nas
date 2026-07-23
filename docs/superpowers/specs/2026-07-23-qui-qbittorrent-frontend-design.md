# qui — modern web front-end for qBittorrent

**Date:** 2026-07-23
**Status:** Approved (design)

## Problem / goal

Replace the qBittorrent built-in WebUI as the interface we actually use with
[`autobrr/qui`](https://github.com/autobrr/qui) — a fast, modern web UI for
qBittorrent. `qui` is **not** a torrent client: it has no BitTorrent engine and
runs *on top of* an existing qBittorrent daemon, talking to it over the
qBittorrent WebUI API. So the qBittorrent daemon stays exactly as-is; qui
becomes the front door and its own built-in WebUI is retired as a public
surface.

## Constraints (from the current stack)

- qBittorrent runs inside gluetun's network namespace (`network_mode:
  service:gluetun`). gluetun aliases `qbittorrent` on `nas-network` at `:8080`,
  which is how the *arr clients already reach it.
- **Three host cron scripts** talk to qBittorrent at `http://localhost:8080`
  relying on qBittorrent's *localhost auth-bypass*:
  `qbittorrent_settings_enforce.py`, `qbittorrent_stalled_kickstart.py`,
  `media_ops_status.py`. These depend on the `127.0.0.1:8080:8080` loopback
  publish on the gluetun block. That publish is loopback-only (never public)
  and **must stay**.
- The public surface is SWAG's wildcard `*.${PUBLIC_DOMAIN}` (`4eva.me`).
  Services are published by a `swag=enable` label; qBittorrent has **no** such
  label, so the auto-proxy has never generated a public route for it. Any
  public route to qbit's WebUI, if one exists, is a *hand-written* proxy-conf in
  the live SWAG config (`${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/`), which is
  not tracked in this repo.

## Design

### 1. New `qui` service in `docker-compose.yml`

A plain `nas-network` service — **not** in gluetun's netns. qui is only an API
client of qBittorrent, so it sits on the normal bridge and reaches the daemon
at `qbittorrent:8080` (gluetun's alias), the same way the *arr clients do.

Key facts confirmed from qui's docs (getqui.com):

- Image `ghcr.io/autobrr/qui:latest`, listens on `7476`.
- Config lives in `/config`: `config.toml`, logs, and `qui.db` (SQLite).
- **No PUID/PGID support.** The container's user is set directly (docs show
  `--user="99:100"` on Unraid). We set `user: "${PUID}:${PGID}"` so it can own
  `${CONFIG_DIRECTORY}/qui` (same approach slskd already uses).
- Relevant env vars (all prefixed `QUI__`, double underscore):
  `QUI__HOST`, `QUI__PORT`, `QUI__SESSION_SECRET` (auto-generated if unset —
  which would log everyone out on every restart, so we pin it),
  `QUI__BASE_URL` (subpath reverse-proxy — not needed, we serve at a subdomain
  root), `QUI__LOG_LEVEL`, `QUI__DATA_DIR` (defaults to `/config`).
- OIDC is available but out of scope; qui's built-in admin login is the auth.

Compose block (following the repo's hardening pattern):

```yaml
qui:
  image: ghcr.io/autobrr/qui:latest
  container_name: qui
  user: "${PUID:-1000}:${PGID:-1000}"
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
  environment:
    - TZ=${TZ:-UTC}
    - QUI__HOST=0.0.0.0
    - QUI__PORT=7476
    - QUI__SESSION_SECRET=${QUI_SESSION_SECRET}
  volumes:
    - ${CONFIG_DIRECTORY}/qui:/config
  ports:
    - '127.0.0.1:7476:7476'
  restart: unless-stopped
  depends_on:
    qbittorrent:
      condition: service_started
  networks:
    - nas-network
  labels:
    - swag=enable
    - swag_port=7476
    - swag_proto=http
    - com.centurylinklabs.watchtower.enable=true
  healthcheck:
    test: [CMD, wget, --no-verbose, --tries=1, --spider, 'http://localhost:7476/']
    interval: 60s
    timeout: 10s
    retries: 3
    start_period: 45s
  logging:
    driver: json-file
    options:
      max-size: 10m
      max-file: '2'
```

Notes:
- No caps needed — a single Go binary that listens and makes HTTP calls.
- `cleanuparr`/`prowlarr`/etc. patterns show `swag=enable` alone works only for
  apps with a bundled SWAG preset conf matching the container name. qui has no
  preset, so `swag_port`/`swag_proto` labels tell the auto-proxy mod which
  upstream to generate. If the auto-proxy still can't produce a working conf,
  fall back to a hand-written `qui.subdomain.conf` (implementation will verify
  the generated conf after first `up`).
- Watchtower label on: pullable ghcr image, unlike the locally-built webapps.

### 2. Retire qBittorrent's own WebUI as a public surface

- **Keep** `127.0.0.1:8080:8080` on the gluetun block (host crons + loopback
  auth-bypass depend on it; it is not public).
- **Verify + remove** any hand-written public proxy-conf for qbittorrent in the
  live `${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/` (e.g.
  `qbittorrent.subdomain.conf`). This is the only genuinely-public route, and it
  is not in the repo, so it is a manual host-side check documented in the plan.
  After this, `qui.4eva.me` is the sole public front door to qBittorrent.

### 3. Dependents — no reconfiguration needed

sonarr / radarr / lidarr / prowlarr / cleanuparr reach `qbittorrent:8080` over
the API on `nas-network`. The daemon, its port, the gluetun netns, forwarded
P2P ports (37021), `stop_grace_period`, and the lockfile self-heal init script
are all untouched. qui is simply an additional API consumer of the same
endpoint. This is verified, not assumed.

### 4. First-run setup (manual, one-time)

qui cannot be seeded from env — the admin account and the qBittorrent instance
are created in the web UI on first launch:

1. Browse to `https://qui.4eva.me`, create the admin login.
2. **Add instance** → URL `http://qbittorrent:8080`, username/password =
   existing `QBITTORRENT_USER` / `QBITTORRENT_PASS`.
   (qui connects from `nas-network`, not loopback, so qbit's localhost
   auth-bypass does not apply — the real creds are required. They already exist
   in `.env`.)

### 5. Supporting changes

- `.env.example`: add `QUI_SESSION_SECRET=` with a note to generate a random
  value (e.g. `openssl rand -hex 32`).
- `CLAUDE.md`: add qui to the stack description and a short gotcha noting it is
  a UI over qBittorrent, that qbit's loopback publish must stay for the crons,
  and the public route is now `qui.4eva.me`.
- `docker compose config` must stay green (CI gate).

## Scope decisions (YAGNI)

- **Management-only.** The optional `${SHARE_DIRECTORY}/downloads:/downloads`
  mount that would enable qui's orphan-scan / hardlink / reflink features is
  **not** included. It is a one-line add later if wanted; the path must match
  qbit's (`/downloads`).
- No OIDC, no PostgreSQL backend (SQLite in `/config` is fine for a
  single-instance homelab), no multi-instance setup.

## Success criteria

- `docker compose config` passes.
- `docker compose up -d qui` starts qui healthy.
- `https://qui.4eva.me` serves qui's login over SWAG's TLS.
- After first-run setup, qui shows the live qBittorrent torrent list.
- The *arr clients and the three host crons continue to reach qBittorrent
  unchanged (spot-check: `media_ops_status.py` still reports qbit reachable).
- No public route to qBittorrent's built-in WebUI remains.
