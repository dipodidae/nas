#!/usr/bin/with-contenv bash
# shellcheck shell=bash
#
# Self-heal qBittorrent's recurring crash-loop.
#
# Root cause: qbit shares gluetun's network namespace (network_mode:
# service:gluetun). When gluetun is recreated (compose up/update, watchtower,
# VPN reconnect) qbit is force-restarted. If it is SIGKILLed before it can
# flush torrents.db and release its single-instance lock, it leaves an orphaned
# `lockfile` on the persistent /config volume. On the next start qBittorrent's
# QLockFile sees that file and refuses to launch, so qbittorrent-nox exits
# immediately and s6 restarts it in a tight loop (the "incrementing PIDs" tell)
# while the container reports "Up (unhealthy)" forever.
#
# This init script runs as root BEFORE qbittorrent-nox starts. At that point no
# qbit process exists, so any lockfile present is definitionally stale and safe
# to delete unconditionally. This makes the container recover on its own across
# every recreate/restart instead of needing a manual stop/rm/start each time.
#
# Companion mitigation lives in docker-compose.yml: `stop_grace_period: 120s`
# gives qbit time to shut down cleanly and remove its own lockfile, so this
# script is the safety net rather than the everyday path.

shopt -s nullglob

for f in /config/qBittorrent/lockfile /config/qBittorrent/config/lockfile; do
    if [[ -e "${f}" ]]; then
        echo "[init-qbit-lockfile] removing stale lockfile: ${f}"
        rm -f "${f}"
    fi
done

echo "[init-qbit-lockfile] stale-lockfile check complete"
