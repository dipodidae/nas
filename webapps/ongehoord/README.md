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
├── README.md
└── src/                             # git submodule → ongehoord-ui-content (tracks `acceptance`)
```

App runtime secrets live in **`~/nas/.env.ongehoord`** (gitignored), injected into
the container via the compose `env_file:` directive.

This app has **no credentials of its own**. Access is gated at nginx by the single
tinyauth door shared with every other protected route (ADR-0034); the proxy-conf
that does it is tracked at `swag/proxy-confs/ongehoord.subdomain.conf`, not here.
The per-app basic auth (`BASIC_AUTH_*` → `/config/nginx/.htpasswd-ongehoord`) and
the duplicate `ongehoord.subdomain.conf.sample` that carried it were retired on
2026-09-04 — the sample was a second source of truth for a conf ADR-0022 already
tracks, and it still shipped `auth_basic` lines.

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

# 2. Nothing to bake. There is no per-app credential: the route sits behind the
#    single tinyauth door (ADR-0034), whose conf is already tracked at
#    swag/proxy-confs/ongehoord.subdomain.conf and bind-mounted by swag. If you
#    edited that conf, apply it with a RECREATE, not a reload -- Docker binds
#    each conf by inode, so a replaced file never reaches the container:
make swag-apply

# 3. (One-off) DNS: CNAME `ongehoord` → ${PUBLIC_DOMAIN}. The wildcard cert
#    already covers it; with Cloudflare proxy on, nothing else is needed.

# 4. Verify (302 to the login page anonymously, 200 with a session).
curl -s -o /dev/null -w '%{http_code}\n' "https://ongehoord.${PUBLIC_DOMAIN}/nl"
```

## Rotate the preview password

There is no preview password any more. Rotate the one credential for the whole
public surface instead: mint a new hash, put it in `~/nas/.env` as
`TINYAUTH_PASSWORD_HASH`, then `make tinyauth-users && docker compose up -d
tinyauth`. ADR-0034.

## Notes

- **Not labelled for Watchtower** — locally built, Watchtower can't pull it.
  Update by pulling the submodule and rebuilding (see above).
- The image is fully self-contained (nitro bundles deps + the `@nuxt/content`
  SQLite DB), so no persistent volume is required.
