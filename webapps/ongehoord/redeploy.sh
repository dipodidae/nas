#!/usr/bin/env bash
#
# Redeploy the self-hosted ongehoord preview (ongehoord.4eva.me).
#
# Ensures the app submodule (webapps/ongehoord/src) is on the `acceptance`
# branch, fast-forwards it to origin, rebuilds the image, and recreates the
# container. The submodule is normally checked out at a detached HEAD (the
# commit pinned by the parent repo), so we explicitly switch to `acceptance`
# before pulling. The image build sets NUXT_IMAGE_PROVIDER=ipx (see Dockerfile)
# so @nuxt/image serves images via the in-process IPX optimizer instead of
# Vercel's edge endpoint.
#
# Usage: pnpm ongehoord:deploy   (or: bash webapps/ongehoord/redeploy.sh)

set -euo pipefail

BRANCH="acceptance"

# Resolve paths relative to this script so it works from any CWD.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # webapps/ongehoord
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"               # nas repo root
SUBMODULE="${SCRIPT_DIR}/src"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

log "Ensuring submodule is initialised"
git -C "${REPO_ROOT}" submodule update --init "${SUBMODULE}"

log "Switching submodule to '${BRANCH}'"
if [ -n "$(git -C "${SUBMODULE}" status --porcelain)" ]; then
  echo "ERROR: ${SUBMODULE} has uncommitted changes — refusing to switch/pull." >&2
  git -C "${SUBMODULE}" status --short >&2
  exit 1
fi
git -C "${SUBMODULE}" checkout "${BRANCH}"

log "Pulling latest '${BRANCH}'"
git -C "${SUBMODULE}" pull --ff-only origin "${BRANCH}"
echo "Now at: $(git -C "${SUBMODULE}" log --oneline -1)"

log "Building image (nas/ongehoord:latest)"
docker compose -f "${REPO_ROOT}/docker-compose.yml" build ongehoord

log "Recreating container"
docker compose -f "${REPO_ROOT}/docker-compose.yml" up -d --force-recreate --no-build ongehoord

log "Waiting for healthcheck"
for _ in $(seq 1 40); do
  status="$(docker inspect -f '{{.State.Health.Status}}' ongehoord 2>/dev/null || echo missing)"
  [ "${status}" = "healthy" ] && break
  sleep 3
done
echo "Container health: ${status:-unknown}"

if [ "${status:-}" = "healthy" ]; then
  log "Deployed — live at https://ongehoord.4eva.me"
else
  echo "WARNING: container is not healthy (status=${status:-unknown}). Check: docker logs ongehoord" >&2
  exit 1
fi
