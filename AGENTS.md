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
- Include a lightweight healthcheck — but **run its exact command inside the container before committing it.** Three images in this stack are distroless or minimal and break the obvious probe: both Beszel images have no `/bin/sh` at all (a `CMD-SHELL` probe fails with `stat /bin/sh: no such file or directory`), and `streamystats-jobs` is a compiled binary with a shell but no `node`/`wget`/`curl`/`nc` — a `fetch()` probe left it permanently `starting` while the server logged `status=running`. A healthcheck that cannot run does not fail loudly: it reports `unhealthy` forever while the service is fine, or — worse, if the broken command happens to exit 0 — `healthy` forever while it is not. Use each image's own health subcommand where one exists (`/beszel health --url …`, `/agent health`), or read the kernel socket table in exec form (`grep -q ":0BBD" /proc/net/tcp*` — port in hex, state `0A` = LISTEN). ADR-0028, ADR-0030.
- Use linuxserver.io images where precedent exists (justify alternatives in comment)
- Config volumes: `${CONFIG_DIRECTORY}/<service>:/config`
- Never hard-code user paths or secrets—use env vars
- Expose via SWAG: add label `swag=enable` (otherwise keep internal)
- Only labeled containers auto-update (Watchtower). Add `com.centurylinklabs.watchtower.enable=true` to opt a service into auto-updates; omit it for locally-built images or anything you want to upgrade manually.
- Explain version pins or digest usage in comments
- Security: never request privileged mode, host networking, or extra capabilities without justification. **All four are now asserted** by `make check` (`no-host-namespaces`, `cap-drop-all`, `raw-cap-access`), because the pressure to add one arrives with an upstream doc attached: Beszel's documentation says its agent _must_ use `network_mode: host`, and following that would also have forced publishing the Docker API on the host, since a host-networked container cannot resolve `dockerproxy` (ADR-0013, ADR-0028). Capability grants are **measured**, not copied — `scrutiny` needs `SYS_ADMIN` alone, though upstream asks for `SYS_RAWIO` too (ADR-0023).
- Access Docker only through the existing `dockerproxy` service (tecnativa/docker-socket-proxy on `tcp://dockerproxy:2375`); never mount raw `/var/run/docker.sock` into any other container.

### P2P networking (no VPN)

- `slskd` and `qbittorrent` are plain `nas-network` members (the gluetun/AirVPN WireGuard sidecar was **removed 2026-07-27**). They egress over the host's home IP and each own their `ports:` block: qBittorrent `6881/tcp+udp` (BitTorrent, default) + `127.0.0.1:8080:8080` (WebUI); slskd `50300/tcp` (Soulseek, default) + `127.0.0.1:5030:5030` (WebUI). Inter-service references (`http://qbittorrent:8080`, `slskd:5030`) resolve via normal Docker service DNS.
- **Historical caveat:** slskd running on the home IP is exactly what got the IP soft-blocked by slsknet before — the VPN was originally introduced for that. If Soulseek logins start timing out post-removal, that's the first suspect (see the slskd login-timeout gotcha below).
- **Prowlarr was never tunneled** and stays on `nas-network` with its own `127.0.0.1:9696:9696` mapping; CF-protected indexers (1337x, EZTV) carry the `cloudflare` tag so Prowlarr proxies their requests through `byparr` (FlareSolverr implementation at `http://byparr:8191/`).
- When adding a new inbound P2P port: publish it on the service's own `ports:` block, forward it on the router, and set the listening port inside the service's own config — qbit uses `Session\Port` in `qBittorrent.conf`; slskd uses `soulseek.listen_port` in `slskd.yml`. In-app UPnP/NAT-PMP can be used now that there's no VPN, but manual router forwarding is preferred for stability.

### Operational gotchas

- **Change service settings through the API, never by editing a live container's config file.** qBittorrent and Jellyfin both rewrite their config on shutdown, so an edit made to the file of a running container is overwritten when it exits. This is recorded for Jellyfin's XML in `docs/jellyfin-playback-audit.md` and applies to every service in the stack. (Note: this rule is correct, but it was _not_ the cause of the qBittorrent upload cap reverting on 2026-09-02 — that container had not restarted and the change had been made via the API. The cause of that revert was never established, which is why `qbittorrent_settings_enforce.py` runs hourly.)
- **The kernel journal on this host retains only ~3 days** (2 boots, 40 MB, measured 2026-09-02). Anything that reads `journalctl -k` — `stack_watchdog.py`'s OOM and disk-error checks, and ADR-0008's whole evidence base — has that horizon. Do not conclude "it has never happened" from a clean `journalctl`; check whether the window even covers the period you mean.
- **Root access is `/etc/sudoers.d/nas-ops`, not a password on disk.** It grants `wan_shaper.sh apply|status|check|clear` plus read-only diagnostics (`smartctl`, `iptables -t mangle -S/-L`, `systemctl status/list-units`, `tune2fs -l`) passwordless, and nothing else. `.sudo-pwd` used to hold a plaintext sudo password in the repo root at mode 664; it is deleted. **Add to that sudoers file when automation needs a new root capability — do not reintroduce a stored password.**
- **`/mnt/drive` is one USB disk with no redundancy and no SMART, and some "backups" do not protect what they appear to.** Config backups are fine — live copies are on the NVMe, backups on the USB disk, two devices. But `backups/aac-remux-originals` sits on the same disk as the converted files, so it guards against a bad remux and not disk loss; and since the original DTS track survives byte-identical inside each converted file, it does not protect the audio either. Before calling something a backup here, check which physical device each copy is on (`df --output=source` then `lsblk -no PKNAME`).
- **Never hardcode a credential in a tracked file — this repo is PUBLIC on GitHub.** `scripts/legacy/qbittorrent-scheduler.py` carried a plaintext password from 2026-05-26 to 2026-09-02 that matched the live `PLAYLIST_GENERATOR_PASSWORD`, guarding an internet-facing service. Scrubbing the file does not undo it: git history is public, so an exposed credential must be **rotated**, not just deleted. Everything reads from `.env`, which is gitignored.
- **The media drive has no SMART and no redundancy, and `scrutiny` does not change that.** `/mnt/drive` is a single 9.1 TB USB external disk holding everything; its bridge refuses SMART under every `smartctl -d` type (sat, sat,12, sat,16, usbjmicron, usbsunplus, usbcypress, scsi — all verified 2026-09-02, from the host **and** from inside the scrutiny container with `SYS_ADMIN`+`SYS_RAWIO` granted and `/dev/sda` passed in). **`scrutiny` covers the NVMe only — a green Scrutiny dashboard is not coverage of the 4.6 TB.** The media disk's channels are all in `stack_watchdog.py`: I/O errors and USB resets from the kernel log (6 h window), an ext4 read-only remount, the mount disappearing, and — the durable one — **ext4's own superblock error counter** via `tune2fs -l`, which survives the reboot and the log rotation that hide the kernel-log version. Two traps in that check, both pinned by unit tests: `Filesystem state` must be compared for **equality** with `clean`, because `clean with errors` and `not clean with errors` both _contain_ `clean` and would pass a substring test during the exact failure it guards against; and `tune2fs` omits `FS Error count` entirely when it is zero, so **absence is the healthy state**, not unknown. **A read-only remount is the one to fear**: ext4 does it on error by default, and every \*arr import then fails while every container still reports healthy. ADR-0023.
- **When a check passes, ask whether it proves the property you care about or just the component that carries it.** This bit three times in one investigation: a `LibraryMonitor` line proved _a_ refresh but not what caused it; a cgroup match proved the container but not the process; `tc qdisc show | grep cake` proved CAKE was loaded but not that egress was still prioritised. Make the thing answer for its own behaviour — `scripts/wan_shaper.sh check` verifies qdisc _and_ rate _and_ DSCP marks, and every caller asks it rather than grepping `tc` and drawing its own conclusion. Test for writing a check: name the failure you are guarding against, then ask whether your check would still pass during it.
- **`RestartCount: 0` does not mean a container was never replaced.** The counter is per-container, so a `docker compose up -d` gives a fresh container reading zero. To tell a restart from a recreate, compare `.Created` against `.State.StartedAt` (a recreate has them close together and a new `.Id`); `docker events` only reaches back a few hours.
- **The qBittorrent upload cap has two values, chosen by the shaper's state.** 25 Mbps while CAKE is installed (pure capacity — DSCP makes torrents yield), 15 Mbps when it is not (measured unshaped at 0% loss). `qbittorrent_settings_enforce.py` asks `wan_shaper.sh check` (not `tc` directly — see the rule above) and picks; it runs `*/5`, not hourly, because it is a safety net and an hour of unmanaged uplink is an hour of 5% loss. The watchdog observes, the enforcer acts — keep those separate.
- **Torrent egress is marked DSCP CS1 so it yields automatically.** `scripts/wan_shaper.sh` puts qBittorrent and slskd in CAKE's Bulk tin: measured, they use ~26 Mbps when the link is idle and collapse to 1.76 Mbps (the 6.25% threshold) the moment any other flow appears. This replaces an earlier arithmetic budget that silently assumed one remote viewer — `RemoteClientBitrateLimit` is a per-_stream_ ceiling, not an aggregate, verified with two concurrent remote requests each offered the full 8 Mbps. **There is no VPN on this host**, so the marks go on the real packets in `mangle POSTROUTING` (before Docker's SNAT, which does not disturb DSCP). If a tunnel is ever reintroduced, the mark must move to its OUTER packets — WireGuard will not carry an inner mark.
- **P2P upload must stay well under the link's real upstream, or everything remote breaks.** This connection's upstream is **~31 Mbps** (measured 2026-09-02 at `/sys/class/net/enp88s0/statistics/tx_bytes` during a multi-stream upload, with P2P throttled — do not trust a speedtest's own number, and do not trust qBittorrent's). qBittorrent's cap had been **33.55 Mbps, i.e. 108% of the whole link**, which kept the modem queue permanently full: **5% packet loss, 127 ms latency spikes, 25 ms jitter**, versus 0%/18 ms/1.8 ms throttled. That collapses TCP throughput for every other flow and is what made remote Jellyfin playback stutter for weeks while LAN playback was fine. The cap is now 15 Mbps and is pinned in `DESIRED_PREFS` in `scripts/qbittorrent_settings_enforce.py`. **If playback is bad only when away from home, measure loss and jitter before touching the media server** — `ping -c 30 -i 0.3 1.1.1.1` while P2P runs, then again throttled.
- **One door for the whole public surface, and both directions are asserted (ADR-0034).** Thirteen browser-only routes sit behind `tinyauth` at `auth.${PUBLIC_DOMAIN}`; `jellyfin`, `nextcloud` and `ntfy` are excluded **on purpose** because they have clients that cannot follow a `302` (TV/DLNA, WebDAV sync, a phone's token subscription) — and a door on the alert channel makes a broken door a _silent_ one. The apex is public with `/ops.html` and `/ops-status.json` path-scoped. `make check` fails both when a `protect` route loses its auth include and when a `never` route gains one; `make verify-runtime` re-checks the live answers. Three things to know before touching any of it: **(1)** `auth_basic` does not stack with `auth_request`, it **preempts** it — nginx runs the basic-auth module first, so its `401` is what `error_page 401` converts and the auth subrequest is never made, locking out every valid session (measured on ongehoord). **(2)** A conf edit needs `make swag-apply`, not `nginx -s reload`: Docker binds each conf by inode, so `git checkout`/`revert`/prettier/`sed -i` detach the mount and nginx keeps serving the old file with a clean `git diff` and a passing `nginx -t`. **(3)** A `500` on a protected route means the door is jammed **shut** — tinyauth unreachable — not open; that fail-closed asymmetry is the design, and the unprotected routes keep serving through it.
- **Six ntfy lanes, and the split is load-bearing (ADR-0033).** `nas-critical` (prio 5) is the box or a user-visible service being broken; `nas-attention` (4) needs a human today, not now; `nas-media` (3) is new stuff you can actually watch; `nas-requests` (4) is Jellyseerr approvals and issues; `nas-infra` (2) is routine ops, recoveries and the 09:00 digest; `nas-updates` (1) is `diun` and nothing else. **Severity is carried by the PRIORITY; audience by the TOPIC.** Never encode severity in a topic name — a phone can mute a topic but cannot un-mute half of one, so `nas-errors`/`nas-warnings` would just become two muted topics. The `nas-` prefix is also load-bearing: it makes one wildcard ACL (`nas-*`) and one glance at the phone's subscription list sufficient.
  **Nothing publishes directly.** Every host publisher calls `scripts/notify.py` with a LANE; the router resolves the topic from `NTFY_TOPIC_<LANE>`, so no script and no compose file holds a topic string. `make check` asserts that, that every lane has a priority, and that `nas-critical` can be neither delayed by quiet hours nor swallowed by a cooldown.
  **Lidarr deliberately publishes nothing through its own import connector**: this box does hundreds of music imports a day and they would drown everything. `scripts/process_soulseek_imports.py` publishes instead, one message per artist per 6 h, because Soulseek albums never reach Lidarr's import pipeline anyway.
  **Deliberately not notified at all**: On Grab, On Rename, On Retag, On Application Update, On Test, subtitle downloads (bazarr/lingarr), recyclarr syncs, per-run cron _successes_, `qbittorrent_settings_enforce` runs that changed nothing, `media_ops_status` runs, and any `*/5` job reporting "all good". Those are the actual noise fix; the 09:00 digest reports the aggregate instead.
- **Some services cannot set an auth header; ntfy has three ways round it.** `?auth=<base64url("Basic "+base64(user:pass))>` as a query parameter (used by Jellyseerr's webhook), `user:pass@host` URL userinfo (used by Bazarr's Apprise URL and Scrutiny's shoutrrr URL), or an **ntfy access token** for a service whose notifier accepts only a token and not basic auth (Diun — `NTFY_DIUN_TOKEN`). All three verified working against this deny-all instance. Prefer the token where it is available: `ntfy token add <user>` inherits that account's ACL and is revocable on its own, so it does not force a password rotation across every other publisher that shares the account.
- **Jellyseerr's webhook `jsonPayload` must be double-encoded.** Its runtime does `JSON.parse(JSON.parse(base64decode(stored)))`, but its settings API validates and stores whatever plain JSON you send — so configuring it via the API with a raw template saves something the runtime rejects with `"[object Object]" is not valid JSON`. Send `json.dumps(template_text)`, not the template text.
- **Every crontab line must `cd /home/tom/nas` before any relative path.** Cron's `$HOME` is `/home/tom`, which has no `.venv`, so a line like `*/5 * * * * .venv/bin/python scripts/foo.py` fails instantly, produces no output, and is indistinguishable from a working job. That is not hypothetical: `media_ops_status.py` was dead this way from 2026-06-10 to 2026-09-01 and the ops dashboard served June data the whole time. `scripts/stack_watchdog.py` now lints the live crontab for exactly this (plus lines naming a script that does not exist), so the bug cannot be reintroduced silently.
- **Schedule new jobs through `scripts/cron_job.py`.** It pushes an ntfy alert on a fatal exit and records `logs/cron-state/<name>.json` so the watchdog can alert when a job stops running at all. Put it **inside** any `flock` — `flock -n` exits 1 without running anything when the lock is held, which outside the wrapper would look like a successful run. `--ok-codes` defaults to `0,1` because this repo treats 1 as "partial / reported a finding", not failure.
- **A thing that should be running can stop being there, and nothing notices.** `autoheal` was stopped on 2026-07-29 (collateral from a bare `docker compose stop`, per shell history — its own log shows it had never restarted anything, so it was not stopped for misbehaving) and stayed down for over a month. Everything it watched was healthy, so the only symptom was an absence. Two derived rules: the watchdog compares against `docker compose config --services` rather than only inspecting running containers, and it checks `autoheal` specifically. Also note `AUTOHEAL_DEFAULT_STOP_TIMEOUT` must be ≥ the longest `stop_grace_period` in the stack **and** `CURL_TIMEOUT` must exceed _that_ — autoheal's restart call blocks for the whole stop timeout, so a shorter curl timeout logs a spurious failure and re-issues the restart every interval on top of the one still in flight (verified with a probe: three overlapping requests before the first succeeded).
- **slskd's `start_period` must outlast a full forced share rescan, and a rescan happens even with a valid cache.** Measured 2026-09-02 from slskd's own log: `Scan found 194358 files ... in 6787113ms` = **113 min**. The old 90m window expired at 92% scanned, the container flipped to `unhealthy`, and `autoheal` was ~19 min from restarting it mid-scan. That restart is self-perpetuating, not merely wasteful: an interrupted scan logs `Previous share scan was marked as suspect` and the next start force-rescans **even though the on-disk cache restored fine** — so restart → suspect → rescan → restart is a loop in which slskd is never up. Now `4h`, asserted against a declared floor in `check-invariants.sh`. **Read this number off a COMPLETED run, never extrapolate** — it has been wrong three times (9 min stock, 30 min extrapolated while directories were warm in page cache, 90 min outgrown by the share). The cost is a 4h blind window, because Docker counts no failures inside `start_period`; that is covered by observation rather than action — `stack_watchdog.py --starting-max-min` alerts on a container parked in `starting` and never restarts one.
- **While slskd initializes it has NO HTTP listener at all**, so "connection reset by peer" on `:5030` is the expected state for ~2h after a cold start, not a fault. Verified by reading `/proc/net/tcp6` inside the container (empty) against a control (`lidarr` showed its `:8686` socket in the same read). Every slskd-dependent cron job therefore exited `2`, which `cron_job.py` treats as fatal — so a routine, self-resolving startup pushed priority-5 alerts with a skull at every tick. **An alerter that cries wolf during normal operation is worse than none**, because it trains you to swipe it away. `scripts/slskd_state.py` distinguishes "broken" from "still coming up" and those jobs now exit `1`, which is inside `cron_job.py`'s `--ok-codes 0,1`. Do **not** fix this by widening `--ok-codes`: that silences the real failure too. ADR-0026.
- **Labels are immutable, which limits AGENTS.md's own advice about autoheal.** Removing an `autoheal=true` label needs a container recreate — useless when the thing you are protecting is precisely a container you must not restart. Stopping `autoheal` is then the only lever, and it is acceptable _because_ `stack_watchdog.py` alerts on it every five minutes (it did, twice, and reported `[RESOLVED]` on restart). ADR-0026.
- **Bazarr's post-processing was failing on EVERY subtitle download, silently, for every show (fixed 2026-09-02).** `subcleaner` writes its default config into its own directory on first run, and `/opt/subcleaner` is bind-mounted `:ro` — so every invocation died with `OSError: [Errno 30] Read-only file system: '/opt/subcleaner/subcleaner.conf'`, Bazarr logged `ERROR (post_processing:40)` and **discarded the subtitle**. The API still answered `204`, the history still said "downloaded ... with a score of 99", and ffsubsync still reported an offset correction: every layer reported success except the one that wrote the file. The conf is now **tracked** at `bazarr/subcleaner.conf` and mounted read-only over that path (ADR-0022's reasoning — this failure is invisible, so it must not live only in the gitignored config dir). It also sets `log_dir = /config/log` **absolute**, because a relative one resolves against the read-only home; and never create that log via `docker exec`, which makes it root-owned and then Bazarr (uid 1000) fails with `Permission denied`. Full writeup: `docs/bazarr-subcleaner-fix.md`.
- **A hearing-impaired subtitle satisfies a non-HI Bazarr profile, so it never gets upgraded.** Poirot had 14 `.en.hi.srt` files while its profile (`Standard EN`) specifies `hi='False'`, and Bazarr reported **nothing missing** — so its scheduled upgrade never reconsidered them. Removing the HI files made Bazarr immediately report 15 English subtitles missing and fetch non-HI replacements. Compounding it, `days_to_upgrade_subs = 7` means the scheduled upgrade only ever revisits subtitles downloaded in the last week, so anything older is permanently frozen at whatever it got first. If subtitles are "bad" but Bazarr says nothing is missing, check the `.hi.` variants and that setting before touching providers.
- **`*arr` "Update Library" notifications return 204 and can still do nothing.** Sonarr/Radarr/Lidarr's MediaBrowser connection reports the *arr's own path (`/tv`, `/movies`, `/music`); Jellyfin's libraries live under `/data/movies/{series,movies,music}`. `POST /Library/Media/Updated` answers 204 for a path under no Jellyfin library and drops it silently, so the connection looks healthy while nothing refreshes. Sonarr and Radarr have `mapFrom`/`mapTo` fields for this and they are now set (`/tv` -> `/data/movies/series`, `/movies` -> `/data/movies/movies`). **Lidarr's connection does not expose those fields**, so `scripts/lidarr_jellyfin_bridge.py` (cron `2-59/5`) does the translation outside Lidarr. **Lidarr's root is `/data/music`, not `/music`, since the 2026-09-02 repath (ADR-0003)** — the bridge maps both, longest-first, and `make verify-runtime` asserts Lidarr's live root is one it can translate. When it cannot, the bridge exits **2** and holds its cursor rather than warning and returning 0, which is how a day of imports was lost silently: `cron_job.py`'s `--ok-codes` defaults to `0,1`, so a warning-plus-0 can never alert. Related: the *arr **Test** button returns HTTP 200 while proving nothing — it only calls the Emby-style _notify_ API (which Jellyfin does not implement, logging `Unable to send notification to Emby`) and never exercises the library-update path at all. Verify by capturing the inbound request instead: drop `.docker-config/jellyfin/logging.json` with `"Microsoft.AspNetCore.Hosting.Diagnostics": "Information"` in the `Override` block, **restart the container** (the file is not hot-reloaded), then watch for `Request starting ... POST ... /Library/Media/Updated` followed ~60s later by `LibraryMonitor: "X" ("/data/movies/...") will be refreshed`. The second line is the one that matters; the POST alone is not proof. Remove the file and restart to revert.
- **qBittorrent crash-loop after an ungraceful kill (self-healing):** if qbit is SIGKILLed before flushing `torrents.db` (hard `docker kill`, power loss, too-short stop on a recreate/update) it leaves an orphaned `lockfile` (`${CONFIG_DIRECTORY}/qbittorrent/qBittorrent/lockfile`, plus a legacy nested `qBittorrent/config/lockfile`) and the next start spins in a tight s6 start→exit loop (rapid PID churn, container "Up (unhealthy)"). Not an ownership issue — config is correctly `${PUID}:${PGID}`. Durable fixes live in `compose/media-download.yaml`: `cap_add: [KILL]` (the actual root cause — `docs/decisions/0004-qbittorrent-cap-kill.md`), `stop_grace_period: 120s` (clean shutdown) and a bind-mounted LSIO init script `qbittorrent/custom-cont-init.d/01-clear-stale-lockfile.sh` that clears stale lockfiles at container init, so qbit self-recovers on every restart. Manual fallback: `docker stop qbittorrent && rm -f ${CONFIG_DIRECTORY}/qbittorrent/qBittorrent{,/config}/lockfile && docker start qbittorrent`.

### Media / library caveats

- **Do not run Lidarr's "Rename Files" on Blue Öyster Cult (artist id 13).** `/music/Blue Öyster Cult/1977 - Spectres/` contains duplicate copies of tracks 04 and 08 that differ only in apostrophe character (`’` vs `'`). Lidarr's pending rename would move one over the other and destroy a file. Someone has to pick which copy to keep first. Found 2026-09-01 while looking for a safe rename to verify notifications with.
- **`aac_fallback_track.py` moves the default audio flag, and that is the whole point.** Jellyfin's StreamBuilder evaluates the _default_ stream, so adding a browser-safe track without flagging it default changes nothing. The cost is that a client which could handle DTS/AC3 5.1 gets stereo unless the viewer picks the surround track. **A 5.1 AAC default does not avoid this** — measured 2026-09-01 against the live StreamBuilder: a client reporting `maxChannelCount 2` (any browser on stereo output) transcodes it exactly as it would DTS; only a client reporting 6 direct-plays it. Stereo AAC as default is the robust choice, and for anything not already browser-safe the real answer is a native client (Jellyfin Media Player / Findroid / Infuse), which plays AC3, E-AC3 and DTS untouched.

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
- `API_KEY_CLEANUPARR` - Cleanuparr API key (`X-Api-Key` header on `http://127.0.0.1:11011/api/...`).
  Read from the `admin` user row in `${CONFIG_DIRECTORY}/cleanuparr/users.db`; regenerate in the UI
  under Account, or via `POST /api/account/api-key/regenerate`. See `docs/cleanuparr-configuration.md`.
- `PLAYLIST_GENERATOR_DB_PASSWORD` - Postgres/pgvector password for playlist-generator-db
- `LASTFM_API_KEY` - Last.fm API key for playlist-generator enrichment (read-only; `LASTFM_API_SECRET` optional)
- `OPENAI_API_KEY`, `DISCOGS_TOKEN` - optional API keys for playlist-generator (app degrades gracefully without them)
- `API_KEY_JELLYFIN`, `JELLYFIN_USER_ID` - used by playlist-generator's "Push to Jellyfin" export (creates a Jellyfin playlist for that user via the Jellyfin API)
- `API_KEY_JELLYFIN_ARR` - dedicated Jellyfin API key used **only** by the Sonarr/Radarr/Lidarr "Update Library" connections, so it can be revoked independently of the scripts' key
- **REQUIRED (scripts)** `NTFY_URL` - where `scripts/notify.py` publishes. Default and correct value is `http://127.0.0.1:8410`: **loopback**, so alert contents never leave the box (ADR-0012). Containers cannot use this address — they use `http://ntfy:8410`, because ntfy runs as `${PUID}:${PGID}` (ADR-0014) and a non-root process cannot bind `:80`.
- **REQUIRED (scripts)** `NTFY_TOKEN_SCRIPTS` - access token for the write-only `nas-scripts` account, granted `nas-*`. Every host publisher goes through the router with it. Read at call time, so a rotation takes effect on the next publish with no restart.
- **REQUIRED (containers)** `NTFY_TOKEN_ARR` - access token for `nas-arr`, granted write-only on `nas-media` / `nas-attention` / `nas-requests` and **nothing else** — publishing to `nas-critical` with it returns 403, asserted. It is stored inside the \*arr SQLite databases and in `${CONFIG_DIRECTORY}/ntfy/arr-token` (mode `0600`, owned `${PUID}:${PGID}`), which is bind-mounted read-only at `/run/ntfy-arr-token` into sonarr/radarr/lidarr for `scripts/arr_notify.sh`. **It must never appear in a container `environment:` block** (ADR-0011); `make check` asserts that.
- **REQUIRED (phone/browser)** `NTFY_TOKEN_PHONE` - **read-only** on `nas-*`. Cannot publish, so the credential typed into a phone and backed up to Google cannot inject a fake alert. Also what `make notify-test` reads messages back with, because a `200` on publish is not proof of delivery.
- **REQUIRED (scripts)** `NTFY_TOPIC_CRITICAL`, `NTFY_TOPIC_ATTENTION`, `NTFY_TOPIC_MEDIA`, `NTFY_TOPIC_REQUESTS`, `NTFY_TOPIC_INFRA`, `NTFY_TOPIC_UPDATES` - the lane → topic mapping, in **one** place, shared by the host router and by the containers whose native notifier needs a literal topic name (`diun`, `scrutiny`, `cleanuparr`, the \*arr Ntfy connectors). `compose/*.yaml` interpolates these, so no compose file holds a topic literal either — `make check` asserts that too. `scripts/notify.py` falls back to `nas-<lane>` when a key is unset, so a fresh clone and CI still work. See ADR-0033.
- **REQUIRED (containers)** `NTFY_SCRIPTS_USER`, `NTFY_SCRIPTS_PASSWORD` - basic auth for the one publisher whose notifier cannot set an `Authorization` header at all: `scrutiny`, whose shoutrrr URL carries credentials as userinfo. It reports SMART/disk failures, which are `nas-critical` — a lane `nas-arr` deliberately cannot reach — so it authenticates as `nas-scripts`, not as `nas-arr`.
- **REQUIRED (containers)** `NTFY_ARR_USER`, `NTFY_ARR_PASSWORD` - basic auth for `nas-arr`. Kept because the account needs a password at all; the publishers that can use a bearer token do.
- **OPTIONAL (scripts)** `NTFY_QUIET_HOURS` - override the 23:00–08:00 Europe/Amsterdam quiet window as `"23-8"`. Empty disables quiet hours entirely. Only `nas-media`, `nas-infra` and `nas-updates` are ever delayed; `nas-critical` and `nas-requests` never are.
- **REQUIRED (scripts/admin)** `NTFY_PHONE_USER`, `NTFY_PHONE_PASSWORD` - the **read-only** account (`nas-phone`) the phone and browser log in with when a session rather than a token is wanted.
- **REQUIRED (scripts/admin)** `NTFY_ADMIN_USER`, `NTFY_ADMIN_PASSWORD` - ntfy admin, for the web UI and for managing the other accounts.
- `NTFY_WEB_PUSH_PUBLIC_KEY`, `NTFY_WEB_PUSH_PRIVATE_KEY` - VAPID keypair for browser Web Push, generated once with `docker exec ntfy ntfy webpush keys`. **Regenerating them invalidates every existing browser subscription.** The public key is served to every browser at `/config.js` and is not secret; the private key is.
- `BESZEL_KEY` - the Beszel hub's **public** key, which the agent uses to authorise the hub's inbound SSH connection. Derive it with `ssh-keygen -y -f ${CONFIG_DIRECTORY}/beszel/id_ed25519` after the hub's first start. `BESZEL_ADMIN_USER` / `BESZEL_ADMIN_PASSWORD` are the hub login. There is deliberately **no** `BESZEL_TOKEN`: that belongs to the agent-dials-hub WebSocket topology, and here the hub dials the agent — `make check` rejects a credential-shaped env var the process does not read (ADR-0011). See ADR-0028.
- **REQUIRED (container, if diun is running)** `NTFY_DIUN_TOKEN` - ntfy **access token** (not a password) used by the `diun` container to publish to `nas-updates`. Its native notifier accepts a token **only**, so it can use neither the `?auth=` query trick nor URL userinfo — the third answer to the "some services cannot set an auth header" problem below. Minted on **`nas-scripts`**, not `nas-arr`, because it publishes to `nas-updates` which `nas-arr` cannot reach by construction; kept as its own token so it stays revocable alone: `docker exec ntfy ntfy token add --label "diun (update notifications)" nas-scripts`, revoke with `ntfy token remove nas-scripts <token>`. This is the one documented exception to "no ntfy token in a container `environment:` block" — it has no file-based option at all, and `make check`'s `ntfy-arr-token-is-a-file` is scoped to the \*arr token accordingly. See ADR-0024, ADR-0033.
- **REQUIRED (host, if tinyauth is running)** `TINYAUTH_USER`, `TINYAUTH_PASSWORD_HASH` - the single credential for the whole protected public surface (ADR-0034). One user on purpose: one household, no directory. `TINYAUTH_PASSWORD_HASH` is **bcrypt and contains `$`** — mint it with `docker run --rm -it ghcr.io/tinyauthapp/tinyauth:v5.1.3 user create --interactive`. Neither variable is ever referenced from a compose file: `make tinyauth-users` renders them into `secrets/tinyauth-users` (mode `0600`, gitignored) which the container mounts **`:ro`** at `/secrets/users`, so the credential never enters an `environment:` block and never reaches `docker inspect` (ADR-0011), and cannot be rewritten by the container that reads it (ADR-0033). `make check` asserts all four properties plus that the rendered file still matches `.env` byte for byte — the `$` in the hash was already re-expanded once by Make (`$2: unbound variable`), so the rendering is not assumed to be safe, it is checked.
- `NAS_HEARTBEAT_URL` - ping URL for the off-box dead-man's switch (`scripts/heartbeat.py`, healthchecks.io free tier). Unset until someone creates the check; `stack_watchdog.py` raises a standing warning while it is.
- `RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` - off-box config backup (`scripts/offsite_backup.sh`, `make backup-offsite`). For Backblaze B2 use the **S3-compatible** endpoint and S3 credentials, not the native `b2:` backend — restic's own docs recommend it, and the S3 backend only _hides_ obsolete files, so the bucket needs a "keep only the last version" lifecycle rule or `forget --prune` accrues cost forever. `RESTIC_PASSWORD_FILE` is a path to a `0600` file, **not** the passphrase: losing it makes every snapshot unreadable, so it does not belong in the same file as everything else and a copy must live off this machine. `RESTIC_REPOSITORY` is empty until a destination is chosen, and the script exits `2` rather than pretending to have a backup.

## General Development Principles

- Readability and maintainability first—optimize only after measurement
- Avoid heavy dependencies for trivial tasks; propose before adding
- Update healthchecks when adding services or changing ports
- If unsure about structural changes, propose a plan before editing
- Never alter existing code style/formatting without good reason
- Run linting and tests before committing changes

## Operational Scripts (on-demand)

Scripts in `scripts/` that run on-demand (not cron) against the live stack:

- `slskd_lidarr_nuke.py` — on-demand clean-slate: nukes the whole Lidarr queue
  (remove+blocklist+skipRedownload), wipes all slskd transfers, and sweeps the
  slskd completed folder. Acts by default; `--dry-run` to preview.
- `slskd_incomplete_sweep.py` — deletes orphaned dirs from the slskd-owned zones
  of `/downloads/incomplete` (legacy flat root + `incomplete/slskd`), gated on
  live slskd transfers + qBittorrent torrents + an age gate; never touches
  `incomplete/qbittorrent`. Acts by default; `--dry-run` to preview.
- `qbittorrent_settings_enforce.py` — enables qBittorrent Auto TMM and flips
  existing torrents to auto-managed so categories drive save paths (relocating
  out of `complete/manual/`). Acts by default; `--dry-run` to preview. Uses
  `QBITTORRENT_USER` / `QBITTORRENT_PASS` / `QBITTORRENT_HOST`.

Cron-driven media maintenance scripts (`album_art.py`, `replaygain.py`) are the
opposite default — **dry-run unless `--apply` is passed**. `album_art.py`
backfills missing `folder.jpg` album covers via sacad's `sacad_r`; it reuses
`SHARE_DIRECTORY` (music root = `$SHARE_DIRECTORY/music`) and adds no new `.env`
key. Requires `sacad` in the venv (pinned in `scripts/requirements.txt`). Runs
weekly (Sun 04:45, flock-guarded). See `scripts/README.md` for details.

## Exit Codes (Python Scripts)

- `0` - Success
- `1` - Partial success / non-fatal issues (e.g., some services missing)
- `2` - Fatal error (including interrupts, no data, critical failures)
