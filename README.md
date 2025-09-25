<div align="center">

# NAS Stack

Self‑hosted media + automation + infrastructure stack for Raspberry Pi 5. One Docker Compose file. Batteries included: streaming, download automation, reverse proxy + TLS, health checks, smart maintenance scripts.

</div>

<p align="center">
<b>Fast to deploy · Reasonably secure defaults · Scriptable maintenance · Pi‑optimized</b>
</p>

## Feature Highlights

- Unified Docker Compose: Jellyfin, Sonarr, Radarr, Bazarr, Prowlarr, qBittorrent, Nextcloud, Lazylibrarian, SWAG
- Automated: indexers, downloads, subtitles, updates (selective Watchtower), health recovery (Autoheal)
- Reverse proxy + HTTPS: Cloudflare DNS + SWAG + Let's Encrypt with per‑service subdomains
- Hardened access: Docker socket proxy, non‑root users, isolated network, minimal exposed ports
- Pi 5 tuned: tmpfs transcoding, conservative memory + CPU limits, optional hardware acceleration
- Observability: healthchecks on every critical service, structured logs, scriptable audits
- Maintenance scripts: priority checker, config backups, permission audit, post‑update verifier, stalled torrent kickstart
- Extensible: drop in new services, label for proxy + auto‑update, add a healthcheck and go

---

## 🚀 Getting Started

```bash
# In the cloned directory
cp .env.example .env       # provide values (timezone, domain, API keys, credentials)
docker compose up -d       # launch stack
docker compose ps          # verify containers healthy
```

> Tip: run `docker compose logs -f swag` until certificates are issued.

## 🏗️ Architecture

| Layer           | Components                                                                                                      |
| --------------- | --------------------------------------------------------------------------------------------------------------- |
| Media Apps      | Jellyfin (stream), Sonarr (TV), Radarr (Movies), Bazarr (Subtitles), Prowlarr (Indexers), Lazylibrarian (Books) |
| Download        | qBittorrent (client)                                                                                            |
| Storage / Files | Nextcloud (sync & share)                                                                                        |
| Edge            | SWAG (NGINX reverse proxy + ACME), Cloudflare DDNS                                                              |
| Automation      | Watchtower (selective), Autoheal, Python scripts (`scripts/`)                                                   |
| Security        | Docker socket proxy, least‑privilege users, network isolation                                                   |

All services join a single custom network and use `${CONFIG_DIRECTORY}` for persistent config. Media libraries live under `${SHARE_DIRECTORY}`.

## ⚙️ Configuration

Minimal required `.env` keys (see full example):

```ini
TZ=Europe/London
PUID=1000
PGID=1000
CONFIG_DIRECTORY=/opt/appdata
SHARE_DIRECTORY=/mnt/storage
DOMAIN=yourdomain.com
ADMIN_EMAIL=admin@yourdomain.com
CLOUDFLARE_API_TOKEN=***
QBITTORRENT_USER=admin
QBITTORRENT_PASS=change_me
WATCHTOWER_SCHEDULE=0 4 * * *
```

Optional integrations:

```ini
CLEAN_SUBTITLES_DIRECTORY=/abs/path/to/clean-subtitles   # enables Bazarr mount
JELLYFIN_PUBLISHED_URL=https://jellyfin.${DOMAIN}
```

Directory layout (create + chown to PUID/PGID):

```ini
${CONFIG_DIRECTORY}/{jellyfin,sonarr,radarr,bazarr,prowlarr,lazylibrarian,qbittorrent,nextcloud,swag}
${SHARE_DIRECTORY}/{Movies,Series,Books,Music,Downloads,NextcloudData}
```

## 🧩 Automation Scripts

Located in `scripts/` (see `scripts/README.md` for full docs).

| Script                           | Purpose                                                 |
| -------------------------------- | ------------------------------------------------------- |
| prowlarr-priority-checker.py     | Analyze & recommend indexer priorities (fuzzy matching) |
| config_backup.py                 | Tar + prune config backups (fast/exclusion modes)       |
| permissions_auditor.py           | Report / optionally fix ownership & mode drift          |
| post_update_verifier.py          | Verify core service health after updates                |
| qbittorrent_stalled_kickstart.py | Nudge stalled torrents back to life                     |

## 🛡️ Security

- Reverse proxy terminates TLS; only required ports exposed externally
- Docker socket never mounted directly (proxy mediator only)
- Non‑root users (PUID/PGID) for app processes
- Healthchecks + Autoheal mitigate silent failures
- Explicit environment variables (no secrets baked in images)
- Add new services: join network, add `swag=enable` label only if public
