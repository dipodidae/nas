# Migrating from DuckDNS to Cloudflare DNS for SWAG/SSL

This guide walks you through migrating your NAS Docker setup from DuckDNS to Cloudflare DNS for SSL certificate management with SWAG.

---

## 1. Prepare Cloudflare

- Log in to Cloudflare and select your domain (e.g., `4eva.me`).
- Go to DNS settings:
  - Remove any DuckDNS-related A/CNAME records.
  - Create an A record for your subdomain (e.g., `nas.4eva.me`) pointing to your public IP.
  - If your IP is dynamic, enable Cloudflare’s “Proxy (orange cloud)” so SWAG can handle SSL.
- Get your Cloudflare API token:
  - Go to Profile → API Tokens → Create Token
  - Use the “Edit zone DNS” template for your domain.
  - Copy the API token (you’ll need it in SWAG).

## 2. Update SWAG to use Cloudflare

- In `docker-compose.yml`, update the `swag:` service environment:
  ```yaml
  environment:
    - PUID=${PUID}
    - PGID=${PGID}
    - TZ=${TZ}
    - VALIDATION=dns
    - DNSPLUGIN=cloudflare
    - CF_API_TOKEN=${CLOUDFLARE_API_TOKEN}
    - URL=4eva.me
    - SUBDOMAINS=wildcard
    - ONLY_SUBDOMAINS=false
    - DOCKER_MODS=linuxserver/mods:swag-dashboard|linuxserver/mods:universal-docker|linuxserver/mods:swag-auto-proxy
    - DOCKER_HOST=dockerproxy
  ```
- Remove DuckDNS-related environment variables from SWAG.

## 3. Remove DuckDNS Container

- Run:
  ```zsh
  docker compose down duckdns
  ```
- Remove the `duckdns:` service from `docker-compose.yml`.

## 4. Update Environment Variables

- In your `.env` (or `.env.example`):
  - Remove DuckDNS variables:
    ```
    # DUCKDNS_EMAIL=...
    # DUCKDNS_TOKEN=...
    # DUCKDNS_URL=...
    # DUCKDNS_SUBDOMAIN=...
    ```
  - Add Cloudflare token:
    ```
    CLOUDFLARE_API_TOKEN=your-cloudflare-api-token
    ```
  - Update service URLs if needed:
    ```
    JELLYFIN_PUBLISHED_URL=https://jellyfin.4eva.me
    ```

## 5. Recreate SWAG Container

- Run:
  ```zsh
  docker compose down swag
  docker compose up -d swag
  ```
- SWAG will request SSL certificates using Cloudflare DNS.
- Check logs:
  ```zsh
  docker logs -f swag
  ```
  Look for: `Certificate successfully obtained!`

## 6. Update Internal Service URLs

- Replace any references to DuckDNS domains with your new Cloudflare domain/subdomains (e.g., `jellyfin.4eva.me`, `plex.4eva.me`).
- Ensure subdomains match your SWAG `SUBDOMAINS` config.

## 7. Test Your Setup

- Open your browser:
  - https://jellyfin.4eva.me
  - https://plex.4eva.me
- Access SWAG dashboard: `http://<your-ip>:81`
- Verify SSL certificates are valid.

## 8. Clean Up Old DuckDNS Config

- Delete the DuckDNS config folder if not needed:
  ```zsh
  rm -rf ${CONFIG_DIRECTORY}/duckdns
  ```
- Remove old DuckDNS environment variables from `.env`.

---

## ✅ After Migration

Your NAS services will now use Cloudflare DNS + SSL via SWAG. Enjoy improved reliability and easier management!

---

## Example SWAG Service (Cloudflare)

```yaml
swag:
  image: lscr.io/linuxserver/swag
  container_name: swag
  cap_add:
    - NET_ADMIN
  security_opt:
    - no-new-privileges:true
  environment:
    - PUID=${PUID}
    - PGID=${PGID}
    - TZ=${TZ}
    - VALIDATION=dns
    - DNSPLUGIN=cloudflare
    - CF_API_TOKEN=${CLOUDFLARE_API_TOKEN}
    - URL=4eva.me
    - SUBDOMAINS=wildcard
    - ONLY_SUBDOMAINS=false
    - DOCKER_MODS=linuxserver/mods:swag-dashboard|linuxserver/mods:universal-docker|linuxserver/mods:swag-auto-proxy
    - DOCKER_HOST=dockerproxy
  volumes:
    - ${CONFIG_DIRECTORY}/swag:/config
    - ./rootpage/dist:/config/www/rootpage:ro
    - ./rootpage/nginx-rootpage.conf:/config/nginx/site-confs/root.conf:ro
    - ./jellyfin.subdomain.conf:/config/nginx/proxy-confs/jellyfin.subdomain.conf:ro
  ports:
    - 443:443
    - 80:80
    - 81:81
  restart: unless-stopped
  networks:
    - nas-network
  labels:
    - com.centurylinklabs.watchtower.enable=true
```

## Example .env (Cloudflare)

```dotenv
# System Configuration
PUID=1000
PGID=1000
TZ=Europe/Amsterdam

# Storage Paths
SHARE_DIRECTORY=/mnt/drive
CONFIG_DIRECTORY=/mnt/drive/.docker-config

# Cloudflare Configuration
CLOUDFLARE_API_TOKEN=your-cloudflare-api-token-here

# Service URLs
JELLYFIN_PUBLISHED_URL=https://jellyfin.4eva.me
```
