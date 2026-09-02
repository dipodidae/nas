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

# Route build temp off /tmp. On this host /tmp is a quota'd tmpfs that fills up
# (large unrelated files exhaust the per-user quota); `docker compose build`
# then can't write its metadata file and aborts with "disk quota exceeded".
# Use a repo-local dir on the roomy ext4 root instead. It sits outside the
# build context (webapps/ongehoord) so it's not sent to the daemon.
export TMPDIR="${REPO_ROOT}/.deploy-tmp"
mkdir -p "${TMPDIR}"

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# skills-lock.json is a Claude-skills lockfile inside the app submodule whose
# `computedHash` fields get rewritten locally by skills tooling. It's pure
# churn — never a real source edit and irrelevant to the Nuxt Docker build —
# but git refuses to checkout/switch branches while it's dirty, which aborts
# the submodule steps below. Treat it as disposable: discard it before the
# git ops, and exclude it from the uncommitted-changes guard.
LOCKFILE="skills-lock.json"
if git -C "${SUBMODULE}" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "${SUBMODULE}" checkout -- "${LOCKFILE}" 2>/dev/null || true
fi

log "Ensuring submodule is initialised"
git -C "${REPO_ROOT}" submodule update --init "${SUBMODULE}"

log "Switching submodule to '${BRANCH}'"
dirty="$(git -C "${SUBMODULE}" status --porcelain | awk -v f="${LOCKFILE}" '$2 != f')"
if [ -n "${dirty}" ]; then
  echo "ERROR: ${SUBMODULE} has uncommitted changes — refusing to switch/pull." >&2
  echo "${dirty}" >&2
  exit 1
fi
git -C "${SUBMODULE}" checkout "${BRANCH}"

log "Pulling latest '${BRANCH}'"
git -C "${SUBMODULE}" pull --ff-only origin "${BRANCH}"
echo "Now at: $(git -C "${SUBMODULE}" log --oneline -1)"

log "Building image (nas/ongehoord:latest)"
# Build with buildx + the host-network entitlement instead of `docker compose
# build`. The Nuxt build fetches webfonts at build time (@nuxt/fonts resolves
# `Atkinson Hyperlegible` from fonts.googleapis.com / fonts.bunny.net). On this
# host the default BuildKit sandbox network can't egress to those CDNs — its CNI
# bridge isn't covered by the gluetun/VPN firewall's NAT rules — so the build
# dies with ETIMEDOUT during `nuxt build`. `--network=host --allow network.host`
# runs the RUN steps on the host network, which reaches the CDNs fine. `--load`
# materialises the image into the local docker image store so the compose
# `up --no-build` below can use it. Context/dockerfile/tag mirror the compose
# service definition (webapps/ongehoord/compose.yaml `ongehoord.build`).
docker buildx build --network=host --allow network.host --load \
  -t nas/ongehoord:latest \
  -f "${SCRIPT_DIR}/Dockerfile" "${SCRIPT_DIR}"

log "Recreating container"
docker compose -f "${REPO_ROOT}/compose.yaml" up -d --force-recreate --no-build ongehoord

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
