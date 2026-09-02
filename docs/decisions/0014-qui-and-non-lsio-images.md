# ADR-0014 — qui replaces qBittorrent's public WebUI; non-LSIO images need a pre-chowned config dir

**Date:** 2026-09-01
**Status:** accepted

## qui is a UI _over_ qBittorrent, not a replacement client

`qui` (autobrr/qui) has **no BitTorrent engine**. It manages the existing
qBittorrent daemon over the WebUI API at `qbittorrent:8080`, exactly like the
\*arr clients do, and is published at `qui.${PUBLIC_DOMAIN}`.

qBittorrent's own WebUI is retired as a **public** surface: the SWAG preset
`qbittorrent.subdomain.conf` was disabled (`.disabled`), and the route is now
the manually-enabled `qui.subdomain.conf` (container `qui`, port 7476).

## INVARIANT: qBittorrent's `127.0.0.1:8080:8080` publish must stay

Retiring the _public_ WebUI is not the same as removing the loopback publish.
`scripts/` authenticate against qBittorrent at `localhost:8080` using qbit's
localhost auth-bypass:

- `qbittorrent_settings_enforce.py`
- `qbittorrent_stalled_kickstart.py`
- `slskd_incomplete_sweep.py`
- `media_ops_status.py`

Remove the loopback publish and all of those break.

## qui needs real credentials; qbittorrent must not carry them

qui connects from `nas-network`, **not** loopback, so the auth-bypass does not
apply to it and it needs the real `QBITTORRENT_USER` / `QBITTORRENT_PASS`.
These are entered **once, by hand, in qui's UI** when adding the instance —
not as container environment. See ADR-0011 for why they are not on the
qbittorrent container either.

qui also cannot be seeded from env at all: its admin account and the
qBittorrent instance are both created in the web UI on first run.

## Deploy gotcha: `qui` and `ntfy` need a pre-chowned config directory

Neither is a linuxserver image, so neither has a root init to chown its config
directory. Both run directly as `${PUID}:${PGID}` and write their own state
(`config.toml`/`qui.db`; `user.db`/`cache.db`/`webpush.db`).

If Docker auto-creates the host directory as **root** on first `up`, the
container **crash-loops on `permission denied`.** The host directory must be
owned by `${PUID}:${PGID}` _before_ starting:

```sh
mkdir -p ${CONFIG_DIRECTORY}/qui  && chown ${PUID}:${PGID} ${CONFIG_DIRECTORY}/qui
mkdir -p ${CONFIG_DIRECTORY}/ntfy && chown ${PUID}:${PGID} ${CONFIG_DIRECTORY}/ntfy
```

`make bootstrap` does exactly this, idempotently. Apply the same treatment to
any future non-LSIO image that runs as a non-root `user:`.
