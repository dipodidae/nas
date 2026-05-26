#!/bin/bash
# Optimize disk I/O for NAS media streaming
# Run with: sudo bash optimize-disk-io.sh

set -e

echo "=== NAS Disk I/O Optimization ==="
echo

# 1. Set I/O scheduler to mq-deadline (better for HDDs with sequential reads)
echo "[1/3] Setting I/O scheduler to mq-deadline for /dev/sda..."
echo "mq-deadline" > /sys/block/sda/queue/scheduler
CURRENT_SCHEDULER=$(cat /sys/block/sda/queue/scheduler)
echo "✓ Current scheduler: $CURRENT_SCHEDULER"
echo

# 2. Increase read-ahead buffer from 2MB to 16MB for high-bitrate 4K streaming
echo "[2/3] Increasing read-ahead buffer to 16MB..."
OLD_READAHEAD=$(cat /sys/block/sda/queue/read_ahead_kb)
echo 16384 > /sys/block/sda/queue/read_ahead_kb
NEW_READAHEAD=$(cat /sys/block/sda/queue/read_ahead_kb)
echo "✓ Read-ahead: ${OLD_READAHEAD}KB → ${NEW_READAHEAD}KB"
echo

# 3. Optimize queue depth for better throughput
echo "[3/3] Optimizing I/O queue settings..."
# Increase nr_requests for better throughput with large files
OLD_NR_REQUESTS=$(cat /sys/block/sda/queue/nr_requests)
echo 1024 > /sys/block/sda/queue/nr_requests
NEW_NR_REQUESTS=$(cat /sys/block/sda/queue/nr_requests)
echo "✓ Queue depth: ${OLD_NR_REQUESTS} → ${NEW_NR_REQUESTS}"
echo

echo "=== Optimization Complete ==="
echo
echo "Current settings:"
echo "  Scheduler: $(cat /sys/block/sda/queue/scheduler)"
echo "  Read-ahead: $(cat /sys/block/sda/queue/read_ahead_kb)KB"
echo "  Queue depth: $(cat /sys/block/sda/queue/nr_requests)"
echo
echo "⚠️  These changes are temporary and will reset on reboot."
echo "    To make permanent, run: sudo bash $(dirname "$0")/make-io-persistent.sh"
