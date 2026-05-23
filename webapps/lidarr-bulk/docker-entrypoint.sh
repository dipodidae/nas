#!/bin/sh
# Self-healing /config ownership, plus session-key bootstrap done BEFORE node
# starts (so nuxt-auth-utils picks NUXT_SESSION_PASSWORD up from process.env
# at module-init time, not via late runtimeConfig mutation).
set -e

CONFIG_DIR=${CONFIG_DIR:-/config}
mkdir -p "$CONFIG_DIR"
chown -R node:node "$CONFIG_DIR" 2>/dev/null || true
chmod 700 "$CONFIG_DIR" 2>/dev/null || true

SESSION_KEY_FILE="$CONFIG_DIR/session.key"
if [ -z "${NUXT_SESSION_PASSWORD:-}" ]; then
  if [ ! -s "$SESSION_KEY_FILE" ]; then
    # 48 bytes of randomness, base64-encoded -> 64 chars.
    su-exec node:node sh -c "umask 077 && head -c 48 /dev/urandom | base64 -w 0 > '$SESSION_KEY_FILE'"
  fi
  NUXT_SESSION_PASSWORD="$(cat "$SESSION_KEY_FILE")"
  export NUXT_SESSION_PASSWORD
fi

exec su-exec node:node node .output/server/index.mjs
