#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════
# QUICK START GUIDE - qBittorrent Optimization for Overnight Downloads
# ═══════════════════════════════════════════════════════════════════════════

cat << 'EOF'
╔══════════════════════════════════════════════════════════════════════════╗
║  qBittorrent Performance Optimization - Installation Guide              ║
║  Target: Maximum overnight download speed (01:00 - 08:00)               ║
╚══════════════════════════════════════════════════════════════════════════╝

📋 WHAT WILL BE CHANGED:
   • qBittorrent config: Optimized for 2000 connections, 8 active downloads
   • Docker Compose: Increased memory (2GB) and ulimits (65536 FDs)
   • Automation: Time-based scheduler (full speed 01:00-08:00, idle otherwise)
   • Optional: Kernel network tuning (TCP buffers, BBR congestion control)

⚠️  BEFORE YOU START:
   • This will restart your qBittorrent container (active downloads pause briefly)
   • Backup is automatic, but verify: ls -la /mnt/docker-usb/qbittorrent/qBittorrent/*.backup-*
   • Ensure Python 3 + requests library installed (script checks automatically)

═══════════════════════════════════════════════════════════════════════════

STEP 1: Apply Docker Compose Changes
──────────────────────────────────────────────────────────────────────────
The docker-compose.yml has been updated with:
  • ulimits: 65536 file descriptors (for 2000 connections)
  • Memory: 2GB (up from 1GB, supports 256MB disk cache)
  • CPU: 2.0 cores (up from 1.5, for hash checking)

Run:
  cd /home/tom/nas
  docker-compose up -d qbittorrent

This recreates the container with new limits. Downloads resume automatically.

═══════════════════════════════════════════════════════════════════════════

STEP 2: Install Optimized Configuration
──────────────────────────────────────────────────────────────────────────
Run the automated installer:

  cd /home/tom/nas/scripts
  ./install-qbittorrent-optimization.sh

This script will:
  ✓ Backup existing config
  ✓ Stop qBittorrent container
  ✓ Install optimized config file
  ✓ Start container
  ✓ Install Python dependencies (requests)
  ✓ Setup cron job (runs every minute)
  ✓ Test scheduler immediately

═══════════════════════════════════════════════════════════════════════════

STEP 3 (OPTIONAL): Apply Kernel Network Tuning
──────────────────────────────────────────────────────────────────────────
For maximum throughput, apply system-wide network tuning:

  sudo cp /home/tom/nas/scripts/99-qbittorrent-sysctl.conf /etc/sysctl.d/
  sudo sysctl -p /etc/sysctl.d/99-qbittorrent-sysctl.conf

This increases TCP buffer sizes and enables BBR congestion control.

Skip this if:
  • You're unsure about kernel tuning
  • This is a shared/production system
  • Your downloads are already hitting disk I/O limits (120 MB/s)

═══════════════════════════════════════════════════════════════════════════

VERIFICATION
──────────────────────────────────────────────────────────────────────────
1. Check qBittorrent WebUI:
   http://$(hostname -I | awk '{print $1}'):8080
   
   Verify in Settings > Connection:
     • Global max connections: 2000
     • Max connections per torrent: 300
     • Memory cache: 256 MB

2. Check scheduler logs:
   tail -f /tmp/qbittorrent-scheduler.log

   You should see entries like:
     2026-01-21 22:00:00 - INFO - 💤 [22:00] IDLE WINDOW - Throttling to minimum
     2026-01-22 01:00:00 - INFO - 🚀 [01:00] ACTIVE WINDOW - Setting aggressive mode

3. Monitor container stats:
   docker stats qbittorrent --no-stream

   Expected usage during active downloads:
     • CPU: 50-150%
     • MEM: 800MB-1.5GB
     • NET I/O: 50-120 MB/s (depends on swarm health)

═══════════════════════════════════════════════════════════════════════════

TESTING
──────────────────────────────────────────────────────────────────────────
Add a test torrent (well-seeded public torrent like Ubuntu ISO) and verify:

1. During IDLE hours (08:00-01:00):
   • Download speed: ~50 KB/s (throttled)

2. During ACTIVE hours (01:00-08:00):
   • Download speed: ISP maximum (50-100 MB/s on Gigabit)
   • Active torrents: Maximum 8 downloading simultaneously

═══════════════════════════════════════════════════════════════════════════

TROUBLESHOOTING
──────────────────────────────────────────────────────────────────────────
Problem: Scheduler not switching speeds
  → Check cron: crontab -l | grep qbittorrent
  → Check Python: python3 /home/tom/nas/scripts/qbittorrent-scheduler.py
  → Check logs: tail -f /tmp/qbittorrent-scheduler.log

Problem: "Too many open files" error
  → Verify ulimits applied: docker inspect qbittorrent | grep -A5 Ulimits
  → Restart container: docker-compose restart qbittorrent

Problem: Container OOM killed
  → Check memory: docker stats qbittorrent
  → Reduce disk cache: Edit qBittorrent.conf, set DiskCacheSize=128
  → Or increase container memory to 3GB in docker-compose.yml

Problem: Slow downloads even during active hours
  → Check swarm health: Is torrent well-seeded? (need 10+ seeders)
  → Check disk I/O: iostat -x 1 (if await > 50ms, disk is bottleneck)
  → Check CPU: docker stats (if CPU > 180%, reduce active downloads to 6)

═══════════════════════════════════════════════════════════════════════════

ROLLBACK
──────────────────────────────────────────────────────────────────────────
If you need to revert everything:

  # 1. Restore old config
  docker stop qbittorrent
  LATEST_BACKUP=$(ls -t /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf.backup-* | head -1)
  cp "$LATEST_BACKUP" /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf
  
  # 2. Remove scheduler cron
  crontab -l | grep -v qbittorrent-scheduler | crontab -
  
  # 3. Restore old Docker Compose limits (edit docker-compose.yml manually)
  
  # 4. Restart container
  docker start qbittorrent

═══════════════════════════════════════════════════════════════════════════

FINE-TUNING
──────────────────────────────────────────────────────────────────────────
After a few days of use, adjust based on observations:

Too aggressive (disk thrashing, high CPU):
  • Reduce active downloads: Session\MaxActiveDownloads=6
  • Reduce cache: Session\DiskCacheSize=128
  • Reduce connections: Session\MaxConnections=1500

Not aggressive enough (slow downloads, low resource usage):
  • Increase active downloads: Session\MaxActiveDownloads=10
  • Increase cache: Session\DiskCacheSize=512 (if RAM available)
  • Longer download window: Edit scheduler, set START=23, END=9

═══════════════════════════════════════════════════════════════════════════

📚 DOCUMENTATION
──────────────────────────────────────────────────────────────────────────
Full details: /home/tom/nas/scripts/QBITTORRENT_OPTIMIZATION.md

Key files:
  • Config: /mnt/docker-usb/qbittorrent/qBittorrent/qBittorrent.conf
  • Scheduler: /home/tom/nas/scripts/qbittorrent-scheduler.py
  • Logs: /tmp/qbittorrent-scheduler.log
  • Sysctl: /home/tom/nas/scripts/99-qbittorrent-sysctl.conf

═══════════════════════════════════════════════════════════════════════════

Ready to proceed? Run STEP 1 above.
EOF
