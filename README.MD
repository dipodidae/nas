# Docker NAS Configuration

This repository contains the Docker Compose configuration files to set up a fully-featured NAS (Network Attached Storage) solution. The setup includes tools for media management, download automation, and content streaming, all running in Docker containers.

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
| **SWAG**        | Secure Web Application Gateway for reverse proxy and SSL management.       | `443`, `80`  |
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
   # Environment Variables
   PUID=1000
   PGID=1000
   TZ=Your/Timezone
   CONFIG_DIRECTORY=/path/to/config
   SHARE_DIRECTORY=/path/to/share
   DUCKDNS_URL=your-duckdns-url
   DUCKDNS_TOKEN=your-duckdns-token
   DUCKDNS_EMAIL=your-email@example.com
   ```

3. Adjust file paths in `.env` to match your system.

4. Start the services:
   ```bash
   docker-compose up -d
   ```

5. Access services via their respective ports (see the [Services Overview](#services-overview)).

---

## 🛡️ Security
- **SWAG** handles reverse proxy and SSL certificate management.
- All containers are configured with `PUID` and `PGID` to ensure proper file permissions.

---

## 🧹 Maintenance
- **Watchtower** automates container updates.
- Use `docker-compose logs -f [service_name]` to monitor service logs.

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
    └── watchtower/
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
