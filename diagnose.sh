#!/bin/bash

# 🔍 NAS Connectivity Diagnostic Script
# Run this to check the health of your 4eva.me services

echo "🚀 NAS Connectivity Diagnostics for 4eva.me"
echo "============================================="
echo ""

echo "📊 System Status:"
echo "Current time: $(date)"
echo "Public IP: $(curl -s ifconfig.me 2>/dev/null || echo 'Unable to detect')"
echo ""

echo "🐳 Container Health:"
docker compose ps --format "table {{.Service}}\t{{.State}}\t{{.Status}}" | head -20
echo ""

echo "🔌 Port Status:"
echo "Port 80:  $(netstat -tln | grep :80 | wc -l) listeners"
echo "Port 443: $(netstat -tln | grep :443 | wc -l) listeners"
echo "Port 81:  $(netstat -tln | grep :81 | wc -l) listeners"
echo ""

echo "🌐 DNS Status:"
echo "Checking 4eva.me resolution..."
if command -v nslookup >/dev/null 2>&1; then
    nslookup 4eva.me 8.8.8.8 | grep -A 2 "Name:"
elif command -v dig >/dev/null 2>&1; then
    dig @8.8.8.8 4eva.me +short
else
    echo "DNS tools not available"
fi
echo ""

echo "🔗 Local Connectivity Tests:"
echo "Testing local HTTPS access..."

# Test root domain
echo -n "4eva.me: "
if curl -s -k -H "Host: 4eva.me" https://127.0.0.1 --connect-timeout 5 --max-time 10 >/dev/null 2>&1; then
    echo "✅ OK"
else
    echo "❌ FAIL"
fi

# Test Jellyfin
echo -n "jellyfin.4eva.me: "
if curl -s -k -H "Host: jellyfin.4eva.me" https://127.0.0.1 --connect-timeout 5 --max-time 10 >/dev/null 2>&1; then
    echo "✅ OK"
else
    echo "❌ FAIL"
fi

# Test Overseerr
echo -n "overseerr.4eva.me: "
if curl -s -k -H "Host: overseerr.4eva.me" https://127.0.0.1 --connect-timeout 5 --max-time 10 >/dev/null 2>&1; then
    echo "✅ OK"
else
    echo "❌ FAIL"
fi

echo ""

echo "📋 SWAG Health:"
if docker exec swag nginx -t 2>/dev/null; then
    echo "✅ Nginx config valid"
else
    echo "❌ Nginx config errors"
fi

echo -n "✅ SSL certificates: "
if docker exec swag test -f /config/etc/letsencrypt/live/4eva.me/fullchain.pem; then
    echo "Present"
    cert_date=$(docker exec swag openssl x509 -enddate -noout -in /config/etc/letsencrypt/live/4eva.me/fullchain.pem 2>/dev/null | cut -d= -f2)
    echo "   Expires: $cert_date"
else
    echo "Missing"
fi

echo ""

echo "🔍 Recent SWAG Logs:"
docker logs swag --tail 5 2>/dev/null | grep -E "(error|Error|ERROR|Server ready|emerg)" || echo "No recent errors or ready status"

echo ""

echo "📡 Cloudflare DDNS Status:"
docker logs cloudflare-ddns --tail 3 2>/dev/null | grep -E "(up to date|Failed|detected)" || echo "No recent DDNS activity"

echo ""

echo "💡 External Access Test:"
echo "If local tests pass but external fails, check:"
echo "1. Router port forwarding (80, 443 → $(hostname -I | awk '{print $1}'))"
echo "2. ISP CGNAT/Double NAT restrictions"
echo "3. DNS propagation (wait 5-10 minutes)"
echo "4. Cloudflare proxy settings (orange vs grey cloud)"

echo ""
echo "🎯 Quick External Test:"
echo "Try: https://4eva.me from your phone's mobile data"
echo "Or:  https://www.whatsmydns.net/#A/4eva.me"
