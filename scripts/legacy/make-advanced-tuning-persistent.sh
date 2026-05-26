#!/bin/bash
# Make advanced system tuning persistent across reboots
# Run with: sudo bash make-advanced-tuning-persistent.sh

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  Making Advanced Tuning Persistent                         ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo

SYSCTL_CONF="/etc/sysctl.d/99-nas-performance.conf"
UDEV_RULE="/etc/udev/rules.d/60-nas-disk-optimization.rules"

# ============================================================================
# 1. Create sysctl configuration for kernel parameters
# ============================================================================
echo "[1/3] Creating persistent sysctl configuration..."

cat > "$SYSCTL_CONF" << 'EOF'
# NAS Performance Tuning - System-wide optimizations
# Created by advanced-system-tuning.sh
# Location: /etc/sysctl.d/99-nas-performance.conf

# ============================================================================
# Virtual Memory Optimization
# ============================================================================
# Reduce swap usage - prefer keeping things in RAM
vm.swappiness=10

# Keep file cache longer (better for media streaming)
vm.vfs_cache_pressure=10

# Optimize write buffering
vm.dirty_ratio=15
vm.dirty_background_ratio=5

# ============================================================================
# Network Stack Tuning for High-Bandwidth Streaming
# ============================================================================
# TCP buffer sizes - allow up to 128MB for large transfers
net.core.rmem_max=134217728
net.core.wmem_max=134217728
net.core.rmem_default=16777216
net.core.wmem_default=16777216

# TCP auto-tuning for optimal throughput
net.ipv4.tcp_rmem=4096 87380 134217728
net.ipv4.tcp_wmem=4096 65536 134217728

# TCP optimizations
net.ipv4.tcp_window_scaling=1
net.ipv4.tcp_timestamps=1
net.ipv4.tcp_fastopen=3

# Connection handling
net.core.somaxconn=4096
net.core.netdev_max_backlog=5000

# TCP keepalive for long streaming sessions
net.ipv4.tcp_keepalive_time=600
net.ipv4.tcp_keepalive_intvl=30
net.ipv4.tcp_keepalive_probes=3

# ============================================================================
# File System Limits
# ============================================================================
# Increase file descriptor limit
fs.file-max=2097152

# Inotify limits for media management apps (Sonarr, Radarr, etc.)
fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=512
EOF

echo "✓ Created: $SYSCTL_CONF"
echo

# ============================================================================
# 2. Update udev rules to include new disk optimizations
# ============================================================================
echo "[2/3] Updating udev rules with advanced disk optimizations..."

cat > "$UDEV_RULE" << 'EOF'
# NAS disk I/O optimization for media streaming
# Applies to /dev/sda (main media drive)

# Basic I/O settings
ACTION=="add|change", KERNEL=="sda", ATTR{queue/scheduler}="mq-deadline"
ACTION=="add|change", KERNEL=="sda", ATTR{queue/read_ahead_kb}="16384"
ACTION=="add|change", KERNEL=="sda", ATTR{queue/nr_requests}="1024"

# Advanced I/O settings
ACTION=="add|change", KERNEL=="sda", ATTR{queue/max_sectors_kb}="1024"
ACTION=="add|change", KERNEL=="sda", ATTR{queue/add_random}="0"
ACTION=="add|change", KERNEL=="sda", ATTR{queue/rotational}="1"
EOF

echo "✓ Updated: $UDEV_RULE"
echo

# ============================================================================
# 3. Apply and verify
# ============================================================================
echo "[3/3] Applying configurations..."

# Apply sysctl settings immediately
sysctl -p "$SYSCTL_CONF" > /dev/null 2>&1
echo "✓ Sysctl settings applied"

# Reload udev rules
udevadm control --reload-rules
udevadm trigger --subsystem-match=block --action=change
echo "✓ Udev rules reloaded"

echo

# ============================================================================
# Verification
# ============================================================================
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  ✅ Persistent Configuration Complete                      ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo
echo "Configuration files created:"
echo "  • $SYSCTL_CONF"
echo "  • $UDEV_RULE"
echo
echo "Current settings:"
echo "  VM swappiness: $(cat /proc/sys/vm/swappiness)"
echo "  Cache pressure: $(cat /proc/sys/vm/vfs_cache_pressure)"
echo "  TCP max buffers: $(cat /proc/sys/net/core/rmem_max) bytes"
echo "  Disk scheduler: $(cat /sys/block/sda/queue/scheduler)"
echo "  Read-ahead: $(cat /sys/block/sda/queue/read_ahead_kb)KB"
echo "  Max I/O size: $(cat /sys/block/sda/queue/max_sectors_kb)KB"
echo
echo "✅ All optimizations will now apply automatically on every boot!"
echo
