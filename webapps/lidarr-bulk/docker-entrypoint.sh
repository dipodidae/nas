#!/bin/sh
# Self-healing /config ownership. The session-key bootstrap that used to live
# here is gone with the app's own login: there is no session to sign, because
# the route is gated by the one tinyauth door at SWAG (ADR-0034).
set -e

CONFIG_DIR=${CONFIG_DIR:-/config}
mkdir -p "$CONFIG_DIR"
chown -R node:node "$CONFIG_DIR" 2>/dev/null || true
chmod 700 "$CONFIG_DIR" 2>/dev/null || true

exec su-exec node:node node .output/server/index.mjs
