
# Docker NAS Configuration

This repository contains Docker Compose files for a full-featured NAS (Network Attached Storage) solution, including media management, download automation, streaming, and secure reverse proxying with SSL, all in Docker containers.

---

<div align="center">

# NAS Stack
Media automation, streaming, private cloud and secure reverse proxy – containerized.

</div>

---

## 1. Overview
This repository contains a Docker Compose stack providing:
* Media acquisition & management (Radarr, Sonarr, Prowlarr, Bazarr, LazyLibrarian)
* Streaming (Jellyfin)
* Personal cloud (Nextcloud)
* Download client (qBittorrent)
* Reverse proxy + TLS (SWAG with auto-proxy)
* Health & self-healing (healthchecks + Autoheal)
* Automated updates (Watchtower)
* Dynamic DNS (Cloudflare DDNS)

Designed and tuned for a Raspberry Pi 5 (also works on other Linux hosts).

---

## 2. Stack Components
| Service | Purpose | Key Ports (Host) | Notes |
|---------|--------|------------------|-------|
| dockerproxy | Restricted Docker API for SWAG auto-proxy | – | Read-only socket
| SWAG | Reverse proxy, TLS, auto proxy labels | 443, 80, 81 | Auto-discovers `swag=enable`
| Jellyfin | Media streaming (movies, shows, music) | 8096, 8920 | Hardware accel ready (enable when /dev/dri present)
| Sonarr | TV automation | 8989 | Depends on Prowlarr
| Radarr | Movie automation | 7878 | Depends on Prowlarr
| Prowlarr | Indexer manager | 9696 | Feeds *arr apps
| Bazarr | Subtitle automation | 6767 | Integrates with Radarr/Sonarr
| LazyLibrarian | Books / audiobooks | 5299 | Optional
| qBittorrent | Torrent client | 8080, 6881/tcp+udp | User/pass via env
| Nextcloud | Personal cloud | 8087 (HTTPS inside container) | Exposed via SWAG externally
| Cloudflare DDNS | Updates DNS A record | – | Uses API token
| Watchtower | Auto image updates | – | Controlled by schedule
| Autoheal | Restart unhealthy containers | – | Uses healthchecks

---

## 3. Prerequisites
* Docker + Docker Compose v2
* Domain managed in Cloudflare (API token with DNS edit for the zone)
* Storage mounted for media & config
* Non-root user (PUID/PGID) owning the data directories

---

## 4. Directory Layout (example)
```
CONFIG_DIRECTORY (/opt/appdata/)
   jellyfin/
   sonarr/
   radarr/
   bazarr/
   prowlarr/
   lazylibrarian/
   qbittorrent/
   nextcloud/
   swag/

SHARE_DIRECTORY (/mnt/media/)
   Movies/
   Series/
   Books/
   Music/
   Downloads/
   NextcloudData/
```

---

## 5. Environment Variables (.env template)
```
PUID=1000
PGID=1000
TZ=Europe/Amsterdam

CONFIG_DIRECTORY=/opt/appdata
SHARE_DIRECTORY=/mnt/media

DOMAIN=example.com
ADMIN_EMAIL=admin@example.com
CLOUDFLARE_API_TOKEN=xxxx

JELLYFIN_PUBLISHED_URL=https://jellyfin.example.com

QBITTORRENT_USER=admin
QBITTORRENT_PASS=changeme

WATCHTOWER_SCHEDULE=0 4 * * *
```

Add or adjust additional application-specific values as needed.

---

## 6. Usage
Clone + start:
```
git clone <repo-url>
cd nas
cp .env.example .env   # if you maintain an example
edit .env               # set required values
docker compose up -d
```

List containers:
```
docker compose ps
```

Tail logs for one service:
```
docker compose logs -f jellyfin
```

Update (manual):
```
docker compose pull && docker compose up -d
```

---

## 7. Reverse Proxy & External Access
* SWAG issues certificates and proxies any container labeled `swag=enable`.
* Add new subdomains in DNS (wildcard supported) and label the service.
* Jellyfin external URL should match `JELLYFIN_PublishedServerUrl`.

---

## 8. Health & Self-Healing
Each service has a `HEALTHCHECK`. Autoheal monitors them and restarts failing containers.
Check health:
```
docker inspect --format='{{.Name}} => {{.State.Health.Status}}' $(docker ps -q)
```

---

## 9. Performance (Raspberry Pi 5)
* Jellyfin transcode temp on tmpfs (`/tmp` size 4G) to reduce I/O latency.
* Remove CPU limits for Jellyfin to avoid throttling during peak transcode bursts.
* Keep adequate cooling; verify no throttling: `vcgencmd get_throttled` returns `0x0`.
* Put CONFIG_DIRECTORY and active Downloads on SSD if possible.
* Use `noatime` mount option for media volumes to reduce metadata writes.

### Hardware Acceleration (when /dev/dri exists)
Uncomment the `devices` and `group_add` block under Jellyfin, ensure user in `video` & `render` groups, then restart.

---

## 10. Backups
Config backup:
```
tar -czf nas-config-$(date +%Y%m%d).tar.gz -C "$CONFIG_DIRECTORY" .
```
Compose + env:
```
tar -czf nas-stack-$(date +%Y%m%d).tar.gz docker-compose.yml .env
```

Nextcloud data must be backed up at rest (filesystem snapshot + database if using an external DB; current setup uses internal sqlite).

---

## 11. Logs & Monitoring
Show disk usage:
```
df -h
docker system df
```
Prune unused images/volumes (careful):
```
docker system prune -f
```

---

## 12. Security Notes
* Reverse proxy terminates TLS; do not expose raw service ports externally.
* Limit SSH access; use firewall to restrict inbound ports to 80/443 only.
* Keep API tokens & passwords only in `.env` (not committed).
* Watchtower updates images – review logs after updates.

---

## 13. Troubleshooting
| Issue | Quick Check | Command |
|-------|-------------|---------|
| Service unhealthy | Inspect health | `docker inspect <name> | grep -i status` |
| High I/O latency | Check iowait | `pidstat -d 1 5` |
| Slow Jellyfin start | See logs | `docker compose logs jellyfin --tail=200` |
| DNS not updating | Cloudflare DDNS logs | `docker compose logs cloudflare-ddns` |
| Autoheal restarts often | Inspect failing healthcheck | `docker compose logs <svc>` |

Restart a single service:
```
docker compose restart sonarr
```

---

## 14. Extending
1. Add service block to `docker-compose.yml`.
2. Label with `swag=enable` if it needs reverse proxy exposure.
3. Add a healthcheck.
4. Restart stack.

---

## 15. License
See `LICENSE`.

---

## 16. Minimal .env Example
```
PUID=1000
PGID=1000
TZ=UTC
CONFIG_DIRECTORY=/opt/appdata
SHARE_DIRECTORY=/mnt/media
DOMAIN=example.com
ADMIN_EMAIL=admin@example.com
CLOUDFLARE_API_TOKEN=xxxx
JELLYFIN_PUBLISHED_URL=https://jellyfin.example.com
QBITTORRENT_USER=admin
QBITTORRENT_PASS=changeme
WATCHTOWER_SCHEDULE=0 4 * * *
```

---

## 17. Quick Reference Commands
```
docker compose up -d
docker compose down
docker compose pull
docker compose logs -f swag
docker stats
docker compose exec jellyfin bash
```

---

All set. Adjust, extend, automate.

