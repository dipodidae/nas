# Webapps

Reusable pattern for adding small webapps that live behind SWAG on this stack.
Every new app should follow the recipe here so they all look the same and the
deploy steps stay identical.

## Folder layout

Each app lives at `~/nas/webapps/<appname>/` and is fully self-contained:

```
webapps/<appname>/
├── Dockerfile                       # multi-stage, non-root final image
├── .dockerignore
├── .env.example                     # any per-app env vars (no secrets)
├── package.json                     # for Node apps
├── README.md                        # how to run locally + extend
├── <appname>.subdomain.conf.sample  # SWAG proxy-conf, copied into SWAG /config
└── src/ (or app/ + server/ for Nuxt) # application source
```

The compose `build:` context is `./webapps/<appname>`, so the app builds in its
own directory and the repo root stays clean.

## docker-compose service template

Add the service to `~/nas/docker-compose.yml` matching the existing hardening
pattern (security_opt, cap_drop, 127.0.0.1 binding, healthcheck, logging cap).
Minimal template for a Node-style app listening on `:3000`:

```yaml
<appname>:
  build:
    context: ./webapps/<appname>
  image: nas/<appname>:latest
  container_name: <appname>
  security_opt:
    - no-new-privileges:true
  cap_drop:
    - ALL
  read_only: true # optional: most node apps tolerate it
  tmpfs:
    - /tmp # if read_only is set
  environment:
    - TZ=${TZ:-UTC}
    - NITRO_HOST=0.0.0.0
    - NITRO_PORT=3000
    # app-specific env here
  volumes:
    - ${CONFIG_DIRECTORY}/<appname>:/config # only if the app needs to persist state
  ports:
    - '127.0.0.1:3000:3000' # internal only — SWAG fronts it
  restart: unless-stopped
  networks:
    - nas-network
  labels:
    - swag=enable
  healthcheck:
    test: [CMD, wget, --no-verbose, --tries=1, --spider, 'http://localhost:3000/healthz']
    interval: 60s
    timeout: 5s
    retries: 3
    start_period: 30s
  logging:
    driver: json-file
    options:
      max-size: 10m
      max-file: '2'
```

Rules to keep this consistent with the rest of the stack:

- **Always bind WebUI ports to `127.0.0.1`.** The public surface is SWAG.
- **Never request privileged mode, host networking, or extra capabilities** unless
  the app actually needs them. A plain Node listener on a high port needs none.
- **Run as a non-root user inside the container.** For Node, use `USER node` in
  the Dockerfile final stage.
- **Add a `/healthz` endpoint** the healthcheck can hit cheaply (`wget --spider`
  is preferred over `curl` because alpine ships wget without extra packages).
- **Cap container logs** with `max-size: 10m`, `max-file: "2"` — matches every
  other service.
- **Label the container `swag=enable`** so the linuxserver SWAG auto-proxy mod
  knows about it (it currently isn't enabled in this stack — see note below —
  but the label is harmless and keeps services consistent).

## SWAG subdomain conf template

The reverse proxy on this host uses linuxserver's SWAG with hand-managed
proxy-confs at `${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/`. The local style
includes `resolver.conf` (which already wires Docker's embedded DNS at
`127.0.0.11`) rather than inlining a `resolver` directive — this is what
`bazarr.subdomain.conf` and friends do, so match that style.

Drop the following into the app's source as
`<appname>.subdomain.conf.sample`, then during deploy copy it to
`${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/<appname>.subdomain.conf` (no
`.sample` suffix) and reload nginx:

```nginx
## <appname>.subdomain.conf — copy to:
##   ${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/<appname>.subdomain.conf
# make sure that your <appname> container is named <appname>
# make sure that your dns has a CNAME for <appname> pointing at PUBLIC_DOMAIN

server {
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name <appname>.*;

    include /config/nginx/ssl.conf;

    client_max_body_size 1m;   # tune per app; 0 = unlimited (linuxserver default)

    # enable for Authelia (requires authelia-location.conf in the location block)
    #include /config/nginx/authelia-server.conf;

    location / {
        # enable the next two lines for http basic auth
        #auth_basic "Restricted";
        #auth_basic_user_file /config/nginx/.htpasswd;

        # enable for Authelia (requires authelia-server.conf in the server block)
        #include /config/nginx/authelia-location.conf;

        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        set $upstream_app <appname>;
        set $upstream_port 3000;
        set $upstream_proto http;
        proxy_pass $upstream_proto://$upstream_app:$upstream_port;
    }
}
```

Notes:

- `server_name <appname>.*;` is the linuxserver convention — works with the
  wildcard cert SWAG already holds for `*.${PUBLIC_DOMAIN}`.
- The Docker DNS resolver is in `/config/nginx/resolver.conf`; don't reinvent.
- Add a second `location ~ /api { ... }` block only if your app's WebSocket /
  long-poll / API path needs different proxy options (longer timeouts, etc.).

## Deploy checklist

From `~/nas`:

```bash
# 1. Add the service block to docker-compose.yml (see template above).
# 2. Build and start it. --build picks up Dockerfile changes.
docker compose up -d --build <appname>

# 3. Install the SWAG proxy-conf. The sample lives in the app folder so it
#    travels with the source; the live copy lives in the SWAG config volume.
cp webapps/<appname>/<appname>.subdomain.conf.sample \
   "${CONFIG_DIRECTORY:?}/swag/nginx/proxy-confs/<appname>.subdomain.conf"

# 4. Reload nginx inside SWAG (no SWAG restart needed for proxy-conf changes).
docker exec swag nginx -s reload

# 5. (One-off) point DNS: create a CNAME for <appname> at PUBLIC_DOMAIN.
#    With Cloudflare proxy on, the wildcard cert already covers it.

# 6. Verify.
curl -fsS -o /dev/null -w '%{http_code}\n' "https://<appname>.${PUBLIC_DOMAIN}/healthz"
```

If the proxy-conf changes after deploy, repeat steps 3–4.

## Existing apps using this pattern

- `lidarr-bulk/` — bulk-import artists and albums into Lidarr.
