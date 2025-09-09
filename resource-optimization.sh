#!/bin/bash

# Resource Optimization Script for NAS Docker Setup
# This script helps manage container resources and prevent performance issues

echo "🔧 NAS Resource Optimization Script"
echo "===================================="

# Function to stop and remove Calibre containers
cleanup_calibre() {
    echo "🗑️  Removing Calibre and Calibre-web containers..."
    docker stop calibre calibre-web 2>/dev/null || true
    docker rm calibre calibre-web 2>/dev/null || true
    echo "✅ Calibre containers removed"
}

# Function to recreate containers with resource limits
apply_resource_limits() {
    echo "🚀 Applying resource limits and recreating containers..."

    # Stop all containers
    docker compose down

    # Remove any orphaned containers
    docker compose rm -f

    # Start with resource limits
    docker compose up -d

    echo "✅ Containers recreated with resource limits"
}

# Function to monitor resource usage
monitor_resources() {
    echo "📊 Current resource usage:"
    echo "=========================="

    # System load
    echo "📈 System Load:"
    uptime
    echo ""

    # Memory usage
    echo "💾 Memory Usage:"
    free -h
    echo ""

    # Container stats
    echo "🐳 Container Resource Usage:"
    docker stats --no-stream --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}\t{{.BlockIO}}"
    echo ""
}

# Function to create monitoring script
create_monitoring_script() {
    cat > /home/tom/nas/monitor-containers.sh << 'EOF'
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
EOF

    chmod +x /home/tom/nas/monitor-containers.sh
    echo "✅ Created monitoring script: monitor-containers.sh"
}

# Function to create resource limit summary
show_resource_summary() {
    echo "📋 Applied Resource Limits Summary:"
    echo "=================================="
    echo ""
    echo "🎬 Media Services:"
    echo "  • Jellyfin:    3.0 CPU, 2GB RAM (transcoding needs)"
    echo "  • Plex:        2.0 CPU, 1GB RAM"
    echo ""
    echo "📥 Download Stack:"
    echo "  • qBittorrent: 1.5 CPU, 1GB RAM"
    echo "  • Sonarr:      0.5 CPU, 512MB RAM"
    echo "  • Radarr:      0.5 CPU, 512MB RAM"
    echo "  • Prowlarr:    0.5 CPU, 512MB RAM"
    echo "  • Bazarr:      0.5 CPU, 512MB RAM"
    echo ""
    echo "☁️  Infrastructure:"
    echo "  • SWAG:        1.0 CPU, 512MB RAM"
    echo "  • Nextcloud:   1.0 CPU, 512MB RAM"
    echo "  • Overseerr:   0.5 CPU, 512MB RAM"
    echo "  • LazyLib:     0.5 CPU, 256MB RAM"
    echo ""
    echo "🗑️  Removed:"
    echo "  • Calibre & Calibre-web (resource hogs)"
    echo ""
}

# Main menu
case "${1:-menu}" in
    "cleanup")
        cleanup_calibre
        ;;
    "apply")
        cleanup_calibre
        apply_resource_limits
        ;;
    "monitor")
        monitor_resources
        ;;
    "summary")
        show_resource_summary
        ;;
    "all")
        cleanup_calibre
        apply_resource_limits
        create_monitoring_script
        show_resource_summary
        echo ""
        echo "🎉 Optimization complete!"
        echo ""
        echo "📝 Next steps:"
        echo "  • Run './monitor-containers.sh' to watch resource usage"
        echo "  • Check that all services are working properly"
        echo "  • Monitor for a few hours to ensure stability"
        ;;
    *)
        echo "Usage: $0 [option]"
        echo ""
        echo "Options:"
        echo "  cleanup  - Remove Calibre containers only"
        echo "  apply    - Apply all resource limits and restart"
        echo "  monitor  - Show current resource usage"
        echo "  summary  - Show resource limits summary"
        echo "  all      - Do everything (recommended)"
        echo ""
        echo "Example: $0 all"
        ;;
esac
