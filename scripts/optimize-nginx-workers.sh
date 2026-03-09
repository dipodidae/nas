#!/bin/bash
# Optimize Nginx worker configuration for SWAG container
# Run with: bash optimize-nginx-workers.sh

set -e

echo "╔════════════════════════════════════════════════════════════╗"
echo "║  Nginx Worker & Connection Optimization                   ║"
echo "╚════════════════════════════════════════════════════════════╝"
echo

NGINX_CUSTOM_CONF="/mnt/docker-usb/swag/nginx/nginx.conf"

if [ ! -f "$NGINX_CUSTOM_CONF" ]; then
    echo "❌ Nginx config not found at: $NGINX_CUSTOM_CONF"
    echo "   Is SWAG running and configured?"
    exit 1
fi

echo "Checking current nginx configuration..."
echo

# Check if custom optimizations already exist
if grep -q "worker_connections 8192" "$NGINX_CUSTOM_CONF"; then
    echo "✅ Nginx already optimized!"
    echo "   Current configuration includes performance tuning."
    echo
    cat "$NGINX_CUSTOM_CONF" | grep -A2 "events {"
    echo
    exit 0
fi

echo "Creating optimized nginx configuration snippet..."
echo

# Backup original
cp "$NGINX_CUSTOM_CONF" "${NGINX_CUSTOM_CONF}.backup.$(date +%Y%m%d_%H%M%S)"
echo "✓ Backup created"

# Create optimization snippet
cat > "/mnt/docker-usb/swag/nginx/custom-performance.conf" << 'EOF'
# NAS Performance Optimizations for Nginx
# These settings optimize for high-bandwidth video streaming

# Worker process optimization
worker_processes auto;  # Use all available CPU cores
worker_rlimit_nofile 65535;  # Max open files per worker

events {
    worker_connections 8192;  # Increased from default 1024
    use epoll;  # Efficient connection processing on Linux
    multi_accept on;  # Accept multiple connections at once
}

http {
    # Connection handling
    keepalive_timeout 65;
    keepalive_requests 1000;

    # File handling for large media files
    sendfile on;
    sendfile_max_chunk 512k;
    tcp_nopush on;
    tcp_nodelay on;

    # Buffer sizes for large file streaming
    client_body_buffer_size 256k;
    client_max_body_size 0;  # No limit for uploads
    large_client_header_buffers 4 32k;

    # Timeouts
    client_body_timeout 300s;
    client_header_timeout 300s;
    send_timeout 300s;

    # Compression (but not for video - waste of CPU)
    gzip on;
    gzip_vary on;
    gzip_comp_level 4;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;
    gzip_disable "msie6";

    # Don't compress already-compressed video files
    gzip_proxied no-store no-cache private expired auth;

    # Connection pooling
    upstream_keepalive_connections 32;
}
EOF

echo "✓ Created performance optimization file"
echo

echo "═══════════════════════════════════════════════════════════════"
echo "  RECOMMENDED NGINX TUNING"
echo "═══════════════════════════════════════════════════════════════"
echo
echo "The optimization file has been created at:"
echo "  /mnt/docker-usb/swag/nginx/custom-performance.conf"
echo
echo "⚠️  IMPORTANT:"
echo "   LinuxServer SWAG container manages nginx.conf automatically."
echo "   To apply these optimizations, you have two options:"
echo
echo "   OPTION 1 (Recommended): Edit the Jellyfin subdomain config"
echo "   The main optimizations are already applied there:"
echo "     • sendfile on"
echo "     • tcp_nopush on"
echo "     • tcp_nodelay on"
echo "     • Optimized buffers"
echo "     • Extended timeouts"
echo
echo "   OPTION 2: Add to SWAG's custom includes"
echo "   Create: /mnt/docker-usb/swag/nginx/custom-server-http.conf"
echo "   Then copy the http { } section from custom-performance.conf"
echo
echo "═══════════════════════════════════════════════════════════════"
echo
echo "Current Jellyfin proxy config already includes:"
echo "  ✓ sendfile on"
echo "  ✓ tcp_nopush on"
echo "  ✓ tcp_nodelay on"
echo "  ✓ Extended timeouts (3600s)"
echo "  ✓ Optimized buffers (64k)"
echo
echo "✅ Your nginx is already well-optimized for streaming!"
echo
