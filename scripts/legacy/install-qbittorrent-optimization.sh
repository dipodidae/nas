#!/usr/bin/env bash
set -euo pipefail
IFS=$'\n\t'

# ═══════════════════════════════════════════════════════════════════════════
# qBittorrent Optimization Installer
# Applies configuration, sets up scheduler cron, and optionally applies sysctl
# ═══════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/mnt/docker-usb/qbittorrent/qBittorrent"
BACKUP_SUFFIX="backup-$(date +%Y%m%d-%H%M%S)"

echo "🚀 qBittorrent Optimization Installer"
echo "════════════════════════════════════════════════════════════════════════"

# ───────────────────────────────────────────────────────────────────────────
# 1. Backup existing configuration
# ───────────────────────────────────────────────────────────────────────────
echo "📦 Backing up current config..."
if [[ -f "$CONFIG_DIR/qBittorrent.conf" ]]; then
    cp "$CONFIG_DIR/qBittorrent.conf" "$CONFIG_DIR/qBittorrent.conf.$BACKUP_SUFFIX"
    echo "   ✓ Backup: $CONFIG_DIR/qBittorrent.conf.$BACKUP_SUFFIX"
else
    echo "   ⚠ No existing config found (fresh install?)"
fi

# ───────────────────────────────────────────────────────────────────────────
# 2. Stop qBittorrent container
# ───────────────────────────────────────────────────────────────────────────
echo "🛑 Stopping qBittorrent container..."
docker stop qbittorrent || echo "   ⚠ Container not running"

# ───────────────────────────────────────────────────────────────────────────
# 3. Install optimized configuration
# ───────────────────────────────────────────────────────────────────────────
echo "📝 Installing optimized config..."
cp "$SCRIPT_DIR/qbittorrent-optimized.conf" "$CONFIG_DIR/qBittorrent.conf"
echo "   ✓ Config installed"

# ───────────────────────────────────────────────────────────────────────────
# 4. Start qBittorrent container
# ───────────────────────────────────────────────────────────────────────────
echo "▶️  Starting qBittorrent container..."
docker start qbittorrent
sleep 5
echo "   ✓ Container started"

# ───────────────────────────────────────────────────────────────────────────
# 5. Install Python dependencies for scheduler
# ───────────────────────────────────────────────────────────────────────────
echo "📦 Installing Python dependencies..."
if command -v pip3 &>/dev/null; then
    pip3 install --user requests &>/dev/null || pip3 install requests
    echo "   ✓ Python 'requests' library installed"
else
    echo "   ⚠ pip3 not found - install manually: pip3 install requests"
fi

# ───────────────────────────────────────────────────────────────────────────
# 6. Setup cron job for time-based scheduling
# ───────────────────────────────────────────────────────────────────────────
echo "⏰ Setting up cron scheduler..."
CRON_CMD="* * * * * /usr/bin/python3 $SCRIPT_DIR/qbittorrent-scheduler.py >> /tmp/qbittorrent-scheduler.log 2>&1"
CRON_COMMENT="# qBittorrent time-based speed scheduler"

# Remove existing cron entries for this script
crontab -l 2>/dev/null | grep -v "qbittorrent-scheduler.py" | crontab - 2>/dev/null || true

# Add new cron entry
(crontab -l 2>/dev/null; echo "$CRON_COMMENT"; echo "$CRON_CMD") | crontab -
echo "   ✓ Cron job installed (runs every minute)"
echo "   📄 Logs: /tmp/qbittorrent-scheduler.log"

# ───────────────────────────────────────────────────────────────────────────
# 7. Test scheduler immediately
# ───────────────────────────────────────────────────────────────────────────
echo "🧪 Testing scheduler..."
sleep 5  # Wait for qBittorrent API to be ready
python3 "$SCRIPT_DIR/qbittorrent-scheduler.py" || echo "   ⚠ Test failed - check logs"

# ───────────────────────────────────────────────────────────────────────────
# 8. Optional: Apply kernel tuning
# ───────────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════════════"
echo "⚠️  OPTIONAL: Kernel Network Tuning"
echo "════════════════════════════════════════════════════════════════════════"
echo "For maximum performance, apply kernel-level network tuning:"
echo ""
echo "  sudo cp $SCRIPT_DIR/99-qbittorrent-sysctl.conf /etc/sysctl.d/"
echo "  sudo sysctl -p /etc/sysctl.d/99-qbittorrent-sysctl.conf"
echo ""
echo "This increases TCP buffer sizes and connection limits system-wide."
echo "Skip if you're unsure or on a shared system."
echo ""

# ───────────────────────────────────────────────────────────────────────────
# 9. Summary
# ───────────────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════════════════════════"
echo "✅ Installation Complete!"
echo "════════════════════════════════════════════════════════════════════════"
echo ""
echo "📊 Configuration Summary:"
echo "   • Download window: 01:00 - 08:00 (unlimited speed)"
echo "   • Idle window: 08:00 - 01:00 (50 KB/s throttle)"
echo "   • Max connections: 2000 global, 300 per torrent"
echo "   • Active downloads: 8 simultaneous"
echo "   • Disk cache: 256 MB"
echo "   • Seeding: Stop at 1.0 ratio or 24 hours"
echo ""
echo "📝 Next Steps:"
echo "   1. Check WebUI: http://$(hostname -I | awk '{print $1}'):8080"
echo "   2. Monitor scheduler: tail -f /tmp/qbittorrent-scheduler.log"
echo "   3. Add torrents and verify download speed during active hours"
echo ""
echo "🔧 To revert: docker stop qbittorrent && cp $CONFIG_DIR/qBittorrent.conf.$BACKUP_SUFFIX $CONFIG_DIR/qBittorrent.conf && docker start qbittorrent"
echo ""
