# NAS Media Server Cluster

A comprehensive Docker-based media server and automation stack running on Raspberry Pi 5, featuring streaming, download automation, reverse proxy, and monitoring capabilities.

## 🏗️ Architecture

### Core Services

- **Jellyfin** - Media streaming server with hardware acceleration support
- **SWAG** - Reverse proxy with SSL/TLS termination and automatic certificate management
- **Nextcloud** - File sync and sharing platform with external storage integration

### Media Automation (\*arr Stack)

- **Sonarr** - TV series management and automation
- **Radarr** - Movie management and automation
- **Bazarr** - Subtitle management for movies and TV shows
- **Prowlarr** - Indexer management for \*arr applications
- **Lazylibrarian** - Book and audiobook management

### Download & Storage

- **qBittorrent** - BitTorrent client with web interface
- **Nextcloud** - Additional file storage and sharing

### Infrastructure & Monitoring

- **Watchtower** - Automatic container updates
- **Autoheal** - Container health monitoring and restart automation
- **Cloudflare DDNS** - Dynamic DNS updates for external access
- **Docker Socket Proxy** - Secure Docker API access

## 🚀 Quick Start

### Prerequisites

- Raspberry Pi 5 with sufficient cooling
- Docker and Docker Compose installed
- Domain name configured with Cloudflare
- Environment variables configured (see Configuration section)

### Deployment

```bash
# Clone the repository
git clone <repository-url>
cd nas

# Configure environment variables
cp .env.example .env
# Edit .env with your specific values

# Start the stack
docker compose up -d

# Check service status
docker compose ps
```

## ⚙️ Configuration

### Required Environment Variables

Create a `.env` file with the following variables:

```bash
# Timezone and User Configuration
TZ=Europe/London
PUID=1000
PGID=1000

# Directory Paths
CONFIG_DIRECTORY=/opt/appdata
SHARE_DIRECTORY=/mnt/storage

# Domain and SSL
DOMAIN=yourdomain.com
ADMIN_EMAIL=admin@yourdomain.com
CLOUDFLARE_API_TOKEN=your_cloudflare_token

# Application Credentials
QBITTORRENT_USER=admin
QBITTORRENT_PASS=your_secure_password
JELLYFIN_PUBLISHED_URL=https://jellyfin.yourdomain.com

# Watchtower Schedule (cron format)
WATCHTOWER_SCHEDULE=0 4 * * *

# Optional: External utilities / scripts
# Directory that contains the clean-subtitles project (mounted read-only into Bazarr)
# Example (absolute path recommended):
# CLEAN_SUBTITLES_DIRECTORY=/home/tom/projects.clean-subtitles
# If unset, the Bazarr container will not have the extra /clean-subtitles mount.
```

### Directory Structure

Ensure the following directories exist and have proper permissions:

```
/opt/appdata/          # Application configurations
├── jellyfin/
├── sonarr/
├── radarr/
├── bazarr/
├── prowlarr/
├── lazylibrarian/
├── qbittorrent/
├── nextcloud/
└── swag/

/mnt/storage/          # Media and data storage
├── Movies/
├── Series/
├── Books/
├── Music/
├── Downloads/
└── NextcloudData/
```

## 🌐 Service Access

| Service        | Internal Port | External Access                      | Purpose             |
| -------------- | ------------- | ------------------------------------ | ------------------- |
| Jellyfin       | 8096          | https://jellyfin.yourdomain.com      | Media streaming     |
| Sonarr         | 8989          | https://sonarr.yourdomain.com        | TV management       |
| Radarr         | 7878          | https://radarr.yourdomain.com        | Movie management    |
| Bazarr         | 6767          | https://bazarr.yourdomain.com        | Subtitle management |
| Prowlarr       | 9696          | https://prowlarr.yourdomain.com      | Indexer management  |
| qBittorrent    | 8080          | https://qbittorrent.yourdomain.com   | Download client     |
| Lazylibrarian  | 5299          | https://lazylibrarian.yourdomain.com | Book management     |
| Nextcloud      | 8087          | https://nextcloud.yourdomain.com     | File sharing        |
| SWAG Dashboard | 81            | https://yourdomain.com:81            | Proxy management    |

## 🔧 Hardware Optimization

### Raspberry Pi 5 Specific Features

- **4GB tmpfs** for Jellyfin transcoding to reduce SD card wear
- **Hardware acceleration ready** - `/dev/dri` mounting prepared for when available
- **Resource limits** configured to prevent system overload
- **Health checks** on all services for automatic recovery

### Performance Tuning

- CPU reservations ensure fair resource allocation
- Memory limits prevent individual services from consuming all RAM
- Log rotation configured to prevent disk space issues
- Network isolation with custom bridge network

## 📊 Monitoring & Health

### Health Checks

All services include health checks with automatic restart via Autoheal:

- **Jellyfin**: System info endpoint monitoring
- **Media services**: Web interface availability
- **SWAG**: SSL certificate and proxy functionality
- **Infrastructure services**: Process monitoring

### Automated Updates

- **Watchtower**: Runs daily at 4 AM to update containers
- **Only labeled containers** are updated automatically
- **Email notifications** for update events

### Logging

- **Centralized logging** with size limits and rotation
- **JSON format** for structured log analysis
- **Service-specific retention** policies

## 🔐 Security Features

- **No-new-privileges** security option where applicable
- **Read-only Docker socket** access via proxy
- **SSL/TLS termination** at reverse proxy
- **Automatic certificate management** via Let's Encrypt
- **Network isolation** with custom bridge network
- **Non-root user execution** for all services

## 🛠️ Maintenance

### Backup Recommendations

```bash
# Backup configurations
tar -czf appdata-backup-$(date +%Y%m%d).tar.gz /opt/appdata/

# Backup docker-compose and environment
tar -czf compose-backup-$(date +%Y%m%d).tar.gz docker-compose.yml .env
```

### Log Management

```bash
# View service logs
docker compose logs jellyfin
docker compose logs -f sonarr

# Check container health
docker compose ps
```

### Updates

```bash
# Manual update (if watchtower disabled)
docker compose pull
docker compose up -d

# View current versions
docker compose images
```

## 🐛 Troubleshooting

### Common Issues

#### Jellyfin Hardware Acceleration

If `/dev/dri` becomes available, uncomment the devices section in docker-compose.yml:

```yaml
devices:
  - /dev/dri:/dev/dri
group_add:
  - video
  - render
```

#### Permission Issues

Ensure PUID/PGID match your user:

```bash
id $USER  # Note the uid and gid values
```

#### Storage Issues

Monitor disk usage:

```bash
df -h
docker system df
docker system prune  # Clean unused resources
```

#### Network Connectivity

Check service connectivity:

```bash
docker compose exec jellyfin curl -f http://sonarr:8989/
```

## 📈 Scaling & Extensions

### Adding Services

1. Add service definition to docker-compose.yml
2. Include in nas-network
3. Add health check and labels
4. Configure SWAG subdomain if needed

### Resource Adjustment

Monitor resource usage and adjust limits:

```bash
docker stats
```

### External Storage

Additional storage can be mounted and added to service volumes as needed.

## 📄 License

This configuration is provided as-is for educational and personal use. Please ensure compliance with local laws regarding media downloading and sharing.

## 🤝 Contributing

Feel free to submit issues and enhancement requests. When contributing:

1. Test changes thoroughly
2. Update documentation
3. Follow existing patterns and conventions
4. Consider resource impact on Raspberry Pi hardware
