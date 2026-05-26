#!/bin/bash
# Make disk I/O optimizations persistent across reboots
# Run with: sudo bash make-io-persistent.sh

set -e

echo "=== Making I/O Optimizations Persistent ==="
echo

UDEV_RULE="/etc/udev/rules.d/60-nas-disk-optimization.rules"

echo "[1/2] Creating udev rule for automatic optimization..."
cat > "$UDEV_RULE" << 'EOF'
# NAS disk I/O optimization for media streaming
# Applies to /dev/sda (main media drive)

# Set I/O scheduler to mq-deadline for better HDD performance
ACTION=="add|change", KERNEL=="sda", ATTR{queue/scheduler}="mq-deadline"

# Increase read-ahead to 16MB for high-bitrate 4K streaming
ACTION=="add|change", KERNEL=="sda", ATTR{queue/read_ahead_kb}="16384"

# Optimize queue depth for better throughput
ACTION=="add|change", KERNEL=="sda", ATTR{queue/nr_requests}="1024"
EOF

echo "✓ Created udev rule: $UDEV_RULE"
echo

echo "[2/2] Reloading udev rules..."
udevadm control --reload-rules
udevadm trigger --subsystem-match=block --action=change
echo "✓ Udev rules reloaded"
echo

echo "=== Persistent Optimization Complete ==="
echo
echo "Settings will now apply automatically on every boot."
echo "Current settings:"
echo "  Scheduler: $(cat /sys/block/sda/queue/scheduler)"
echo "  Read-ahead: $(cat /sys/block/sda/queue/read_ahead_kb)KB"
echo "  Queue depth: $(cat /sys/block/sda/queue/nr_requests)"
