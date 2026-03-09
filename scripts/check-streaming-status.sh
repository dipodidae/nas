#!/bin/bash
# Check NAS streaming performance and configuration

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  NAS Streaming Performance Check                           ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo

# ============================================================================
# Disk I/O Settings
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📀 Disk I/O Configuration (/dev/sda - Media Drive)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

SCHEDULER=$(cat /sys/block/sda/queue/scheduler)
READAHEAD=$(cat /sys/block/sda/queue/read_ahead_kb)
QUEUE_DEPTH=$(cat /sys/block/sda/queue/nr_requests)
ROTATIONAL=$(cat /sys/block/sda/queue/rotational)

echo "  Scheduler:       $SCHEDULER"
if [[ "$SCHEDULER" == *"[mq-deadline]"* ]]; then
    echo "                   ✅ Optimal for HDD sequential reads"
elif [[ "$SCHEDULER" == *"[none]"* ]]; then
    echo "                   ❌ Poor for HDD - should be mq-deadline"
fi
echo

echo "  Read-ahead:      ${READAHEAD}KB"
if [ "$READAHEAD" -ge 16384 ]; then
    echo "                   ✅ Optimal for 4K streaming"
elif [ "$READAHEAD" -ge 8192 ]; then
    echo "                   ⚠️  Good, but 16MB recommended for 4K remux"
else
    echo "                   ❌ Too low for high-bitrate streaming"
fi
echo

echo "  Queue depth:     $QUEUE_DEPTH"
if [ "$QUEUE_DEPTH" -ge 512 ]; then
    echo "                   ✅ Good throughput"
else
    echo "                   ⚠️  Consider increasing to 1024"
fi
echo

echo "  Drive type:      $([ "$ROTATIONAL" -eq 1 ] && echo "HDD (spinning disk)" || echo "SSD")"
echo

# ============================================================================
# Mount Options
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🗂️  Mount Configuration"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

MOUNT_OPTS=$(mount | grep "/mnt/drive-next" | head -1 | sed 's/.*(\(.*\))/\1/')
echo "  /mnt/drive-next: $MOUNT_OPTS"

if [[ "$MOUNT_OPTS" == *"noatime"* ]]; then
    echo "                   ✅ noatime enabled (reduces I/O)"
fi
if [[ "$MOUNT_OPTS" == *"lazytime"* ]]; then
    echo "                   ✅ lazytime enabled (better performance)"
fi
echo

# ============================================================================
# Docker Container Status
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🐳 Docker Container Status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

echo "  Jellyfin:"
JELLYFIN_STATUS=$(docker inspect --format='{{.State.Status}}' jellyfin 2>/dev/null || echo "not running")
JELLYFIN_HEALTH=$(docker inspect --format='{{.State.Health.Status}}' jellyfin 2>/dev/null || echo "no healthcheck")

echo "    Status:  $JELLYFIN_STATUS"
echo "    Health:  $JELLYFIN_HEALTH"

if [ "$JELLYFIN_STATUS" == "running" ]; then
    # Get resource limits
    MEM_LIMIT=$(docker inspect --format='{{.HostConfig.Memory}}' jellyfin | awk '{printf "%.1fGB", $1/1024/1024/1024}')
    CPU_LIMIT=$(docker inspect --format='{{.HostConfig.NanoCpus}}' jellyfin | awk '{printf "%.1f", $1/1000000000}')

    echo "    Memory:  $MEM_LIMIT limit"
    echo "    CPU:     ${CPU_LIMIT} cores limit"

    if [[ "$MEM_LIMIT" == "1.5GB" ]]; then
        echo "             ✅ Correct for direct-play only"
    elif [[ "$MEM_LIMIT" == "2.0GB" ]]; then
        echo "             ⚠️  Can be reduced to 1.5GB for direct-play"
    fi
fi
echo

echo "  SWAG (Nginx):"
SWAG_STATUS=$(docker inspect --format='{{.State.Status}}' swag 2>/dev/null || echo "not running")
SWAG_HEALTH=$(docker inspect --format='{{.State.Health.Status}}' swag 2>/dev/null || echo "no healthcheck")

echo "    Status:  $SWAG_STATUS"
echo "    Health:  $SWAG_HEALTH"
echo

# ============================================================================
# Resource Usage
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  📊 Current Resource Usage"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

docker stats --no-stream jellyfin swag 2>/dev/null || echo "  Containers not running"
echo

# ============================================================================
# Network Check
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  🌐 Network Status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

echo "  Testing Jellyfin connectivity..."
if curl -sf http://localhost:8096/System/Info/Public > /dev/null 2>&1; then
    echo "  ✅ Jellyfin is accessible locally"
else
    echo "  ❌ Cannot reach Jellyfin on localhost:8096"
fi
echo

echo "  Testing external connectivity..."
if curl -sf -k https://jellyfin.4eva.me > /dev/null 2>&1; then
    echo "  ✅ Jellyfin is accessible via HTTPS"
else
    echo "  ❌ Cannot reach Jellyfin via public domain"
fi
echo

# ============================================================================
# Recommendations
# ============================================================================
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  💡 Recommendations"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# Check if optimizations are applied
NEED_OPTIMIZATION=0

if [[ "$SCHEDULER" != *"[mq-deadline]"* ]]; then
    echo "  ❌ I/O scheduler not optimized"
    NEED_OPTIMIZATION=1
fi

if [ "$READAHEAD" -lt 16384 ]; then
    echo "  ❌ Read-ahead buffer too small"
    NEED_OPTIMIZATION=1
fi

if [ $NEED_OPTIMIZATION -eq 1 ]; then
    echo
    echo "  Run this to apply all optimizations:"
    echo "  $ cd ~/nas/scripts && sudo bash apply-all-fixes.sh"
else
    echo "  ✅ All optimizations appear to be applied!"
    echo
    echo "  If you're still experiencing stuttering:"
    echo "    1. Check upload bandwidth (need 80-150 Mbps for 4K remux)"
    echo "    2. Verify direct play in Jellyfin Dashboard → Activity"
    echo "    3. Try a different client with better codec support"
    echo "    4. Check ISP for upload throttling"
fi
echo
