# ongehoord — self-hosted preview of dipodidae/ongehoord-ui-content (Nuxt 4 +
# @nuxt/content). Upstream deploys to Vercel and ships no Dockerfile, so this
# wrapper image builds the nitro `node-server` output and serves it on :3000.
# The app source lives in the nested git submodule at ./src (tracks `dev`).
#
# Build context is ./webapps/ongehoord (see docker-compose.yml), so paths here
# are relative to that dir — the submodule is COPYed in from ./src.

# ---- build stage -----------------------------------------------------------
FROM node:22-alpine AS build

# Native build deps for better-sqlite3 (@nuxt/content) + sharp (@nuxt/image).
RUN apk add --no-cache python3 make g++ libc6-compat

WORKDIR /app
# NUXT_IMAGE_PROVIDER=ipx switches @nuxt/image off the Vercel edge optimizer
# (provider: 'vercel' → /_vercel/image, which only exists on Vercel) and onto the
# in-process IPX/sharp optimizer bundled into the node-server. Without this, every
# optimized image URL 404s on this self-hosted deployment. sharp's native deps are
# installed above. The provider is read at build time in nuxt.config.ts.
ENV CI=1 \
    NODE_OPTIONS=--max-old-space-size=4096 \
    NUXT_IMAGE_PROVIDER=ipx

# corepack pins pnpm to the version in package.json's packageManager field.
RUN corepack enable

# Copy the app source (the submodule checkout).
COPY src/ ./

# Install with the committed lockfile; pnpm 10 auto-builds the native deps
# listed under onlyBuiltDependencies in pnpm-workspace.yaml.
RUN pnpm install --frozen-lockfile

# Replicate upstream's proven node-server recipe (package.json "build:e2e"):
# prepare types, build with the node-server preset, then bundle the prebuilt
# @nuxt/content SQLite DB next to the server entry so content resolves at runtime.
RUN pnpm nuxi prepare \
 && NITRO_PRESET=node-server pnpm nuxt build \
 && cp .data/content/contents.sqlite .output/server/ 2>/dev/null || true

# ---- runtime stage ---------------------------------------------------------
FROM node:22-alpine AS runtime

WORKDIR /app
ENV NODE_ENV=production \
    NITRO_HOST=0.0.0.0 \
    NITRO_PORT=3000 \
    PORT=3000

# Nitro's node-server output is fully self-contained (deps bundled in
# .output/server/node_modules), so only the build artifact is needed.
COPY --from=build /app/.output ./.output

# node:alpine ships a non-root `node` user.
USER node

EXPOSE 3000

# busybox wget (bundled in alpine) hits the Dutch landing route. Use 127.0.0.1,
# not localhost: alpine's /etc/hosts maps localhost to ::1 first and nitro binds
# IPv4 only, so an IPv6 probe would never connect.
HEALTHCHECK --interval=60s --timeout=5s --retries=3 --start-period=40s \
  CMD wget --no-verbose --tries=1 --spider http://127.0.0.1:3000/nl || exit 1

CMD ["node", ".output/server/index.mjs"]
