#!/bin/bash
# Apply all NAS performance fixes
# This script combines all optimizations for Jellyfin streaming performance

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAS_DIR="$(dirname "$SCRIPT_DIR")"

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  NAS Performance Optimization - Complete Fix               ║"
echo "║  Fixing stuttering issues with 4K remux streaming          ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo

# Check if running as root
if [ "$EUID" -ne 0 ]; then
    echo "⚠️  This script requires sudo access for I/O optimizations"
    echo "   Running with sudo..."
    exec sudo bash "$0" "$@"
fi

echo "Working directory: $NAS_DIR"
echo

# ============================================================================
# 1. Optimize Disk I/O
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  1. Optimizing Disk I/O for HDD Streaming"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

bash "$SCRIPT_DIR/optimize-disk-io.sh"
echo

# ============================================================================
# 2. Make I/O optimizations persistent
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  2. Making I/O Optimizations Persistent"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

bash "$SCRIPT_DIR/make-io-persistent.sh"
echo

# ============================================================================
# 3. Restart Docker containers with new configuration
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  3. Restarting Services with Optimized Configuration"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

cd "$NAS_DIR"

echo "[1/3] Restarting Nginx (SWAG) to apply new config..."
docker compose restart swag
echo "✓ Nginx restarted"
echo

echo "[2/3] Recreating Jellyfin with new resource limits..."
docker compose up -d jellyfin
echo "✓ Jellyfin recreated"
echo

echo "[3/3] Waiting for services to be healthy..."
sleep 5

# Check health
SWAG_HEALTH=$(docker inspect --format='{{.State.Health.Status}}' swag 2>/dev/null || echo "unknown")
JELLYFIN_HEALTH=$(docker inspect --format='{{.State.Health.Status}}' jellyfin 2>/dev/null || echo "unknown")

echo "  SWAG status: $SWAG_HEALTH"
echo "  Jellyfin status: $JELLYFIN_HEALTH"
echo

# ============================================================================
# Summary
# ============================================================================
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  ✅ All Optimizations Applied Successfully                 ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo
echo "Changes applied:"
echo
echo "  📀 Disk I/O:"
echo "     • Scheduler: none → mq-deadline (better HDD performance)"
echo "     • Read-ahead: 2MB → 16MB (better 4K streaming)"
echo "     • Queue depth: optimized for large files"
echo "     • Persistent across reboots ✓"
echo
echo "  🎬 Jellyfin:"
echo "     • Memory: 2GB → 1.5GB (direct-play optimized)"
echo "     • CPU: 2.5 cores → 1.0 core (direct-play optimized)"
echo "     • I/O priority: weight 800 (high priority)"
echo
echo "  🌐 Nginx:"
echo "     • Sendfile enabled for efficient large file transfers"
echo "     • Optimized buffer sizes for high-bitrate streaming"
echo "     • Extended timeouts for long streaming sessions"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "📊 Current system status:"
echo
docker stats --no-stream jellyfin
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "🎯 Next Steps:"
echo
echo "  1. Test remote streaming - stuttering should be significantly reduced"
echo "  2. If still having issues, check your upload bandwidth:"
echo "     • 4K remux files need 80-150 Mbps upload minimum"
echo "     • Run: speedtest-cli (install if needed)"
echo
echo "  3. Monitor Jellyfin playback:"
echo "     • Dashboard → Activity"
echo "     • Should show 'Direct Play' (not transcoding)"
echo
echo "  4. For best remote streaming experience:"
echo "     • Use Jellyfin clients with good codec support"
echo "     • Consider lower bitrate versions for remote access"
echo "     • Check if ISP throttles large uploads"
echo
echo "✅ Optimization complete!"
