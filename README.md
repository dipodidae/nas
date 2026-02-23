# NAS Docker Compose Stack

One Docker Compose stack for running a small NAS / homelab media and utility suite (reverse proxy, media management, downloads, and supporting automation).

## Overview

This repository provides a single `docker-compose.yml` that:

- Runs common media-server services (indexing, media managers, request management, and streaming).
- Centralizes persistent application configuration under a configurable config root.
- Mounts a configurable “share” root for media and downloads.
- Uses SWAG (nginx + Let’s Encrypt) as the front door, with the linuxserver SWAG auto-proxy mod enabled.


Typical use case: a single Linux host (NAS or homelab) with a large storage mount for media and application configs.

## Services

All services are defined in `docker-compose.yml`.

| Service        | Purpose                                                         | Exposed ports (host)                                        | Volumes                                                                                                                                                        |
| -------------- | --------------------------------------------------------------- | ----------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `swag`         | Reverse proxy + Let's Encrypt (DNS validation) + auto-proxy mod | `80:80`, `443:443`                                          | `${CONFIG_DIRECTORY}/swag:/config`, `./rootpage/dist:/config/www/rootpage:ro`, `./nginx-cache:/var/cache/nginx:rw`                                             |
| `sonarr`       | TV series management                                            | `127.0.0.1:8989:8989`                                       | `${CONFIG_DIRECTORY}/sonarr:/config`, `${SHARE_DIRECTORY}/Series:/tv`, `${SHARE_DIRECTORY}/Downloads:/downloads`                                               |
| `radarr`       | Movie management                                                | `127.0.0.1:7878:7878`                                       | `${CONFIG_DIRECTORY}/radarr:/config`, `${SHARE_DIRECTORY}/Movies:/movies`, `${SHARE_DIRECTORY}/Downloads:/downloads`                                           |
| `lidarr`       | Music collection management                                     | `127.0.0.1:8686:8686`                                       | `${CONFIG_DIRECTORY}/lidarr:/config`, `${SHARE_DIRECTORY}/Music:/music`, `${SHARE_DIRECTORY}/Downloads:/downloads`                                             |
| `slskd`        | Soulseek P2P client                                             | `127.0.0.1:5030:5030`, `50300:50300/tcp`                    | `${CONFIG_DIRECTORY}/slskd:/app`, `${SHARE_DIRECTORY}/Music:/music`, `${SHARE_DIRECTORY}/Downloads:/downloads`                                                 |
| `bazarr`       | Subtitle management                                             | `127.0.0.1:6767:6767`                                       | `${CONFIG_DIRECTORY}/bazarr:/config`, `${SHARE_DIRECTORY}/Movies:/movies`, `${SHARE_DIRECTORY}/Series:/tv`, `${CLEAN_SUBTITLES_DIRECTORY}:/clean-subtitles:ro` |
| `flaresolverr` | Cloudflare challenge bypass proxy (used by Prowlarr)            | `127.0.0.1:8191:8191`                                       | None                                                                                                                                                           |
| `prowlarr`     | Indexer management                                              | `127.0.0.1:9696:9696`                                       | `${CONFIG_DIRECTORY}/prowlarr:/config`                                                                                                                         |
| `qbittorrent`  | Download client                                                 | `127.0.0.1:8080:8080`, `51413:51413/tcp`, `51413:51413/udp` | `${CONFIG_DIRECTORY}/qbittorrent:/config`, `${SHARE_DIRECTORY}/Downloads:/downloads`                                                                           |
| `jellyfin`     | Media streaming server                                          | `8096:8096`, `8920:8920`, `7359:7359/udp`, `1900:1900/udp`  | `${CONFIG_DIRECTORY}/jellyfin:/config`, `${SHARE_DIRECTORY}:/data/movies:ro`                                                                                   |
| `jellyseerr`   | Media requests for Jellyfin/Sonarr/Radarr                       | `127.0.0.1:5056:5056`                                       | `${CONFIG_DIRECTORY}/jellyseerr:/app/config`                                                                                                                   |
| `nextcloud`    | Files + sync (linuxserver Nextcloud)                            | `127.0.0.1:8087:443`                                        | `${CONFIG_DIRECTORY}/nextcloud:/config`, `${SHARE_DIRECTORY}/NextcloudData:/data`, `${SHARE_DIRECTORY}:/external/drive:rw`, `/mnt/sdcard:/external/sdcard:rw` |

Notes:

- Most app containers are labeled `swag=enable`, used by the SWAG auto-proxy mod to generate nginx proxy configs.
- Internal-only services are bound to `127.0.0.1`; only Jellyfin, qBittorrent (P2P port), slskd (P2P port), and SWAG require broader host exposure.

## Architecture

### Networking

The stack defines a single custom bridge network:

- `nas-network` — subnet `172.30.0.0/24`, gateway `172.30.0.1`.
  All services join this network for internal service-to-service communication.

### Reverse proxy

- `swag` publishes ports `80`/`443` (HTTP/HTTPS).
- DNS validation is configured (`VALIDATION=dns`) with the Cloudflare plugin (`DNSPLUGIN=cloudflare`).
- The linuxserver SWAG auto-proxy mod generates nginx proxy configs from container labels (`swag=enable`).

### Service dependencies (compose `depends_on`)

- `sonarr` → `prowlarr`, `qbittorrent`
- `radarr` → `prowlarr`, `qbittorrent`
- `lidarr` → `prowlarr`, `qbittorrent`, `slskd`
- `bazarr` → `prowlarr`
- `prowlarr` → `qbittorrent`
- `jellyseerr` → `jellyfin`, `sonarr`, `radarr`
- `nextcloud` → `swag`

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
- `${SHARE_DIRECTORY}` and the subfolders used by your services (e.g. `Movies/`, `Series/`, `Music/`, `Downloads/`, `NextcloudData/`)

All linuxserver.io containers run as `PUID:PGID`; the host directories must be writable by that user/group.

### 4) (Optional) Build the root landing page

The SWAG container bind-mounts `./rootpage/dist` into its web root. If `rootpage/dist` does not exist, build it:

```bash
cd rootpage
pnpm install
pnpm run build
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
- `CLEAN_SUBTITLES_DIRECTORY`: read-only mount used by Bazarr for pre-cleaned subtitle files.

### Script-only environment variables

The `.env.example` also includes API keys/tokens that are not referenced in `docker-compose.yml`, but are used by scripts under `scripts/`:

- `API_KEY_RADARR`, `API_KEY_SONARR`, `API_KEY_JELLYFIN`, `API_KEY_PROWLARR`
- `PLEX_TOKEN` (used by `enable_bazarr_plex.py` if Plex integration is needed)

See the scripts documentation in [scripts/README.md](scripts/README.md).

### Ports

Most app WebUI ports are bound to `127.0.0.1` and accessed via the SWAG reverse proxy. Services that require direct host or LAN exposure (Jellyfin clients, qBittorrent and slskd P2P) publish ports to all interfaces — see the Services table for specifics.

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
  - Application configuration/state for SWAG, Sonarr, Radarr, Lidarr, Bazarr, Prowlarr, qBittorrent, slskd, Jellyfin, Jellyseerr, Nextcloud.
- `${SHARE_DIRECTORY}`
  - Media and downloads used by multiple services.

Additional persistence to consider:

- Nextcloud data directory: `${SHARE_DIRECTORY}/NextcloudData`.
- SWAG nginx cache: `nginx-cache/` (optional to back up; safe to rebuild, but may be large).

A backup helper exists at [scripts/config_backup.py](scripts/config_backup.py) (see [scripts/README.md](scripts/README.md)).

## Common issues / notes

- SWAG certificate issuance will fail if `CLOUDFLARE_API_TOKEN`, `PUBLIC_DOMAIN`, and `ADMIN_EMAIL` are not set correctly.
- `nextcloud` mounts `/mnt/sdcard:/external/sdcard:rw`. If your host does not have `/mnt/sdcard`, remove or change that bind mount.
- qBittorrent credentials default to `qbittorrent` / `changeme_password` unless overridden; set `QBITTORRENT_USER` and `QBITTORRENT_PASS` before exposing the Web UI.
- `swag` bind-mounts `./rootpage/dist`. If you have not built the rootpage, the landing page content may be missing.
- slskd default credentials are `slskd` / `slskd`; change them before exposing the service publicly.
