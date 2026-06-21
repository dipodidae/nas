# ongehoord — branch preview deployment

Self-hosted preview of [dipodidae/ongehoord-ui-content](https://github.com/dipodidae/ongehoord-ui-content)
(Nuxt 4 + `@nuxt/content`). Upstream ships to Vercel and has **no Dockerfile**,
so this wrapper builds the nitro `node-server` output and serves it behind SWAG
at `ongehoord.${PUBLIC_DOMAIN}`, gated by HTTP basic auth at the proxy layer.

## Layout

```
webapps/ongehoord/
├── Dockerfile                       # builds ./src (the submodule) → node-server image
├── .dockerignore
├── .env                             # SWAG basic-auth creds (gitignored)
├── .env.example
├── ongehoord.subdomain.conf.sample  # SWAG proxy-conf (basic auth enabled)
├── README.md
└── src/                             # git submodule → ongehoord-ui-content (tracks `acceptance`)
```

App runtime secrets live in **`~/nas/.env.ongehoord`** (gitignored), injected into
the container via the compose `env_file:` directive. SWAG basic-auth credentials
live in **`webapps/ongehoord/.env`** and are baked into the SWAG htpasswd file —
they never reach the container (auth is at nginx, not in the app).

## Which branch is deployed

The submodule is pinned to the `acceptance` branch (a clean cut off `main`, used
for previewing work) — `.gitmodules` → `branch = acceptance`.

```bash
# Pull the latest acceptance and rebuild the preview:
git submodule update --remote webapps/ongehoord/src
docker compose up -d --build ongehoord

# Preview a DIFFERENT branch (e.g. feature/map):
git -C webapps/ongehoord/src fetch origin
git -C webapps/ongehoord/src checkout feature/map
docker compose up -d --build ongehoord
# (to make it the tracked default: `git config -f .gitmodules \
#  submodule.webapps/ongehoord/src.branch feature/map`)
```

## First-time deploy

From `~/nas`:

```bash
# 1. Build + start (reads app secrets from ~/nas/.env.ongehoord).
docker compose up -d --build ongehoord

# 2. Bake the basic-auth gate. Reads creds from this app's .env.
set -a; . webapps/ongehoord/.env; set +a
docker run --rm httpd:2.4-alpine \
  htpasswd -nbB "$BASIC_AUTH_USER" "$BASIC_AUTH_PASS" \
  > "${CONFIG_DIRECTORY:?}/swag/nginx/.htpasswd-ongehoord"

# 3. Install the proxy-conf (travels with the source; live copy in SWAG volume).
cp webapps/ongehoord/ongehoord.subdomain.conf.sample \
   "${CONFIG_DIRECTORY:?}/swag/nginx/proxy-confs/ongehoord.subdomain.conf"

# 4. Reload nginx (no SWAG restart needed for proxy-conf changes).
docker exec swag nginx -s reload

# 5. (One-off) DNS: CNAME `ongehoord` → ${PUBLIC_DOMAIN}. The wildcard cert
#    already covers it; with Cloudflare proxy on, nothing else is needed.

# 6. Verify (401 without creds, 200/301 with).
curl -s -o /dev/null -w '%{http_code}\n' "https://ongehoord.${PUBLIC_DOMAIN}/nl"
curl -s -o /dev/null -w '%{http_code}\n' -u "$BASIC_AUTH_USER:$BASIC_AUTH_PASS" \
  "https://ongehoord.${PUBLIC_DOMAIN}/nl"
```

## Rotate the preview password

Regenerate `BASIC_AUTH_PASS` in `webapps/ongehoord/.env`, then re-run deploy
steps 2 and 4.

## Notes

- **Not labelled for Watchtower** — locally built, Watchtower can't pull it.
  Update by pulling the submodule and rebuilding (see above).
- The image is fully self-contained (nitro bundles deps + the `@nuxt/content`
  SQLite DB), so no persistent volume is required.
