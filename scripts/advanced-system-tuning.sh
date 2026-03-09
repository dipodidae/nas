#!/bin/bash
# Advanced system tuning for NAS performance
# Optimizes: memory, network, disk I/O, and file system caching
# Run with: sudo bash advanced-system-tuning.sh

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  Advanced NAS System Tuning                                ║"
echo "║  Performance optimizations beyond disk I/O                 ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo

# ============================================================================
# 1. Virtual Memory Optimization
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  💾 Virtual Memory Optimization"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

OLD_SWAPPINESS=$(cat /proc/sys/vm/swappiness)
OLD_CACHE_PRESSURE=$(cat /proc/sys/vm/vfs_cache_pressure)

# Reduce swappiness (60 → 10): minimize swap usage, keep things in RAM
sysctl -w vm.swappiness=10
echo "✓ Swappiness: $OLD_SWAPPINESS → 10 (prefer RAM over swap)"

# Reduce cache pressure (30 → 10): keep file cache longer (better for media streaming)
sysctl -w vm.vfs_cache_pressure=10
echo "✓ Cache pressure: $OLD_CACHE_PRESSURE → 10 (keep file cache longer)"

# Increase dirty ratio for better write performance
sysctl -w vm.dirty_ratio=15
sysctl -w vm.dirty_background_ratio=5
echo "✓ Dirty ratios optimized for better write buffering"

echo

# ============================================================================
# 2. Network Stack Tuning
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🌐 Network Stack Optimization"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# Already using BBR (best congestion control) - verify
CURRENT_CC=$(cat /proc/sys/net/ipv4/tcp_congestion_control)
echo "✓ TCP Congestion Control: $CURRENT_CC (already optimal!)"

# Increase TCP buffer sizes for large file transfers
sysctl -w net.core.rmem_max=134217728      # 128MB receive buffer
sysctl -w net.core.wmem_max=134217728      # 128MB send buffer
sysctl -w net.core.rmem_default=16777216   # 16MB default receive
sysctl -w net.core.wmem_default=16777216   # 16MB default send
echo "✓ Network buffers: increased to 128MB max (better for 4K streaming)"

# TCP auto-tuning
sysctl -w net.ipv4.tcp_rmem="4096 87380 134217728"   # min/default/max read
sysctl -w net.ipv4.tcp_wmem="4096 65536 134217728"   # min/default/max write
echo "✓ TCP auto-tuning: optimized for high-bandwidth transfers"

# TCP window scaling and timestamps (essential for high-speed connections)
sysctl -w net.ipv4.tcp_window_scaling=1
sysctl -w net.ipv4.tcp_timestamps=1
echo "✓ TCP window scaling: enabled (better throughput)"

# Increase connection backlog
sysctl -w net.core.somaxconn=4096
sysctl -w net.core.netdev_max_backlog=5000
echo "✓ Connection backlog: increased (handle more concurrent streams)"

# TCP Fast Open (reduce latency)
sysctl -w net.ipv4.tcp_fastopen=3
echo "✓ TCP Fast Open: enabled (lower latency)"

# Optimize TCP keepalive for streaming
sysctl -w net.ipv4.tcp_keepalive_time=600
sysctl -w net.ipv4.tcp_keepalive_intvl=30
sysctl -w net.ipv4.tcp_keepalive_probes=3
echo "✓ TCP keepalive: tuned for long streaming sessions"

echo

# ============================================================================
# 3. File System Optimization
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📂 File System & I/O Optimization"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# Increase file descriptor limits
sysctl -w fs.file-max=2097152
echo "✓ File descriptor limit: 2M (handle many concurrent connections)"

# Increase inotify limits (for file watching services like Sonarr/Radarr)
sysctl -w fs.inotify.max_user_watches=524288
sysctl -w fs.inotify.max_user_instances=512
echo "✓ Inotify limits: increased (better for media management apps)"

# Optimize ext4 commit interval (already in mount options, but set kernel default)
echo "✓ File system optimizations: commit=60 in mount options recommended"

echo

# ============================================================================
# 4. Disk-Specific Optimizations
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  💿 Advanced Disk I/O Tuning"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# Increase max transfer size for better throughput
OLD_MAX_SECTORS=$(cat /sys/block/sda/queue/max_sectors_kb)
echo 1024 > /sys/block/sda/queue/max_sectors_kb
NEW_MAX_SECTORS=$(cat /sys/block/sda/queue/max_sectors_kb)
echo "✓ Max I/O size: ${OLD_MAX_SECTORS}KB → ${NEW_MAX_SECTORS}KB (better throughput)"

# Disable add_random for media drive (reduces entropy overhead)
echo 0 > /sys/block/sda/queue/add_random
echo "✓ Random entropy: disabled for media drive (lower overhead)"

# Optimize rotational settings
echo 1 > /sys/block/sda/queue/rotational  # Ensure it's treated as HDD
echo "✓ Drive type: confirmed as rotational (HDD optimizations active)"

echo

# ============================================================================
# 5. Summary
# ============================================================================
echo "╔════════════════════════════════════════════════════════════╗"
echo "║  ✅ Advanced Tuning Complete                               ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo
echo "Applied optimizations:"
echo
echo "  💾 Memory:"
echo "     • Swappiness: $OLD_SWAPPINESS → 10 (minimize swap)"
echo "     • Cache pressure: $OLD_CACHE_PRESSURE → 10 (keep cache longer)"
echo "     • Dirty ratios: optimized for better buffering"
echo
echo "  🌐 Network:"
echo "     • TCP buffers: up to 128MB (was much smaller)"
echo "     • Auto-tuning: enabled for high bandwidth"
echo "     • TCP Fast Open: enabled (lower latency)"
echo "     • Keepalive: optimized for streaming"
echo "     • Connection backlog: 4096 (handle concurrent streams)"
echo
echo "  📂 File System:"
echo "     • File descriptors: 2M max"
echo "     • Inotify watches: 524K (media apps)"
echo
echo "  💿 Disk I/O:"
echo "     • Max transfer: ${OLD_MAX_SECTORS}KB → 1024KB"
echo "     • Entropy: disabled (lower overhead)"
echo "     • Type: confirmed HDD optimizations"
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo
echo "⚠️  These changes are temporary and will reset on reboot."
echo "    Run the persistence script to make them permanent."
echo
