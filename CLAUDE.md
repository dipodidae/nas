# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

Single-host homelab NAS stack: one `docker-compose.yml` orchestrates SWAG (reverse proxy + Let's Encrypt), the \*arr suite (Sonarr/Radarr/Lidarr/Bazarr/Prowlarr), qBittorrent (with `qui` as its web UI), slskd, Jellyfin, Jellyseerr, Nextcloud, and Flaresolverr, plus a Vite landing page (`rootpage/`) and Python operations scripts (`scripts/`).

## Authoritative docs — read these first

- `AGENTS.md` — full conventions (Python style, shell style, Docker Compose rules, env var contract, exit codes). Treat as binding.
- `.github/copilot-instructions.md` — short-form version of the same rules.
- `README.md` — service table, ports, setup walkthrough.
- `scripts/README.md` — per-script flags, exit codes, and the operational workflows (backup, audit, log prune, post-update verify, qBittorrent kickstart, Prowlarr priority management).

The root-level `*-README.md` / `OPTIMIZATION-*.md` / `RADARR_NAMING_*.md` / `JELLYFIN-NO-TRANSCODING-*.md` files document one-off historical fixes and tuning work. They are reference material, not active runbooks — don't assume their advice is still current without checking the live config.

## Common commands

Most tooling is wrapped in `package.json` scripts; prefer those over raw commands.

```bash
# Stack lifecycle
pnpm up | pnpm down | pnpm restart | pnpm logs | pnpm update

# JS/TS lint (rootpage + repo)
pnpm lint              # check
pnpm lint:fix          # autofix

# Python (scripts/ — venv at .venv)
pnpm py:venv           # one-time: create .venv and install requirements
pnpm py:deps           # refresh deps in existing venv
pnpm py:lint           # ruff check scripts
pnpm scripts:test      # legacy import/env smoke harness (scripts/test_scripts.py)

# Pytest (unit tests in scripts/tests/) — no pnpm wrapper, run directly
. .venv/bin/activate && pytest -q scripts/tests
. .venv/bin/activate && pytest scripts/tests/test_backup.py::test_create_backup_success

# Compose validation (matches CI)
docker compose config > /dev/null

# Landing page (rootpage/ is its own pnpm workspace)
cd rootpage && pnpm install && pnpm run build   # outputs dist/, bind-mounted into SWAG
```

CI (`.github/workflows/ci.yml`) runs three gates: `docker compose config`, `pnpm lint`, and `ruff check scripts` + `python scripts/test_scripts.py` + `pytest -q scripts/tests` across Python 3.11/3.12/3.13. Match this locally before pushing.

## Architecture essentials

**One bridge network, one reverse proxy.** All services join `nas-network` (172.30.0.0/24). SWAG terminates TLS on `:80`/`:443` and auto-generates nginx proxy configs from container labels via the linuxserver SWAG auto-proxy mod — adding `labels: [swag=enable]` is what publishes a service on its subdomain. Internal WebUIs bind to `127.0.0.1:<port>` only; the public surface is SWAG plus the P2P ports for slskd (50300) and qBittorrent (6881) and Jellyfin's LAN ports (8096/8920/7359/1900).

**No VPN — P2P runs on the home IP.** `slskd` and `qbittorrent` are plain `nas-network` members that egress over the host's home IP directly; Lidarr/Sonarr/SWAG/qui resolve `qbittorrent:8080` / `slskd:5030` as normal service DNS. Each publishes its own ports: qBittorrent `6881/tcp+udp` (BitTorrent, default) and `127.0.0.1:8080` (WebUI); slskd `50300/tcp` (Soulseek, default) and `127.0.0.1:5030` (WebUI). Inbound P2P requires forwarding `6881` and `50300` on the router — there is no VPN remote-port-forward doing it. The gluetun WireGuard sidecar (AirVPN) that used to tunnel both services was **removed** on 2026-07-27 (`vpn-configs/` + `WIREGUARD_*`/`AIRVPN_*` in `.env` deleted with it). Historical caveat if Soulseek logins start timing out: slskd on the home IP is what got the IP soft-blocked by slsknet before — the tunnel was originally introduced for exactly that. **Prowlarr** was never tunneled and is unaffected; its CF-protected indexers still route through `byparr` (FlareSolverr-compatible) tagged `cloudflare`.

**Two persistence roots, both env-driven.** Every service config lives at `${CONFIG_DIRECTORY}/<service>`; media and downloads live under `${SHARE_DIRECTORY}` with lowercase subfolders (`movies/`, `series/`, `music/`, `downloads/`, `books/`, `nextcloud-data/`). Never hard-code paths — the compose file is intentionally portable across hosts. On this host (Minisforum MS01) `SHARE_DIRECTORY=/mnt/drive` is an ext4 mount.

**Service dependency chain (compose `depends_on`):** prowlarr ← {sonarr, radarr, lidarr, bazarr}; qbittorrent ← {sonarr, radarr, lidarr, prowlarr}; slskd ← lidarr; jellyfin + sonarr + radarr ← jellyseerr; swag ← nextcloud. Lidarr is pinned to the `:nightly` tag; everything else uses `:latest` from `lscr.io/linuxserver/*` except slskd (no LSIO image — uses `slskd/slskd:latest` running as `${PUID}:${PGID}`), flaresolverr (ghcr.io), and jellyseerr (ghcr.io).

**Hardening pattern (apply to any new service):** `security_opt: no-new-privileges:true`, `cap_drop: ALL`, selective `cap_add` (typically `CHOWN`, `SETUID`, `SETGID`, `DAC_OVERRIDE`), bind WebUI to `127.0.0.1`, include a `curl -f` or `wget --spider` healthcheck, and cap container logs (`json-file` with `max-size: 10m`, `max-file: "2"`). The header comment in `docker-compose.yml` explicitly notes Pi-era resource limits (`mem_limit`, `cpus`, `blkio_config`, `ulimit`) were removed for the MS01 host — do not reintroduce them without reason.

**Scripts are operational, not deployed.** Nothing in `scripts/` runs inside containers; they are Python/Bash utilities executed from the host venv against the live services' HTTP APIs (using `API_KEY_*` env vars from `.env`) or directly against the filesystem. They share a small contract documented in `AGENTS.md`: exit `0` success / `1` partial / `2` fatal; side effects centralized in `main()`; pure logic elsewhere for testability.

## Repo-specific gotchas

- **Do not modify Jellyfin's volume mappings.** The owner has a standing instruction (comment at `docker-compose.yml` ~line 414) — `${SHARE_DIRECTORY}:/data/movies:ro` is intentional even though it looks misnamed.
- **`.env` holds two distinct concerns.** Variables consumed by `docker-compose.yml` (paths, domain, Cloudflare token, qBittorrent/slskd creds) _and_ `API_KEY_*` tokens used only by `scripts/`. When you add a script that needs a new key, document it in both `.env.example` and `AGENTS.md`'s env list.
- **Folder name casing matters.** The compose file expects lowercase subfolders under `${SHARE_DIRECTORY}` (`movies`, `series`, `music`, `downloads`, `books`, `nextcloud-data`). The README's old `Movies/Series/Music` casing is stale — trust the compose file.
- **Auto-update path: `watchtower` + `dockerproxy`.** Watchtower runs on the schedule in `WATCHTOWER_SCHEDULE` (default `0 0 4 * * *` — daily at 04:00) and only acts on containers carrying `com.centurylinklabs.watchtower.enable=true`. It talks to Docker through `dockerproxy` (tecnativa/docker-socket-proxy) — never mount `/var/run/docker.sock` into any other service. Locally-built images (`lidarr-bulk`, `4eva-rootpage`) are deliberately unlabeled; `watchtower`/`dockerproxy` themselves are unlabeled too (Watchtower shouldn't self-update or restart its own dependency).
- **`rootpage/dist/` is bind-mounted read-only into SWAG.** Editing source under `rootpage/src/` requires a `pnpm run build` before SWAG sees the change.
- **`docker-compose.yml.backup.*`** is a historical snapshot, not an active file. Don't edit it.
- **qBittorrent crash-loop after an ungraceful kill → stale lockfiles (self-healing).** If qbit is SIGKILLed before flushing its ~6 MB `torrents.db`+WAL (e.g. a hard `docker kill`, host power loss, or a too-short stop timeout during a compose recreate/watchtower update) it leaves an orphaned `${CONFIG_DIRECTORY}/qbittorrent/qBittorrent/lockfile` (and historically a nested `qBittorrent/config/lockfile`). On next start qBittorrent's single-instance lock refuses to launch, `qbittorrent-nox` exits immediately, and s6 restarts it in a tight loop — the only tell is rapidly incrementing PIDs in the log, while the container reports "Up (unhealthy)". **This is NOT an ownership/permissions problem** (config is correctly `${PUID}:${PGID}`). (Historically this fired constantly because qbit shared gluetun's netns and every gluetun recreate force-restarted it; the VPN was removed 2026-07-27, so the trigger is now rare.) Two durable fixes remain in compose: (1) `stop_grace_period: 120s` so qbit shuts down cleanly and removes its own lockfile; (2) an LSIO custom-init script (`qbittorrent/custom-cont-init.d/01-clear-stale-lockfile.sh`, bind-mounted `:ro` to `/custom-cont-init.d`) that deletes any stale lockfile at container init — safe because qbit isn't running yet, so the container self-recovers on every restart. Manual fallback if ever needed: `docker stop qbittorrent && rm -f ${CONFIG_DIRECTORY}/qbittorrent/qBittorrent{,/config}/lockfile && docker start qbittorrent`.
- **qui is a UI *over* qBittorrent, not a replacement client.** `qui` (autobrr/qui) has no BitTorrent engine; it manages the existing qBittorrent daemon over the WebUI API at `qbittorrent:8080` and is published at `qui.4eva.me`. qBittorrent's own WebUI is retired as a *public* surface, but its `127.0.0.1:8080:8080` loopback publish (now on the qbittorrent block itself, since the VPN was removed) **must stay** — `qbittorrent_settings_enforce.py`, `qbittorrent_stalled_kickstart.py`, and `media_ops_status.py` reach it at `localhost:8080` via qbit's localhost auth-bypass. qui connects from nas-network (not loopback), so it needs the real `QBITTORRENT_USER`/`QBITTORRENT_PASS`, entered once in qui's UI when adding the instance. **Deploy gotcha:** qui is NOT a linuxserver image and has no root init to chown `/config` — it runs directly as `${PUID}:${PGID}` and writes `config.toml`/`qui.db` itself. If Docker auto-creates `${CONFIG_DIRECTORY}/qui` as root on first `up`, qui crash-loops on `permission denied`. The host dir must be owned by `${PUID}:${PGID}` before starting (`mkdir -p ${CONFIG_DIRECTORY}/qui && chown ${PUID}:${PGID} ${CONFIG_DIRECTORY}/qui`). The public route is the manually-enabled SWAG preset `qui.subdomain.conf` (container name `qui`, port 7476); qBittorrent's own `qbittorrent.subdomain.conf` was disabled (`.disabled`) to retire its public WebUI.
- **Never drive autoheal (or any auto-restart) off slskd's Soulseek login state.** slskd's web server can be up while its Soulseek login is dead. It is tempting to make the healthcheck probe `isLoggedIn` so a logged-out slskd gets restarted — that creates a permanent restart spiral. The login handshake times out after a hardcoded 5000ms when slsknet still holds a stale session for the username (a "ghost session" after a fast restart); a restart re-presents the same username and re-collides with the ghost, so it never recovers (32→64→128s backoff, container shows "Up N minutes" forever). The **only** cure is to leave slskd DOWN 15–30 min so slsknet reaps the session, then cold-start (`docker compose up -d slskd`). So slskd's healthcheck is deliberately **Soulseek-independent** (web-UI spider) and `autoheal=true` only restarts it if the web server itself dies. Login state is watched **alert-only** by `scripts/slskd_login_watch.py` (cron `*/15`), which never restarts. Don't reintroduce a login-aware healthcheck on the autoheal path.
