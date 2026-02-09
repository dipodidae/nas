# NAS Docker Compose Stack

One Docker Compose stack for running a small NAS / homelab media and utility suite (reverse proxy, media management, downloads, and supporting automation).

## Overview

This repository provides a single `docker-compose.yml` that:

- Runs common media-server services (indexing, media managers, request management, and streaming).
- Centralizes persistent application configuration under a configurable config root.
- Mounts a configurable “share” root for media and downloads.
- Uses SWAG (nginx + Let’s Encrypt) as the front door, with the linuxserver SWAG auto-proxy mod enabled.
- Uses a Docker socket proxy (`dockerproxy`) so services that need Docker API access do not mount the raw Docker socket directly.

Typical use case: a single Linux host (NAS or homelab) with a large storage mount for media and application configs.

## Services

All services are defined in `docker-compose.yml`.

| Service         | Purpose                                                                    | Exposed ports                                              | Volumes                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------- | -------------------------------------------------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `autoheal`      | Restart unhealthy containers via Docker API using `dockerproxy`            | None                                                       | None                                                                                                                                                                                                                                                                                                                                                                                                                 |
| `bazarr`        | Subtitle management                                                        | `6767:6767`                                                | `${CONFIG_DIRECTORY}/bazarr:/config`, `${SHARE_DIRECTORY}/Movies:/movies`, `${SHARE_DIRECTORY}/Series:/tv`, `${CLEAN_SUBTITLES_DIRECTORY}:/clean-subtitles:ro`                                                                                                                                                                                                                                                       |
| `dockerproxy`   | Restricted Docker API proxy for other containers                           | None (internal Docker API on `:2375`)                      | `/var/run/docker.sock:/var/run/docker.sock:ro`                                                                                                                                                                                                                                                                                                                                                                       |
| `jellyfin`      | Media streaming server                                                     | `8096:8096`, `8920:8920`, `7359:7359/udp`, `1900:1900/udp` | `${CONFIG_DIRECTORY}/jellyfin:/config`, `${SHARE_DIRECTORY}:/data/movies`, `${JELLYFIN_CACHE_DIRECTORY}:/cache:rw`                                                                                                                                                                                                                                                                                                   |
| `jellyseerr`    | Media requests for Jellyfin/Sonarr/Radarr                                  | `5056:5056`                                                | `${CONFIG_DIRECTORY}/jellyseerr:/app/config`                                                                                                                                                                                                                                                                                                                                                                         |
| `lazylibrarian` | Book management                                                            | `5299:5299`                                                | `${CONFIG_DIRECTORY}/lazylibrarian:/config`, `${SHARE_DIRECTORY}/Downloads:/downloads`, `${SHARE_DIRECTORY}/Books:/books`                                                                                                                                                                                                                                                                                            |
| `nextcloud`     | Files + sync (linuxserver Nextcloud)                                       | `8087:443`                                                 | `${CONFIG_DIRECTORY}/nextcloud:/config`, `${SHARE_DIRECTORY}/NextcloudData:/data`, `${SHARE_DIRECTORY}:/external/drive:rw`, `${SHARE_DIRECTORY}/Movies:/external/movies:rw`, `${SHARE_DIRECTORY}/Series:/external/series:rw`, `${SHARE_DIRECTORY}/Music:/external/music:rw`, `${SHARE_DIRECTORY}/Books:/external/books:rw`, `${SHARE_DIRECTORY}/Downloads:/external/downloads:rw`, `/mnt/sdcard:/external/sdcard:rw` |
| `prowlarr`      | Indexer management                                                         | `9696:9696`                                                | `${CONFIG_DIRECTORY}/prowlarr:/config`                                                                                                                                                                                                                                                                                                                                                                               |
| `qbittorrent`   | Download client (Web UI + bittorrent ports)                                | `8080:8080`, `6881:6881/tcp`, `6881:6881/udp`              | `${CONFIG_DIRECTORY}/qbittorrent:/config`, `${SHARE_DIRECTORY}/Downloads:/downloads`                                                                                                                                                                                                                                                                                                                                 |
| `radarr`        | Movie management                                                           | `7878:7878`                                                | `${CONFIG_DIRECTORY}/radarr:/config`, `${SHARE_DIRECTORY}/Movies:/movies`, `${SHARE_DIRECTORY}/Downloads:/downloads`, `${CONFIG_DIRECTORY}/radarr/custom-services.d:/custom-services.d`, `${CONFIG_DIRECTORY}/radarr/custom-cont-init.d:/custom-cont-init.d`                                                                                                                                                         |
| `sonarr`        | TV series management                                                       | `8989:8989`                                                | `${CONFIG_DIRECTORY}/sonarr:/config`, `${SHARE_DIRECTORY}/Series:/tv`, `${SHARE_DIRECTORY}/Downloads:/downloads`, `${CONFIG_DIRECTORY}/sonarr/custom-services.d:/custom-services.d`, `${CONFIG_DIRECTORY}/sonarr/custom-cont-init.d:/custom-cont-init.d`                                                                                                                                                             |
| `swag`          | Reverse proxy + Let’s Encrypt (DNS validation) + dashboard/auto-proxy mods | `80:80`, `81:81`, `443:443`                                | `${CONFIG_DIRECTORY}/swag:/config`, `./rootpage/dist:/config/www/rootpage:ro`, `./rootpage/nginx-rootpage.conf:/config/nginx/site-confs/root.conf:ro`, `./nginx-cache:/var/cache/nginx:rw`                                                                                                                                                                                                                           |
| `watchtower`    | Auto-update containers (label-controlled) using `dockerproxy`              | None                                                       | None                                                                                                                                                                                                                                                                                                                                                                                                                 |

Notes:

- Most app containers are labeled `swag=enable`, which is used by the SWAG auto-proxy mod to generate nginx proxy configs.
- Most containers are labeled `com.centurylinklabs.watchtower.enable=true`; Watchtower is configured with `WATCHTOWER_LABEL_ENABLE=true`.

## Architecture

### Networking

The stack defines two bridge networks:

- `nas-media-network` (`media-network`)
  - Subnet `172.30.0.0/16`, gateway `172.30.0.1`.
  - Primary network for internal service-to-service communication.
- `nas-proxy-network` (`proxy-network`)
  - Used by `swag` (and also joined by `jellyfin`).

### Reverse proxy

- `swag` publishes ports `80`/`443` (HTTP/HTTPS) and `81`.
- DNS validation is configured (`VALIDATION=dns`) with the Cloudflare plugin (`DNSPLUGIN=cloudflare`).
- The linuxserver SWAG mods include `swag-auto-proxy`, which relies on container labels (e.g. `swag=enable`, `swag_port=5056`).

### Docker API access

- `dockerproxy` exposes a restricted Docker API to the internal network.
- `watchtower` and `autoheal` talk to Docker via `tcp://dockerproxy:2375`.
- `swag` is also configured with `DOCKER_HOST=dockerproxy` as part of the enabled SWAG Docker-related mods.

### Service dependencies (compose `depends_on`)

- `swag` → `dockerproxy`
- `sonarr` → `prowlarr`, `qbittorrent`
- `radarr` → `prowlarr`, `qbittorrent`
- `bazarr` → `prowlarr`
- `prowlarr` → `qbittorrent`
- `jellyfin` → `swag`
- `jellyseerr` → `jellyfin`, `sonarr`, `radarr`
- `nextcloud` → `swag`
- `watchtower` → `dockerproxy`
- `autoheal` → `dockerproxy`
- `lazylibrarian` → `prowlarr`, `qbittorrent`

## Requirements

- Linux host (the compose file uses Linux-style paths and mounts `/var/run/docker.sock`).
- Docker Engine (a modern version that supports Compose v2 and healthchecks).
- Docker Compose v2 plugin (`docker compose ...`).

If you are not on Linux, you will need to adapt bind mounts accordingly.

## Setup

### 1) Clone

```bash
git clone https://github.com/dipodidae/nas.git
cd nas
```

### 2) Create a `.env`

This repo includes an example file at `.env.example`.

```bash
cp .env.example .env
```

Edit `.env` and set at least:

- `SHARE_DIRECTORY` (your media/share root, e.g. `/mnt/drive`)
- `CONFIG_DIRECTORY` (where container configs should persist, e.g. `/mnt/drive/.docker-config`)
- `PUBLIC_DOMAIN`, `ADMIN_EMAIL`, `CLOUDFLARE_API_TOKEN` (required for SWAG DNS validation)
- `PUID`, `PGID`, `TZ`

### 3) Ensure host directories exist and permissions make sense

At minimum, the following should exist on the host:

- `${CONFIG_DIRECTORY}` (will contain per-service subfolders)
- `${SHARE_DIRECTORY}` and the subfolders used by your services (e.g. `Movies/`, `Series/`, `Downloads/`, `Books/`, `NextcloudData/`)

All linuxserver.io containers run as `PUID:PGID`; the host directories must be writable by that user/group.

### 4) (Optional) Build the root landing page

The SWAG container bind-mounts `./rootpage/dist` into its web root. If `rootpage/dist` does not exist, build it:

```bash
cd rootpage
npm install
npm run build
cd ..
```

### 5) Start the stack

```bash
docker compose up -d
```

## Configuration

### Core environment variables

The compose file uses the following variables (see `.env.example`):

- `PUID`, `PGID`: UID/GID that linuxserver containers run as.
- `TZ`: timezone.
- `SHARE_DIRECTORY`: host path containing media/downloads.
- `CONFIG_DIRECTORY`: host path where service configs are persisted (`${CONFIG_DIRECTORY}/<service>`).
- `PUBLIC_DOMAIN`, `ADMIN_EMAIL`: used by `swag`.
- `CLOUDFLARE_API_TOKEN`: used by `swag` for DNS validation.
- `JELLYFIN_PUBLISHED_URL`: passed to Jellyfin as `JELLYFIN_PublishedServerUrl`.
- `QBITTORRENT_USER`, `QBITTORRENT_PASS`: passed into the qBittorrent container.
- `JELLYFIN_CACHE_DIRECTORY`: host path mounted to Jellyfin’s `/cache`.
- `WATCHTOWER_SCHEDULE`: cron-style schedule string for Watchtower.
- `CLEAN_SUBTITLES_DIRECTORY`: optional read-only mount used by Bazarr.

### Script-only environment variables

The `.env.example` also includes API keys/tokens that are not referenced in `docker-compose.yml`, but are used by scripts under `scripts/`:

- `API_KEY_RADARR`, `API_KEY_SONARR`, `API_KEY_LAZYLIBRARIAN`, `API_KEY_JELLYFIN`, `API_KEY_PROWLARR`
- `PLEX_TOKEN`, `PLEX_IDENTIFIER`, `PLEX_SERVER_NAME`

See the scripts documentation in [scripts/README.md](scripts/README.md).

### Ports

All app ports are currently published to the host (see the Services table). If you want to rely only on reverse proxy access, you would remove or adjust `ports:` mappings in `docker-compose.yml`.

## Usage

### Start

```bash
docker compose up -d
```

### Stop

```bash
docker compose down
```

### Restart

```bash
docker compose restart
```

### Update containers

This stack includes `watchtower`, configured to update only containers labeled with `com.centurylinklabs.watchtower.enable=true`.

To manually pull newer images and recreate containers:

```bash
docker compose pull
docker compose up -d
```

### Check logs

```bash
# All services
docker compose logs -f

# One service
docker compose logs -f jellyfin
```

## Folder Structure

- `docker-compose.yml`: main stack definition.
- `.env.example`: environment variable template.
- `rootpage/`: Vite-based landing page that can be served by SWAG.
- `scripts/`: Python and shell utilities for operating/maintaining the stack (backups, audits, Prowlarr tooling, qBittorrent tuning, ebook deduplication).
- `nginx-cache/`: nginx cache directory bind-mounted into SWAG.
- `logs/`: local log output used by some scripts.
- `swag/`: repository directory reserved for SWAG-related assets (SWAG runtime config is stored under `${CONFIG_DIRECTORY}/swag`).

## Backup / Persistence

Persistent data is primarily stored in two places:

- `${CONFIG_DIRECTORY}/<service>`
  - Application configuration/state for SWAG, Sonarr, Radarr, Bazarr, Prowlarr, qBittorrent, Jellyfin, Jellyseerr, Nextcloud, LazyLibrarian.
- `${SHARE_DIRECTORY}`
  - Media and downloads used by multiple services.

Additional persistence to consider:

- Nextcloud data directory: `${SHARE_DIRECTORY}/NextcloudData`.
- SWAG nginx cache: `nginx-cache/` (optional to back up; safe to rebuild, but may be large).

A backup helper exists at [scripts/config_backup.py](scripts/config_backup.py) (see [scripts/README.md](scripts/README.md)).

## Common issues / notes

- SWAG certificate issuance will fail if `CLOUDFLARE_API_TOKEN`, `PUBLIC_DOMAIN`, and `ADMIN_EMAIL` are not set correctly. The compose file provides a dummy default token, which is not suitable for real use.
- `nextcloud` mounts `/mnt/sdcard:/external/sdcard:rw`. If your host does not have `/mnt/sdcard`, remove or change that bind mount.
- qBittorrent credentials default to `qbittorrent` / `changeme_password` unless overridden; set `QBITTORRENT_USER` and `QBITTORRENT_PASS` before exposing the Web UI.
- `swag` bind-mounts `./rootpage/dist`. If you have not built the rootpage, the landing page content may be missing.
- Many containers publish ports directly to the host; if you’re relying on reverse proxy only, you may want to remove host port bindings.
