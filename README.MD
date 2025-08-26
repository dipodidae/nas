
# Docker NAS Configuration

This repository contains Docker Compose files for a full-featured NAS (Network Attached Storage) solution, including media management, download automation, streaming, and secure reverse proxying with SSL, all in Docker containers.

---


## 🛠️ Services Overview

The following services are included in the configuration:

| Service         | Description                                                                 | Default Port |
|------------------|-----------------------------------------------------------------------------|--------------|
| **Radarr**      | Manages movies and automates downloading and organizing them.              | `7878`       |
| **Sonarr**      | Manages TV shows and automates downloading and organizing them.            | `8989`       |
| **Bazarr**      | Automates subtitle management for your movies and TV shows.                | `6767`       |
| **Jellyfin**    | Media streaming server for movies, TV shows, and music.                   | `8096`, `8920` |
| **Plex**        | Alternative media streaming server with advanced features.                 | `32400`      |
| **SWAG**        | Secure Web Application Gateway for reverse proxy, SSL, dashboard, and auto-proxy. | `443`, `80`, `81` |

| **dockerproxy** | Secure Docker socket proxy for SWAG auto-proxy mod.                        | N/A          |
| **Watchtower**  | Automatically updates Docker images of your containers.                    | N/A          |
| **Prowlarr**    | Indexer manager for Radarr, Sonarr, and other applications.                | `9696`       |
| **LazyLibrarian** | Automates book downloading and management.                               | `5299`       |
| **qBittorrent** | Torrent client with WebUI for downloading and managing content.            | `8080`       |
| **Overseerr**   | Request management and media discovery tool.                               | `5055`       |
| **Calibre**     | E-book management and conversion tool.                                      | `8084`, `8181` |
| **Calibre-Web** | Web interface for Calibre e-book library.                                 | `8085`       |
| **Nextcloud**   | Personal cloud storage and file sharing platform.                          | `8087`       |

---

## 🚀 Getting Started

### Prerequisites
- Docker and Docker Compose installed on your system.
- A `.env` file for environment variables (details below).


### Installation
1. Clone this repository:
   ```bash
   git clone https://github.com/yourusername/docker-nas-config.git
   cd docker-nas-config
   ```

2. Create a `.env` file based on the following template:
   ```env
   # User/Permissions
   PUID=1001
   PGID=1001
   TZ=Europe/Amsterdam

   # Paths
   SHARE_DIRECTORY=/mnt/drive
   CONFIG_DIRECTORY=/mnt/drive/.docker-config

   # DuckDNS
   DUCKDNS_EMAIL=dpdd@squat.net
   DUCKDNS_TOKEN=your-duckdns-token
   DUCKDNS_URL=your-duckdns-url
   DUCKDNS_SUBDOMAIN=your-duckdns-subdomain

   # Service URLs
   JELLYFIN_PUBLISHED_URL=https://jellyfin.yourdomain.duckdns.org

   # Watchtower
   WATCHTOWER_SCHEDULE=0 0 4 * * *

   # qBittorrent
   QBITTORRENT_USER=admin
   QBITTORRENT_PASS=yourpassword

   # Calibre
   CALIBRE_USER=youruser
   CALIBRE_PASS=yourpassword

   # API Keys (optional)
   API_KEY_RADARR=yourkey
   API_KEY_SONARR=yourkey
   API_KEY_LAZYLIBRARIAN=yourkey
   API_KEY_JELLYFIN=yourkey
   PLEX_TOKEN=yourtoken
   PLEX_IDENTIFIER=yourid
   PLEX_SERVER_NAME=yourservername
   ```

3. Adjust file paths and secrets in `.env` to match your system.

4. Start the services:
   ```bash
   docker compose up -d
   ```

5. Access services via their respective ports (see the [Services Overview](#services-overview)).

---


## 🛡️ Security & Proxying
- **SWAG** handles reverse proxy, SSL certificate management, and now includes:
   - **Dashboard mod**: Internal dashboard at `http://<server-ip>:81` (or `dashboard.domain.com` if configured).
   - **Auto-proxy mod**: Automatically detects and reverse proxies containers with the `swag=enable` label.
- **dockerproxy**: Provides secure, read-only Docker API access for SWAG auto-proxy.
- All containers are configured with `PUID` and `PGID` for correct file permissions.

---


## 🧹 Maintenance
- **Watchtower** automates container updates.
- Use `docker compose logs -f [service_name]` to monitor service logs.

---


## 📂 Folder Structure

```plaintext
docker-nas-config/
├── docker-compose.yml
├── .env                  # Environment variables
└── config/               # Configuration files for each service
   ├── radarr/
   ├── sonarr/
   ├── bazarr/
   ├── jellyfin/
   ├── swag/
   ├── prowlarr/
   ├── lazylibrarian/
   ├── qbittorrent/
   ├── calibre/
   ├── calibre-web/
   ├── nextcloud/
   └── ...
```

---


## 🤝 Contributing
Feel free to open issues or submit pull requests to improve this configuration.

---


## 📜 License
This project is licensed under the [MIT License](LICENSE).

---


## 🌐 Resources
- [Docker Documentation](https://docs.docker.com/)
- [LinuxServer.io](https://www.linuxserver.io/)
- [Jellyfin Documentation](https://jellyfin.org/docs/)
- [SWAG Mods Documentation](https://github.com/linuxserver/docker-mods)

---

### 📝 Notes
- To auto-proxy a service, add the label `swag=enable` to its service definition in `docker-compose.yml`.
- The only custom proxy conf in this setup is for Jellyfin; all other services use auto-proxy.
- The dashboard and auto-proxy mods are enabled via the `DOCKER_MODS` environment variable in SWAG.
- The `dockerproxy` service is required for secure Docker API access by SWAG.
