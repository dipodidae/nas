#!/bin/bash

# Continuous container monitoring script
# Run this to keep an eye on resource usage

echo "🔍 Starting continuous container monitoring..."
echo "Press Ctrl+C to stop"
echo ""

while true; do
    clear
    echo "📊 Container Resource Monitor - $(date)"
    echo "========================================"
    echo ""
    
    # System overview
    echo "🖥️  System Overview:"
    echo "Load: $(uptime | awk -F'load average:' '{print $2}')"
    echo "Memory: $(free -h | awk 'NR==2{printf "%.1f%% (%s/%s)", $3*100/$2, $3, $2}')"
    echo ""
    
    # Container stats
    echo "🐳 Container Stats:"
    docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}"
    echo ""
    
    # High resource usage alerts
    echo "⚠️  Resource Alerts:"
    docker stats --no-stream --format "{{.Container}}\t{{.CPUPerc}}\t{{.MemPerc}}" | \
    awk -F'\t' '$2+0 > 80 || $3+0 > 80 {print "🚨 HIGH USAGE: " $1 " (CPU: " $2 ", MEM: " $3 ")"}'
    
    echo ""
    echo "Next update in 5 seconds..."
    sleep 5
done
