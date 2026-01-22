# qBittorrent Optimization - Uncapped 24/7 Performance

## Overview
This document describes the optimizations applied to maximize qBittorrent download performance 24/7 without any time-based throttling.

**Date Applied:** 2026-01-22  
**Previous State:** Time-based scheduling (01:00-08:00 fast, 08:00-01:00 throttled to 50 KB/s)  
**Current State:** Fully uncapped 24/7 with aggressive performance tuning

---

## Changes Made

### 1. Removed Time-Based Scheduling ✓

#### Cron Job Removal
- **Removed:** Cron job running `qbittorrent-scheduler.py` every minute
- **Command:** `crontab -l | grep -v qbittorrent-scheduler.py | crontab -`
- **Verified:** No time-based scheduling in cron

#### Files Preserved (Not Deleted)
- `scripts/qbittorrent-scheduler.py` - Kept for reference but not executed
- `scripts/install-qbittorrent-optimization.sh` - Kept for reference

---

### 2. qBittorrent Configuration Optimizations

**Config File:** `/mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf`  
**Backup Created:** `qBittorrent.conf.backup-YYYYMMDD-HHMMSS`

#### Speed Limits - UNCAPPED
```ini
Session\GlobalDLSpeedLimit=0                    # 0 = unlimited download
Session\GlobalUPSpeedLimit=10240                # 10 MB/s upload (maintain good ratio)
Session\AlternativeGlobalDLSpeedLimit=0         # No alternate limits
Session\AlternativeGlobalUPSpeedLimit=0         # No alternate limits
Session\UseAlternativeGlobalSpeedLimit=false    # Never use alternate limits
Session\BandwidthSchedulerEnabled=false         # No scheduling
```

**Why:** Removes all artificial speed caps. Download is unlimited; upload set to 10 MB/s to maintain swarm health while prioritizing downloads.

#### Connection Limits - MAXIMIZED
```ini
Session\MaxConnections=3000                     # Up from 2000
Session\MaxConnectionsPerTorrent=400            # Up from 300
Session\MaxHalfOpenConnections=150              # Up from 100
```

**Why:** 
- More connections = more peers = better piece availability
- 3000 global connections supported by increased ulimits
- 400 per torrent allows popular torrents to saturate bandwidth

#### Active Downloads - INCREASED
```ini
Session\MaxActiveDownloads=10                   # Up from 8
Session\MaxActiveTorrents=15                    # Up from 12
Session\MaxActiveUploads=8                      # Up from 6
Session\MaxUploadsPerTorrent=8                  # Up from 6
```

**Why:** Can now handle more simultaneous downloads without queueing delays.

#### Disk I/O - AGGRESSIVE CACHING
```ini
Session\DiskCacheSize=512                       # 512 MB (up from 256 MB)
Session\DiskCacheTTL=180                        # 180s (up from 120s)
Session\AsyncIOThreadsCount=12                  # 12 threads (up from 8)
Session\CheckingMemUsageSize=1024               # 1 GB for hash checking
Session\FilePoolSize=1000                       # 1000 open files (up from 500)
Session\HashingThreadsCount=4                   # 4 threads (up from 2)
Session\SendBufferWatermark=5120                # 5 MB send buffer
Session\SendBufferWatermarkFactor=200           # 200%
Session\SocketBacklogSize=300                   # 300 queued connections
Session\DiskIOType=1                            # Async I/O mode
```

**Why:**
- **512 MB cache:** Reduces HDD thrashing significantly on large downloads
- **12 async I/O threads:** Parallel writes across multiple torrents
- **1 GB hash checking:** Faster verification on completion
- **Larger buffers:** Smooth sustained uploads without blocking downloads

#### Protocol Tuning
```ini
Session\BTProtocol=Both                         # Both TCP and uTP
Session\uTPRateLimited=false                    # No uTP rate limit
Session\uTPMixedMode=Prefer TCP                 # TCP preferred for bulk transfers
Session\Encryption=1                            # Prefer plaintext (lower CPU)
```

**Why:**
- TCP preferred for large file transfers (lower latency than uTP)
- No uTP rate limiting allows both protocols to run full speed
- Plaintext encryption reduces CPU overhead on Raspberry Pi

#### Advanced Tuning
```ini
Session\SaveResumeDataInterval=10               # Save every 10 min (down from 15)
Session\MinReconnectTimeout=20                  # Fast retry on peer failures
Session\MaxReconnectTimeout=240                 # Max retry interval
Session\RefreshInterval=1200                    # Tracker refresh 20 min
Session\ChokingAlgorithm=1                      # Rate-based choking
Session\SeedChokingAlgorithm=1                  # Fastest upload choking
```

**Why:** Faster peer reconnection and optimized choking algorithm for sustained throughput.

---

### 3. Docker Container Optimizations

**File:** `docker-compose.yml` - qbittorrent service

#### Removed Resource Limits
```yaml
# REMOVED (was limiting to 2 CPU cores and 2 GB RAM):
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 2G
```

**Why:** Container can now use all available system resources during peak downloads.

#### Increased File Descriptor Limits
```yaml
ulimits:
  nofile:
    soft: 131072                # 131k open files (up from 65k)
    hard: 131072
  nproc:
    soft: 16384                 # 16k processes (up from 8k)
    hard: 16384
```

**Why:** 3000 connections + 1000 file pool + overhead requires higher limits.

#### Container-Level Network Tuning
```yaml
sysctls:
  - net.ipv4.tcp_window_scaling=1       # Essential for high-bandwidth
  - net.ipv4.tcp_timestamps=1           # Better RTT estimation
  - net.ipv4.tcp_sack=1                 # Selective ACK for packet loss
  - net.ipv4.tcp_tw_reuse=1             # Reuse TIME_WAIT sockets
  - net.ipv4.tcp_fin_timeout=15         # Faster connection cleanup
  - net.ipv4.tcp_moderate_rcvbuf=1      # Auto-tune receive buffer
  - net.ipv4.tcp_no_metrics_save=1      # Don't cache per-peer metrics
```

**Why:** Optimizes TCP stack for high connection count and sustained throughput.

---

### 4. Host-Level Network Tuning (OPTIONAL)

**File:** `/etc/sysctl.d/99-qbittorrent-sysctl.conf`

These settings apply system-wide and persist across reboots. Apply with:
```bash
sudo sysctl -p /etc/sysctl.d/99-qbittorrent-sysctl.conf
```

#### TCP Buffer Sizes
```ini
net.core.rmem_max = 134217728           # 128 MB max receive buffer
net.core.wmem_max = 134217728           # 128 MB max send buffer
net.ipv4.tcp_rmem = 4096 87380 134217728    # TCP auto-tuning
net.ipv4.tcp_wmem = 4096 65536 134217728    # TCP auto-tuning
```

**Why:** Allows TCP auto-tuning to scale buffers for high-bandwidth connections.

#### Connection Handling
```ini
net.core.netdev_max_backlog = 5000      # Queue size for incoming packets
net.core.somaxconn = 4096               # Listen queue size
net.ipv4.tcp_max_syn_backlog = 8192     # SYN queue for new connections
```

**Why:** Handles bursts during peer discovery without dropping connections.

#### TCP Congestion Control
```ini
net.ipv4.tcp_congestion_control = bbr   # BBR algorithm
net.core.default_qdisc = fq             # Fair Queue discipline
```

**Why:** BBR (Bottleneck Bandwidth & RTT) is optimal for bulk transfers. Falls back to `cubic` if BBR unavailable.

#### File Descriptors
```ini
fs.file-max = 100000                    # System-wide FD limit
```

**Why:** Supports 3000 connections + disk I/O + other processes.

---

## Performance Impact Summary

| Metric | Before (Scheduled) | After (24/7 Uncapped) | Improvement |
|--------|-------------------|----------------------|-------------|
| **Download Speed** | 01:00-08:00: Unlimited<br>08:00-01:00: 50 KB/s | 24/7: Unlimited | +24/7 availability |
| **Upload Speed** | 01:00-08:00: 5 MB/s<br>08:00-01:00: 50 KB/s | 24/7: 10 MB/s | +100% average |
| **Max Connections** | 2000 | 3000 | +50% |
| **Connections/Torrent** | 300 | 400 | +33% |
| **Disk Cache** | 256 MB | 512 MB | +100% |
| **Async I/O Threads** | 8 | 12 | +50% |
| **File Descriptors** | 65k | 131k | +100% |
| **CPU/RAM Limits** | 2 cores / 2 GB | Unlimited | Container can burst |
| **Active Downloads** | 8 | 10 | +25% |

---

## Verification Steps

### 1. Verify Time-Based Scheduler is Removed
```bash
# Should return 0 or empty
crontab -l | grep -c qbittorrent-scheduler
```

### 2. Verify Configuration
```bash
docker exec qbittorrent cat /config/qBittorrent/qBittorrent.conf | grep -E "GlobalDLSpeedLimit|MaxConnections|DiskCacheSize"
```

Expected output:
```
Session\GlobalDLSpeedLimit=0
Session\MaxConnections=3000
Session\DiskCacheSize=512
```

### 3. Verify Container Limits Removed
```bash
docker inspect qbittorrent | jq '.[0].HostConfig.NanoCpus'
```

Expected: `0` (unlimited)

### 4. Verify Sysctls Applied
```bash
docker exec qbittorrent sysctl net.ipv4.tcp_sack
```

Expected: `net.ipv4.tcp_sack = 1`

### 5. Check Container Health
```bash
docker ps | grep qbittorrent
```

Should show "healthy" status after 40 seconds.

---

## Rollback Procedure

If you need to revert to the time-based scheduled approach:

### 1. Restore Previous Configuration
```bash
# Find latest backup
ls -lt /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf.backup-* | head -1

# Restore (replace YYYYMMDD-HHMMSS with actual timestamp)
docker stop qbittorrent
cp /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf.backup-YYYYMMDD-HHMMSS \
   /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf
```

### 2. Restore Docker Limits
```bash
# Edit docker-compose.yml and add back under qbittorrent service:
deploy:
  resources:
    limits:
      cpus: '2.0'
      memory: 2G
    reservations:
      cpus: '0.5'
      memory: 256M
```

### 3. Re-enable Scheduler
```bash
# Add cron job
(crontab -l; echo "* * * * * /usr/bin/python3 /home/tom/nas/scripts/qbittorrent-scheduler.py >> /tmp/qbittorrent-scheduler.log 2>&1") | crontab -

# Restart container
docker compose up -d qbittorrent
```

---

## Expected Real-World Performance

### Download Speed
- **Public torrents:** 100-800 Mbps (depends on seeders)
- **Well-seeded torrents:** 900+ Mbps (near-gigabit saturation)
- **Limited seeders:** 10-50 Mbps (bottleneck is swarm, not config)

### Upload Speed
- Capped at 10 MB/s (80 Mbps) to prioritize downloads
- Can be increased to 0 (unlimited) if you want maximum ratio

### Disk I/O
- 512 MB cache reduces HDD seeks significantly
- Multiple simultaneous downloads benefit from async I/O

### CPU Usage
- Typical: 10-30% on Raspberry Pi 5
- Peak (hash checking): 50-80%
- No encryption overhead (plaintext preferred)

---

## Additional Optimizations Considered

### Not Implemented (Why)
1. **Host network mode** - Would bypass Docker networking but breaks container isolation
2. **Privileged mode** - Security risk outweighs marginal performance gain
3. **Disabling DHT/PEX/LSD** - Would reduce peer discovery significantly
4. **Unlimited upload** - Could saturate uplink and slow downloads on asymmetric connections
5. **Disabling encryption entirely** - Some peers require it; current preference is fine

### Already Optimal
- File preallocation enabled (prevents fragmentation)
- Sequential piece selection enabled (HDD-friendly)
- Extensive tracker list (good peer discovery)
- Announce to all tiers enabled (faster tracker response)

---

## Monitoring & Maintenance

### Log Locations
- **qBittorrent logs:** `/mnt/docker-usb/qbittorrent/qBittorrent/logs/`
- **Docker logs:** `docker logs qbittorrent`
- **Scheduler log (inactive):** `/tmp/qbittorrent-scheduler.log`

### Performance Metrics to Monitor
```bash
# Current download/upload rates
docker exec qbittorrent curl -s http://localhost:8080/api/v2/transfer/info | jq

# Connection count
docker exec qbittorrent curl -s http://localhost:8080/api/v2/torrents/info | jq '[.[].num_seeds] | add'

# Disk cache hit rate (check logs)
docker exec qbittorrent cat /config/qBittorrent/logs/qbittorrent.log | grep cache
```

### Health Checks
- WebUI responsive: `http://your-server-ip:8080`
- Container healthy: `docker ps | grep qbittorrent`
- No crash loops: `docker logs qbittorrent | grep -i error`

---

## Security Considerations

### What's Safe
- ✅ Increased file descriptors (scoped to container)
- ✅ Container-level sysctls (namespaced, don't affect host)
- ✅ Removed resource limits (Docker monitors overall usage)
- ✅ Network tuning (improves efficiency, no security impact)

### What to Monitor
- ⚠️ Memory usage: No hard limit set; monitor with `docker stats qbittorrent`
- ⚠️ Disk space: Fast downloads can fill disk quickly; monitor `/downloads`
- ⚠️ CPU usage: Hash checking on 10+ torrents can spike CPU

### Best Practices
- Keep WebUI password strong (set via `QBITTORRENT_PASS` env var)
- Use SWAG reverse proxy for HTTPS access
- Don't expose qBittorrent directly to internet (use VPN or private network)
- Regularly check for qBittorrent updates (Watchtower handles this)

---

## Troubleshooting

### Container Won't Start
```bash
# Check sysctls compatibility
docker logs qbittorrent 2>&1 | grep sysctl

# Remove sysctls if needed (edit docker-compose.yml)
```

### Slow Download Despite Optimizations
1. **Check seeders:** Low seeders = slow download (not config issue)
2. **Check disk I/O:** `iostat -x 1` - if %util is 100%, HDD is bottleneck
3. **Check network:** `iftop` - verify network isn't saturated by other services
4. **Check WebUI:** Settings → Speed → Verify limits are 0

### High Memory Usage
```bash
# Check current usage
docker stats qbittorrent --no-stream

# Reduce cache if needed (edit config)
Session\DiskCacheSize=256  # Down from 512
```

### Connection Issues
```bash
# Verify port forwarding
curl -s https://portchecker.co/check?port=6881

# Check firewall
sudo ufw status | grep 6881
```

---

## References

- **qBittorrent Wiki:** https://github.com/qbittorrent/qBittorrent/wiki
- **libtorrent Manual:** https://www.libtorrent.org/reference.html
- **Linux Network Tuning:** https://www.kernel.org/doc/Documentation/networking/ip-sysctl.txt
- **BBR Congestion Control:** https://blog.apnic.net/2020/01/10/when-to-use-and-not-use-bbr/

---

## Changelog

### 2026-01-22 - Initial Uncapped Configuration
- Removed time-based scheduler cron job
- Set download speed to unlimited (0)
- Increased upload speed to 10 MB/s (24/7)
- Removed Docker CPU/memory limits
- Increased connections: 2000 → 3000 global, 300 → 400 per torrent
- Doubled disk cache: 256 MB → 512 MB
- Increased async I/O threads: 8 → 12
- Increased file descriptors: 65k → 131k
- Added container-level network sysctls
- Applied host-level network tuning
- Created comprehensive documentation

---

**Status:** ✅ ACTIVE - Uncapped 24/7 Performance Enabled
