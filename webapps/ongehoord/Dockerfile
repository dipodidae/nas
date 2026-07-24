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
# max-old-space-size caps the V8 heap for the nuxt build. 4096 MB was enough
# until upstream's `acceptance` grew (more @nuxt/content routes + prerendering);
# the build now peaks past 4 GB and OOMs ("Reached heap limit"). The MS01 host
# has 32 GB, so give the build generous headroom.
ENV CI=1 \
    NODE_OPTIONS=--max-old-space-size=8192 \
    NUXT_IMAGE_PROVIDER=ipx

# corepack pins pnpm to the version in package.json's packageManager field
# (currently pnpm 11).
RUN corepack enable

# pnpm 11 config shim — upstream still ships pnpm-10-shaped config (overrides in
# package.json, onlyBuiltDependencies) that pnpm 11 no longer honours; the shim
# mirrors both into their pnpm 11 homes in pnpm-workspace.yaml. See the file
# header for the full rationale. COPYed before the source so its layer caches
# across app-source changes.
COPY pnpm11-config-shim.mjs /usr/local/bin/pnpm11-config-shim.mjs

# Copy the app source (the submodule checkout).
COPY src/ ./

# Reconcile upstream's config with pnpm 11 before installing: without this the
# frozen install aborts with ERR_PNPM_LOCKFILE_CONFIG_MISMATCH (unread
# overrides) and then ERR_PNPM_IGNORED_BUILDS (unread native-dep build
# allow-list). Operates on the in-image copy only.
RUN node /usr/local/bin/pnpm11-config-shim.mjs

# Install with the committed lockfile; the shim moved the native-dep build
# allow-list into pnpm 11's `allowBuilds`, so better-sqlite3 / sharp / esbuild
# compile during install.
RUN pnpm install --frozen-lockfile

# Replicate upstream's proven node-server recipe (package.json "build:e2e"):
# prepare types, build with the node-server preset. A failed build (e.g. a
# prerender error) MUST fail the image build — otherwise we ship an image with
# no .output/server/index.mjs that crash-loops on start.
RUN pnpm nuxi prepare \
 && NITRO_PRESET=node-server pnpm nuxt build

# Bundle the prebuilt @nuxt/content SQLite DB next to the server entry so content
# resolves at runtime. The `|| true` is scoped to ONLY this cp (the DB may be
# absent), and must not mask the build step above.
RUN cp .data/content/contents.sqlite .output/server/ 2>/dev/null || true

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
