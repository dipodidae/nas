# qui — qBittorrent Web Front-end Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `autobrr/qui` as the modern web UI for the existing qBittorrent daemon, published at `qui.4eva.me`, and retire qBittorrent's own WebUI as a public surface — without disturbing the daemon or any of its API consumers.

**Architecture:** A new `qui` service joins `nas-network` (not gluetun's netns) and reaches the qBittorrent daemon at `qbittorrent:8080` (gluetun's alias) over the WebUI API — the same endpoint the *arr clients use. SWAG fronts it via `swag=enable` + `swag_port` labels. The qBittorrent daemon, its gluetun netns, and its loopback publish (`127.0.0.1:8080`, needed by host crons) are untouched.

**Tech Stack:** Docker Compose, `ghcr.io/autobrr/qui:latest` (Go single binary, SQLite in `/config`), SWAG auto-proxy (linuxserver docker-mod).

## Global Constraints

- `docker compose config` must pass (CI gate) — copied verbatim from spec success criteria.
- Never hard-code paths: use `${CONFIG_DIRECTORY}`, `${SHARE_DIRECTORY}`, `${PUID}`, `${PGID}`, `${TZ}`, `${PUBLIC_DOMAIN}`.
- Apply the repo hardening pattern to any new service: `no-new-privileges:true`, `cap_drop: ALL`, `127.0.0.1` WebUI bind, healthcheck, `json-file` logging capped `max-size: 10m` / `max-file: '2'`.
- qui has **no PUID/PGID**; its user is set with `user: "${PUID:-1000}:${PGID:-1000}"`.
- The `127.0.0.1:8080:8080` loopback publish on the gluetun block **must stay** (host crons depend on it).
- Watchtower label (`com.centurylinklabs.watchtower.enable=true`) belongs on qui (pullable ghcr image).
- All work on branch `feat/qui-qbittorrent-frontend`.

---

### Task 1: Add `QUI_SESSION_SECRET` to `.env.example`

**Files:**
- Modify: `/home/tom/nas/.env.example`

**Interfaces:**
- Produces: env var `QUI_SESSION_SECRET`, consumed by the compose `qui` service in Task 2 as `QUI__SESSION_SECRET`.

- [ ] **Step 1: Add the variable near the qBittorrent creds block**

Insert after line 21 (`QBITTORRENT_USER=admin`):

```
# qui — modern web UI for qBittorrent (https://github.com/autobrr/qui).
# Pin the session secret so container restarts don't invalidate logins.
# Generate one with: openssl rand -hex 32
QUI_SESSION_SECRET=change-this-to-a-random-hex-string
```

- [ ] **Step 2: Verify the file still lists the new key**

Run: `grep -n QUI_SESSION_SECRET /home/tom/nas/.env.example`
Expected: one line printed with the new key.

- [ ] **Step 3: Add `QUI_SESSION_SECRET` to the real `.env` (host-side, manual)**

`.env` is gitignored and not committed. Add the same key with a generated value:
Run: `printf 'QUI_SESSION_SECRET=%s\n' "$(openssl rand -hex 32)" >> /home/tom/nas/.env`
Then confirm: `grep -c QUI_SESSION_SECRET /home/tom/nas/.env` → expected `1`.

- [ ] **Step 4: Commit**

```bash
git add /home/tom/nas/.env.example
git commit -m "chore(qui): document QUI_SESSION_SECRET in .env.example"
```

---

### Task 2: Add the `qui` service to `docker-compose.yml`

**Files:**
- Modify: `/home/tom/nas/docker-compose.yml` (add a new service; place it right after the `qbittorrent` service block, which ends at the `logging` block around line 563, before the `jellyfin` block).

**Interfaces:**
- Consumes: `QUI_SESSION_SECRET` (Task 1); the running `qbittorrent` service alias on `nas-network`.
- Produces: container `qui` on `127.0.0.1:7476`, labeled for SWAG (`qui.${PUBLIC_DOMAIN}`) and Watchtower.

- [ ] **Step 1: Insert the service block**

Add immediately after the `qbittorrent` service's `logging:` block and before `  # NOTE: Jellyfin volume mappings...`:

```yaml
  # qui: modern web UI for qBittorrent (autobrr/qui). NOT a torrent client — it
  # has no BitTorrent engine and talks to the existing qBittorrent daemon over
  # its WebUI API at qbittorrent:8080 (gluetun's nas-network alias), exactly
  # like the *arr clients. Plain nas-network service (NOT in gluetun's netns).
  # No PUID/PGID support upstream, so the user is set directly. Config + SQLite
  # (qui.db) live in /config. Admin account and the qBittorrent instance are
  # created in the web UI on first run (qui cannot be seeded from env).
  qui:
    image: ghcr.io/autobrr/qui:latest
    container_name: qui
    user: '${PUID:-1000}:${PGID:-1000}'
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    environment:
      - TZ=${TZ:-UTC}
      - QUI__HOST=0.0.0.0
      - QUI__PORT=7476
      - QUI__SESSION_SECRET=${QUI_SESSION_SECRET}
    volumes:
      - ${CONFIG_DIRECTORY}/qui:/config
    ports:
      - '127.0.0.1:7476:7476'
    restart: unless-stopped
    depends_on:
      qbittorrent:
        condition: service_started
    networks:
      - nas-network
    labels:
      - swag=enable
      - swag_port=7476
      - swag_proto=http
      - com.centurylinklabs.watchtower.enable=true
    healthcheck:
      test: [CMD, wget, --no-verbose, --tries=1, --spider, 'http://localhost:7476/']
      interval: 60s
      timeout: 10s
      retries: 3
      start_period: 45s
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: '2'
```

- [ ] **Step 2: Validate compose syntax + interpolation**

Run: `cd /home/tom/nas && docker compose config > /dev/null && echo OK`
Expected: `OK` (no errors, no "variable is not set" warnings for `QUI_SESSION_SECRET` — Task 1 Step 3 must be done first).

- [ ] **Step 3: Confirm the service renders with the expected wiring**

Run: `cd /home/tom/nas && docker compose config | sed -n '/^  qui:/,/^  [a-z0-9]/p' | grep -E 'image:|7476|QUI__|swag_port|nas-network'`
Expected: shows `ghcr.io/autobrr/qui:latest`, the `127.0.0.1:7476:7476` mapping, the `QUI__` env vars, `swag_port`, and the network.

- [ ] **Step 4: Commit**

```bash
git add /home/tom/nas/docker-compose.yml
git commit -m "feat(qui): add qui web UI service for qBittorrent"
```

---

### Task 3: Document qui in `CLAUDE.md`

**Files:**
- Modify: `/home/tom/nas/CLAUDE.md` (repository purpose line + a new gotcha bullet).

**Interfaces:**
- Consumes: nothing.
- Produces: durable guidance for future sessions.

- [ ] **Step 1: Add qui to the repository-purpose sentence**

In the "## Repository purpose" paragraph, add qui to the service list — after "Jellyfin, Jellyseerr" insert ", qui (qBittorrent web UI)" (keep the existing sentence structure).

- [ ] **Step 2: Add a gotcha bullet**

Add to the "## Repo-specific gotchas" list:

```markdown
- **qui is a UI *over* qBittorrent, not a replacement client.** `qui` (autobrr/qui) has no BitTorrent engine; it manages the existing qBittorrent daemon over the WebUI API at `qbittorrent:8080` and is published at `qui.4eva.me`. qBittorrent's own WebUI is retired as a *public* surface, but its `127.0.0.1:8080:8080` loopback publish on the gluetun block **must stay** — `qbittorrent_settings_enforce.py`, `qbittorrent_stalled_kickstart.py`, and `media_ops_status.py` reach it at `localhost:8080` via qbit's localhost auth-bypass. qui connects from nas-network (not loopback), so it needs the real `QBITTORRENT_USER`/`QBITTORRENT_PASS`, entered once in qui's UI when adding the instance.
```

- [ ] **Step 3: Verify edits landed**

Run: `grep -n "qui" /home/tom/nas/CLAUDE.md`
Expected: at least the purpose-line mention and the gotcha bullet.

- [ ] **Step 4: Commit**

```bash
git add /home/tom/nas/CLAUDE.md
git commit -m "docs(qui): document qui in CLAUDE.md purpose + gotchas"
```

---

### Task 4: Deploy and verify (host-side, manual — user assists)

**Files:** none (runtime).

**Interfaces:**
- Consumes: the committed compose + env changes.

- [ ] **Step 1: Pull + start qui**

Run: `cd /home/tom/nas && docker compose up -d qui`
Expected: `qui` container created and started.

- [ ] **Step 2: Wait for health + confirm it listens**

Run: `docker inspect --format '{{.State.Health.Status}}' qui` (repeat until `healthy`, ~1 min)
Then: `curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:7476/`
Expected: `healthy`, and an HTTP status (200/302/307 — any non-000 proves it's serving).

- [ ] **Step 3: Confirm SWAG generated a proxy-conf and serves the subdomain**

Run: `ls ${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/ | grep -i qui` (expect a `qui`-named conf; if none appeared, restart SWAG: `docker compose restart swag`, wait, re-check).
Then: `curl -sS -o /dev/null -w '%{http_code}\n' -k https://qui.4eva.me/`
Expected: a proxy-conf file exists and the HTTPS request returns a non-000 status.
Fallback if the auto-proxy did not generate a usable conf: copy the SWAG `_app` sample (`${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/`), set `upstream_app qui;`, `upstream_port 7476;`, `upstream_proto http;`, name it `qui.subdomain.conf`, then `docker compose restart swag`.

- [ ] **Step 4: First-run setup in the browser (user)**

At `https://qui.4eva.me`: create the admin login, then **Add instance** → URL `http://qbittorrent:8080`, username `${QBITTORRENT_USER}`, password `${QBITTORRENT_PASS}`. Confirm the live torrent list loads.

- [ ] **Step 5: Confirm dependents still reach qBittorrent**

Run: `cd /home/tom/nas && . .venv/bin/activate && python scripts/media_ops_status.py 2>/dev/null | sed -n '/qBittorrent:/,/^[A-Za-z]/p'`
Expected: qBittorrent section shows reachable with a torrent count (proves the loopback path + creds still work).

---

### Task 5: Retire qBittorrent's public WebUI route (host-side, manual — user assists)

**Files:** live SWAG config only (not in repo).

**Interfaces:**
- Consumes: nothing.

- [ ] **Step 1: Look for a hand-written public proxy-conf for qbittorrent**

Run: `ls -la ${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/ | grep -i qbit`
Expected: either nothing (no public route ever existed — done, nothing to retire) or a `qbittorrent*.conf` (active, no `.sample` suffix).

- [ ] **Step 2: If an active conf exists, disable it**

Run: `mv ${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/qbittorrent.subdomain.conf{,.sample}` (adjust filename to what Step 1 found), then `docker compose restart swag`.

- [ ] **Step 3: Confirm qbittorrent's WebUI is no longer publicly served**

Run: `curl -sS -o /dev/null -w '%{http_code}\n' -k https://qbittorrent.4eva.me/`
Expected: a non-200 (404/502/connection refused) — no working public UI. (The loopback `127.0.0.1:8080` and in-network `qbittorrent:8080` remain reachable; only the public route is gone.)

- [ ] **Step 4: Final confirmation**

`qui.4eva.me` is the sole public front door; the *arr clients, cleanuparr, and the three host crons all continue to reach qBittorrent. No repo changes to commit for this task.

---

## Self-Review

**Spec coverage:**
- New `qui` service → Task 2. ✓
- No PUID/PGID → `user:` set in Task 2. ✓
- Session secret pinned → Task 1 + Task 2. ✓
- SWAG publish via labels + fallback conf → Task 2 (labels) + Task 4 Step 3 (verify/fallback). ✓
- Keep loopback publish / dependents unaffected → Global Constraints + Task 4 Step 5 (verified). ✓
- Retire public qbit route → Task 5. ✓
- First-run manual setup → Task 4 Step 4. ✓
- `.env.example` + `CLAUDE.md` docs → Task 1 + Task 3. ✓ (No root README exists; spec's README mention is satisfied by CLAUDE.md.)
- `docker compose config` green → Task 2 Step 2. ✓
- Management-only (no `/downloads` mount, no OIDC/Postgres) → honored; not added. ✓

**Placeholder scan:** No TBD/TODO; every step has concrete commands. `change-this-to-a-random-hex-string` in `.env.example` is an intentional example-value placeholder consistent with the file's existing `change-this-password` convention, replaced by a real value in Task 1 Step 3.

**Type consistency:** env var `QUI_SESSION_SECRET` (host) → `QUI__SESSION_SECRET` (container) used consistently across Tasks 1–2. Port 7476, container name `qui`, alias `qbittorrent:8080` consistent throughout.
