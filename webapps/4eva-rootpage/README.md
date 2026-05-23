# 4eva-rootpage

Minimal static landing page for the root domain `4eva.me` — a single HTML file
showing the domain name centered on the page. Replaces the heavier Vite-built
`rootpage/` once cut over.

## Stack

- nginx:1.27-alpine serving `src/index.html`
- Listens on `:8080` as the non-root `nginx` user (image default)
- `/healthz` returns `200 ok`

## Local check

```bash
docker build -t nas/4eva-rootpage:dev ./webapps/4eva-rootpage
docker run --rm -p 8080:8080 nas/4eva-rootpage:dev
# open http://localhost:8080
```

## Deploy

1. Add the service to `docker-compose.yml` (see `webapps/README.md` template).
   Map the container to `127.0.0.1:8080:8080` and label it `swag=enable`.
2. Bring it up:
   ```bash
   docker compose up -d --build 4eva-rootpage
   ```
3. Install the SWAG conf and reload nginx:
   ```bash
   cp webapps/4eva-rootpage/4eva-rootpage.subdomain.conf.sample \
      "${CONFIG_DIRECTORY:?}/swag/nginx/proxy-confs/4eva-rootpage.subdomain.conf"
   # remove the old rootpage conf so they don't both claim server_name 4eva.me
   rm -f "${CONFIG_DIRECTORY:?}/swag/nginx/proxy-confs/nginx-rootpage.conf"
   docker exec swag nginx -s reload
   ```
4. Verify:
   ```bash
   curl -fsS https://4eva.me/healthz
   curl -fsS https://4eva.me/ | head
   ```

## Edit the page

Change `src/index.html` and rebuild the image (`docker compose up -d --build
4eva-rootpage`). No build step beyond the Docker build.
