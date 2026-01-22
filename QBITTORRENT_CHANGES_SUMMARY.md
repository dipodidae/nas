# qBittorrent Optimization Summary

**Date:** 2026-01-22  
**Status:** ✅ COMPLETE - Uncapped 24/7 Performance Enabled

## What Was Changed

### 1. Removed All Time-Based Throttling
- ✅ Removed cron job that ran `qbittorrent-scheduler.py` every minute
- ✅ Removed time window enforcement (was: fast 01:00-08:00, throttled 08:00-01:00)
- ✅ Disabled bandwidth scheduling in qBittorrent config

### 2. Speed Limits - Now Uncapped 24/7
| Setting | Before | After |
|---------|--------|-------|
| Download limit | 0 (unlimited) during window<br>50 KB/s outside window | **0 (unlimited) 24/7** |
| Upload limit | 5 MB/s during window<br>50 KB/s outside window | **10 MB/s 24/7** |
| Alternate limits | Enabled by scheduler | **Disabled permanently** |

### 3. Performance Optimizations Applied

#### qBittorrent Configuration
- **Connections:** 2000 → **3000** global, 300 → **400** per torrent
- **Active downloads:** 8 → **10** simultaneous
- **Disk cache:** 256 MB → **512 MB**
- **Async I/O threads:** 8 → **12**
- **Hashing threads:** 2 → **4**
- **File pool:** 500 → **1000** open files
- **Send buffer:** 3 MB → **5 MB**

#### Docker Container
- **Resource limits:** REMOVED (was: 2 CPUs, 2 GB RAM)
- **File descriptors:** 65k → **131k**
- **Process limit:** 8k → **16k**
- **Network sysctls:** Added 7 TCP optimizations

#### Host System (Optional)
- Applied network tuning via `/etc/sysctl.d/99-qbittorrent-sysctl.conf`
- TCP buffer sizes increased to 128 MB
- BBR congestion control (if available)
- Connection queue sizes increased

## How to Verify

```bash
# 1. Check no time-based scheduler in cron
crontab -l | grep scheduler
# Should be empty

# 2. Check container is healthy
docker ps | grep qbittorrent
# Should show "healthy"

# 3. Verify unlimited download speed
docker exec qbittorrent cat /config/qBittorrent/qBittorrent.conf | grep GlobalDLSpeedLimit
# Should show: Session\GlobalDLSpeedLimit=0

# 4. Verify increased connections
docker exec qbittorrent cat /config/qBittorrent/qBittorrent.conf | grep MaxConnections
# Should show: Session\MaxConnections=3000

# 5. Check no resource limits
docker inspect qbittorrent | jq '.[0].HostConfig.NanoCpus'
# Should show: 0 (unlimited)
```

## Expected Performance

- **Download speed:** Unlimited (limited only by seeders and network capacity)
- **Upload speed:** 10 MB/s (80 Mbps) - can be increased if needed
- **Max peers:** 3000 global, 400 per torrent
- **Disk caching:** 512 MB RAM cache reduces HDD thrashing
- **Availability:** 24/7 full speed (no time windows)

## Files Modified

1. **`/mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf`**
   - Backup created: `qBittorrent.conf.backup-YYYYMMDD-HHMMSS`
   - Set all speed limits to uncapped
   - Increased connection and cache limits

2. **`docker-compose.yml`**
   - Removed CPU/memory resource limits
   - Increased file descriptor limits
   - Added network sysctls

3. **`/etc/sysctl.d/99-qbittorrent-sysctl.conf`** (host-level, optional)
   - TCP buffer tuning
   - Connection queue tuning
   - BBR congestion control

## Files NOT Modified

- `scripts/qbittorrent-scheduler.py` - Kept for reference (not executed)
- `scripts/qbittorrent_stalled_kickstart.py` - Still runs daily at 04:00 (different purpose)
- `scripts/install-qbittorrent-optimization.sh` - Kept for reference

## Documentation

Full documentation: **`QBITTORRENT_OPTIMIZATION_UNCAPPED.md`**

Includes:
- Complete changelog of all optimizations
- Explanation of each setting and why it improves performance
- Rollback procedure
- Troubleshooting guide
- Performance monitoring commands
- Security considerations

## Rollback (If Needed)

```bash
# 1. Restore config backup
docker stop qbittorrent
cp /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf.backup-* \
   /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf

# 2. Re-enable scheduler
(crontab -l; echo "* * * * * /usr/bin/python3 /home/tom/nas/scripts/qbittorrent-scheduler.py >> /tmp/qbittorrent-scheduler.log 2>&1") | crontab -

# 3. Restore docker-compose.yml limits (manual edit required)

# 4. Restart
docker compose up -d qbittorrent
```

## Next Steps

1. **Monitor performance:** Check WebUI at http://your-server:8080
2. **Watch disk space:** Fast downloads can fill disk quickly
3. **Monitor memory:** Run `docker stats qbittorrent` to ensure RAM usage is acceptable
4. **Adjust upload limit:** If needed, change `Session\GlobalUPSpeedLimit` (in KB/s)

---

**Configuration is now optimized for maximum sustained torrent download performance, 24/7, without artificial throttling.**
