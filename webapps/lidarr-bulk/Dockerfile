## syntax=docker/dockerfile:1.7
# Multi-stage Nuxt 4 build. Final stage runs as non-root on node:22-alpine.

ARG NODE_VERSION=22-alpine

FROM node:${NODE_VERSION} AS deps
WORKDIR /app
RUN corepack enable
COPY package.json ./
COPY pnpm-lock.yaml* ./
# --no-frozen-lockfile because the repo ships without a committed lockfile;
# CI will commit one later if you want fully reproducible builds.
RUN pnpm install --no-frozen-lockfile

FROM node:${NODE_VERSION} AS build
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN pnpm build

FROM node:${NODE_VERSION} AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NITRO_HOST=0.0.0.0 \
    NITRO_PORT=3000 \
    CONFIG_DIR=/config
# su-exec for dropping privileges from root to node in the entrypoint.
RUN apk add --no-cache su-exec
COPY --from=build /app/.output ./.output
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p /config \
 && chown -R node:node /config /app \
 && chmod +x /usr/local/bin/docker-entrypoint.sh
EXPOSE 3000
# Entrypoint runs briefly as root to chown /config, then drops to `node`.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
