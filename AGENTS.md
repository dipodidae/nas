# Agent Guidelines for NAS Automation Project

This document provides essential guidelines for AI coding agents working in this repository.

## Project Overview

Docker Compose-based NAS solution with media management (Jellyfin, Sonarr, Radarr, Lidarr, Bazarr, Prowlarr), music collection (Lidarr + slskd/Soulseek), request management (Jellyseerr), qBittorrent downloads, Flaresolverr, Nextcloud, SWAG reverse proxy, and Python automation scripts.

## Build, Lint, and Test Commands

### JavaScript/TypeScript Linting

```bash
pnpm lint              # Run ESLint on JS/TS files
pnpm lint:fix          # Auto-fix linting issues
```

### Python Linting

```bash
pnpm py:lint           # Run ruff on scripts/ directory
# Or directly:
. .venv/bin/activate && ruff check scripts
```

### Python Testing

```bash
# Run all tests
pnpm scripts:test
# Or directly:
. .venv/bin/activate && python scripts/test_scripts.py

# Run specific test file with pytest
. .venv/bin/activate && pytest scripts/tests/test_backup.py

# Run single test function
. .venv/bin/activate && pytest scripts/tests/test_backup.py::test_create_backup_success

# Pytest with verbose output
. .venv/bin/activate && pytest -v scripts/tests/
```

### Docker Operations

```bash
pnpm up                # Start all services
pnpm down              # Stop all services
pnpm restart           # Restart services
pnpm logs              # Follow logs
pnpm update            # Pull images and restart
```

### Python Environment Setup

```bash
pnpm py:venv           # Create venv and install dependencies
pnpm py:deps           # Install/update dependencies in existing venv
```

## Code Style Guidelines

### Python Scripts (`scripts/`)

#### Import Order

1. Standard library imports (alphabetical)
2. Third-party imports (alphabetical)
3. Local/relative imports (alphabetical)
4. Blank line between each group

Example:

```python
#!/usr/bin/env python3
"""Module docstring."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from prowlarr_config import load_prowlarr_config
```

#### Type Hints

- Always use type hints for function parameters and return values
- Use modern type hint syntax: `list[str]` not `List[str]` (requires `from __future__ import annotations`)
- Use `Path` for filesystem paths, not `str`
- Use `None` for optional returns explicitly

#### Function Design

- Small, focused functions with single responsibility
- Avoid boolean flag parameters—split into separate functions instead
- Keep side effects (filesystem, network) thin and centralized in `main()`
- Core logic should be pure and testable
- Use dataclasses for structured data instead of long parameter lists

#### Naming Conventions

- Functions/variables: `snake_case`
- Constants: `UPPER_SNAKE_CASE`
- Classes: `PascalCase`
- Private helpers: `_leading_underscore`
- Favor meaningful names over abbreviations

#### Error Handling

- Catch narrow, specific exceptions where possible
- Only use broad `except Exception:` at top-level for clean exit codes
- Provide actionable error messages with context (paths, service names, counts)
- Return meaningful exit codes: 0 (success), 1 (partial/warning), 2 (fatal)

#### Documentation

- Module-level docstring explaining purpose, usage, exit codes
- Docstrings for public functions (one-liner or detailed)
- Inline comments only for non-obvious logic

#### Code Organization

```python
# 1. Shebang and module docstring
#!/usr/bin/env python3
"""Description."""

# 2. Imports
from __future__ import annotations
# ... imports ...

# 3. Constants
DEFAULT_VALUE = 42

# 4. Type definitions / dataclasses
@dataclass
class Config:
    pass

# 5. Helper functions
def _private_helper():
    pass

# 6. Public functions
def public_function():
    pass

# 7. Main entry point
def main():
    pass

if __name__ == "__main__":
    sys.exit(main())
```

### Shell Scripts

- Start with `#!/usr/bin/env bash`
- Add safety options: `set -euo pipefail` and `IFS=$'\n\t'`
- Quote all variable expansions: `"$VAR"` not `$VAR`
- Prefer arrays for argument lists
- Avoid `eval` entirely

### JavaScript/TypeScript

- Uses `@antfu/eslint-config` with formatters enabled
- Run `pnpm lint:fix` before committing

## Docker Compose Guidelines

### Adding New Services

- Join network: `nas-network`
- Include lightweight healthcheck: `curl -f http://localhost:port || exit 1`
- Use linuxserver.io images where precedent exists (justify alternatives in comment)
- Config volumes: `${CONFIG_DIRECTORY}/<service>:/config`
- Never hard-code user paths or secrets—use env vars
- Expose via SWAG: add label `swag=enable` (otherwise keep internal)
- Only labeled containers auto-update (Watchtower). Add `com.centurylinklabs.watchtower.enable=true` to opt a service into auto-updates; omit it for locally-built images or anything you want to upgrade manually.
- Explain version pins or digest usage in comments
- Security: never request privileged mode, host networking, or extra capabilities without justification
- Access Docker only through the existing `dockerproxy` service (tecnativa/docker-socket-proxy on `tcp://dockerproxy:2375`); never mount raw `/var/run/docker.sock` into any other container.

### VPN sidecar (gluetun)

- `slskd` and `qbittorrent` route through `gluetun` (AirVPN WireGuard) via `network_mode: service:gluetun`. These two services own no nas-network attachment, no `ports:`, and no SWAG label of their own — all of that lives on the `gluetun` block. Gluetun registers their names as nas-network aliases so existing inter-service references (`http://qbittorrent:8080`, `slskd:5030`) keep resolving.
- **Prowlarr is NOT tunneled** (tried; reverted): Cloudflare drops indexer fetches from AirVPN IPs with `blocked by CloudFlare Protection`. Prowlarr stays on `nas-network` directly with its own `127.0.0.1:9696:9696` mapping; CF-protected indexers (1337x, EZTV) carry the `cloudflare` tag so Prowlarr proxies their requests through `byparr` (FlareSolverr implementation at `http://byparr:8191/`). Do not re-add prowlarr to the gluetun block without re-litigating this — it will silently break those indexers.
- When adding a new inbound P2P port for one of these services: forward it via AirVPN's client area, publish it on `gluetun` (e.g. `'37022:37022/tcp'`), and add it to `FIREWALL_INPUT_PORTS` on gluetun (comma-separated). Set the listening port inside the service's own config — qbit uses `Session\Port` in `qBittorrent.conf`; slskd uses `soulseek.listen_port` in `slskd.yml`. Disable any in-app UPnP/NAT-PMP/auto-port-mapping ("PortForwardingEnabled" in qbit) — these are dangerous inside a VPN.
- WireGuard secrets live in `.env` as `WIREGUARD_PRIVATE_KEY` / `WIREGUARD_PRESHARED_KEY` / `WIREGUARD_PUBLIC_KEY` / `WIREGUARD_ENDPOINT_IP` / `WIREGUARD_ENDPOINT_PORT` / `WIREGUARD_ADDRESSES` / `WIREGUARD_MTU`. Raw `.conf` files in `vpn-configs/` are gitignored — never commit them.
- **Recreating gluetun cascades.** `network_mode: service:gluetun` resolves to a literal container ID, not a service name — so when gluetun is recreated (port change, env edit, image bump), `slskd` / `qbittorrent` keep their stale reference to the *old* gluetun container. Symptom: they appear "healthy" individually (their own loopback check works) but other containers on `nas-network` get `Connection refused` when hitting their service alias. Use `docker compose up -d --force-recreate gluetun slskd qbittorrent` (all three together) whenever you touch gluetun, or recreate the tunneled services right after.

### Operational gotchas

- **qBittorrent crash-loop after compose recreate / forced restart:** qbit leaves `lockfile` in both `${CONFIG_DIRECTORY}/qbittorrent/qBittorrent/lockfile` and `${CONFIG_DIRECTORY}/qbittorrent/qBittorrent/config/lockfile` when killed ungracefully. On next start the container enters a tight start→"termination initiated"→exit loop with no error logged. Fix: `docker stop qbittorrent && rm -f ${CONFIG_DIRECTORY}/qbittorrent/qBittorrent{,/config}/lockfile && docker start qbittorrent`. Do this *before* investigating other causes when qbit logs show rapid PID churn.

## Security & Secrets

- Never commit secrets to git
- Use environment variables from `.env` file
- If adding required env var, also update README
- Do not output real username or absolute home paths; refer to env vars

## Testing Guidelines

- Place tests in `scripts/tests/` directory
- Name test files: `test_<module>.py`
- Use pytest for running tests
- Test functions should be pure and side-effect free where possible
- Use `tmp_path` fixture for file system operations
- Import modules under test dynamically to avoid import side effects

## Environment Variables

Required in `.env`:

- `CONFIG_DIRECTORY` - Root for service configs
- `SHARE_DIRECTORY` - Root for media/data shares
- `PUID`, `PGID` - User/group IDs for containers
- `TZ` - Timezone
- `PUBLIC_DOMAIN` - Domain for SWAG
- `ADMIN_EMAIL` - Email for SWAG/Let's Encrypt
- `CLOUDFLARE_API_TOKEN` - For DNS validation
- `JELLYFIN_PUBLISHED_URL` - Public URL for Jellyfin
- `QBITTORRENT_USER`, `QBITTORRENT_PASS` - qBittorrent WebUI credentials
- `API_KEY_PROWLARR` - Prowlarr API key (used by scripts)
- `API_KEY_LIDARR` - Lidarr API key (used by scripts)
- `API_KEY_SLSKD` - slskd API key (used by scripts)
- `PLAYLIST_GENERATOR_USERNAME`, `PLAYLIST_GENERATOR_PASSWORD` - basic-auth login for the playlist-generator app
- `PLAYLIST_GENERATOR_DB_PASSWORD` - Postgres/pgvector password for playlist-generator-db
- `LASTFM_API_KEY` - Last.fm API key for playlist-generator enrichment (read-only; `LASTFM_API_SECRET` optional)
- `OPENAI_API_KEY`, `DISCOGS_TOKEN` - optional API keys for playlist-generator (app degrades gracefully without them)
- `API_KEY_JELLYFIN`, `JELLYFIN_USER_ID` - used by playlist-generator's "Push to Jellyfin" export (creates a Jellyfin playlist for that user via the Jellyfin API)

## General Development Principles

- Readability and maintainability first—optimize only after measurement
- Avoid heavy dependencies for trivial tasks; propose before adding
- Update healthchecks when adding services or changing ports
- If unsure about structural changes, propose a plan before editing
- Never alter existing code style/formatting without good reason
- Run linting and tests before committing changes

## Exit Codes (Python Scripts)

- `0` - Success
- `1` - Partial success / non-fatal issues (e.g., some services missing)
- `2` - Fatal error (including interrupts, no data, critical failures)
