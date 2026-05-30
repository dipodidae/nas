# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository purpose

Single-host homelab NAS stack: one `docker-compose.yml` orchestrates SWAG (reverse proxy + Let's Encrypt), the \*arr suite (Sonarr/Radarr/Lidarr/Bazarr/Prowlarr), qBittorrent, slskd, Jellyfin, Jellyseerr, Nextcloud, and Flaresolverr, plus a Vite landing page (`rootpage/`) and Python operations scripts (`scripts/`).

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

**One bridge network, one reverse proxy.** All services join `nas-network` (172.30.0.0/24). SWAG terminates TLS on `:80`/`:443` and auto-generates nginx proxy configs from container labels via the linuxserver SWAG auto-proxy mod — adding `labels: [swag=enable]` is what publishes a service on its subdomain. Internal WebUIs bind to `127.0.0.1:<port>` only; the public surface is SWAG plus the P2P ports for slskd (37020) and qBittorrent (37021) and Jellyfin's LAN ports (8096/8920/7359/1900).

**VPN sidecar for P2P services.** `gluetun` runs a WireGuard tunnel to AirVPN (config in `vpn-configs/`, secrets in `.env` as `WIREGUARD_*`). `slskd` and `qbittorrent` share gluetun's network namespace via `network_mode: service:gluetun` — so they have no nas-network attachment of their own; gluetun aliases their service names on nas-network so Lidarr/Sonarr/SWAG resolve `qbittorrent:8080` / `slskd:5030` unchanged. `DNS_KEEP_NAMESERVER=on` preserves Docker's embedded resolver so internal hostnames (e.g. `byparr`) still resolve from within the tunnel. `FIREWALL_INPUT_PORTS` on gluetun must list every inbound P2P port (currently `37020,37021`, matching AirVPN's remote-forwarded ports). All host port publishings for these two services live on the `gluetun` block, NOT on the service itself. **Prowlarr is deliberately NOT tunneled** — Cloudflare flags AirVPN exit IPs and rejects indexer fetches (1337x, EZTV) with `blocked by CloudFlare Protection`. Prowlarr stays on the home IP; its CF-protected indexers route through `byparr` (FlareSolverr-compatible) tagged `cloudflare`.

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
- **qBittorrent crash-loop after config/network changes → stale lockfiles.** When qbit is killed ungracefully (compose recreate during a running session, kernel kill, etc.), it leaves `${CONFIG_DIRECTORY}/qbittorrent/qBittorrent/lockfile` and `${CONFIG_DIRECTORY}/qbittorrent/qBittorrent/config/lockfile` behind. The container then enters a tight start→`termination initiated`→exit loop with no error in `qbittorrent.log` — the only tell is rapidly incrementing PIDs in the log. Fix: `docker stop qbittorrent && rm -f ${CONFIG_DIRECTORY}/qbittorrent/qBittorrent{,/config}/lockfile && docker start qbittorrent`. Always do this *after* a forced recreate of qbit, before chasing other causes.
- **Never drive autoheal (or any auto-restart) off slskd's Soulseek login state.** slskd's web server can be up while its Soulseek login is dead. It is tempting to make the healthcheck probe `isLoggedIn` so a logged-out slskd gets restarted — that creates a permanent restart spiral. The login handshake times out after a hardcoded 5000ms when slsknet still holds a stale session for the username (a "ghost session" after a fast restart or VPN blip); a restart re-presents the same username and re-collides with the ghost, so it never recovers (32→64→128s backoff, container shows "Up N minutes" forever). The **only** cure is to leave slskd DOWN 15–30 min so slsknet reaps the session, then cold-start (`docker compose up -d slskd`). So slskd's healthcheck is deliberately **Soulseek-independent** (web-UI spider) and `autoheal=true` only restarts it if the web server itself dies. Login state is watched **alert-only** by `scripts/slskd_login_watch.py` (cron `*/15`), which never restarts. Don't reintroduce a login-aware healthcheck on the autoheal path.
