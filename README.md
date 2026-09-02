# 4eva NAS

Single-host homelab media + storage stack. ~26 containers, one Docker Compose
project, one reverse proxy, one 10 TB disk.

> **Not a cluster.** There is no orchestrator, no scheduler and no failover:
> every service runs on one box (a Minisforum MS01) and "managing the cluster"
> means managing one Compose project. The word appears nowhere else in this
> repo on purpose — if a service is down, nothing else is going to pick it up.

| | |
|---|---|
| Host | Minisforum MS01, 30 GiB usable RAM, Intel QSV for transcoding |
| Kernel / Docker | 7.0.0-30-generic / Docker 29.7.2, Compose v5.5.0 |
| Media disk | `/mnt/drive`, ext4, 9.1 T (51 % used) |
| Config disk | `${CONFIG_DIRECTORY}` on the OS NVMe |
| Services | 26 (4 built locally, 16 auto-updated, 6 deliberately not) |
| Public entry | SWAG on `:80`/`:443` + wildcard TLS via Cloudflare DNS-01 |

---

## Contents

- [Quick start](#quick-start) · [Everyday operations](#everyday-operations)
- [Repository layout](#repository-layout) · [Service reference](#service-reference) · [URL map](#url-map)
- [Rules that will bite you](#rules-that-will-bite-you) · [Updating services](#updating-services)
- [Monitoring and alerting](#monitoring-and-alerting) · [Scheduled jobs](#scheduled-jobs)
- [Troubleshooting](#troubleshooting) · [Development](#development) · [Known gaps](#known-gaps)

---

## Quick start

### Already running (the normal case)

```bash
cd ~/nas
make check          # assert the invariants — do this before and after any change
docker compose ps   # what is actually up
```

### From scratch on a new host

```bash
git clone --recurse-submodules <this repo> ~/nas && cd ~/nas

cp .env.example .env && $EDITOR .env     # every variable is documented in there
                                         # REQUIRED / OPTIONAL / SCRIPTS labelled

make bootstrap      # creates nas-network and pre-chowns the two config dirs
                    # that have no root init (qui, ntfy) — skipping this makes
                    # them crash-loop on `permission denied`. ADR-0014.

make lint           # does the compose model even render?
make check          # 22 invariant assertions

docker compose up -d 4eva-rootpage      # swag depends_on it being healthy
docker compose up -d swag               # get TLS working first
docker compose up -d                    # then everything else

make install-hooks  # pre-commit hook running `make check`
```

Python tooling (needed by everything under `scripts/`):

```bash
pnpm py:venv        # creates .venv and installs scripts/requirements.txt
```

`docker compose up -d` will try to **build** the four local images, and
`ongehoord` is not buildable that way — it needs buildx with `--network=host`.
If a full `up` stalls on it, use `webapps/ongehoord/redeploy.sh` and bring the
rest up separately.

---

## Everyday operations

Prefer `make` and the `pnpm` scripts over raw commands — they encode decisions.

```bash
# status
docker compose ps                         # all services
docker compose ps --status running         # just the live ones
docker compose logs -f sonarr              # follow one
pnpm logs                                  # follow everything
make config                                # fully-resolved merged model

# lifecycle
pnpm up          |  make up                # docker compose up -d
pnpm down        |  make down              # stop + remove
pnpm restart                               # restart everything
docker compose restart sonarr              # restart one
docker compose up -d --force-recreate qui  # recreate one from current config

# validation — run BOTH after any compose edit
make lint                                  # docker compose config -q (matches CI)
make check                                 # the incident-derived assertions
```

### Restarting one service safely

Restart cadence is what the module grouping encodes, so check which file a
service lives in before you cycle it:

| Module | Safe to restart | Notes |
|---|---|---|
| `compose/media-manage.yaml` | **Freely** | Stateless HTTP apps over SQLite |
| `compose/media-serve.yaml` | Deliberately | User-visible; Jellyfin is slow to stop |
| `compose/storage.yaml` | Deliberately | Nextcloud holds live sync sessions |
| `compose/infra.yaml` | **Carefully** | `dockerproxy` is watchtower's and autoheal's only Docker route |
| `compose/media-download.yaml` | **Carefully** | See below |

Two services need real care, both for recorded reasons:

- **`qbittorrent`** takes ~6 s to stop gracefully (and 120 s if `CAP_KILL` ever
  goes missing). `stop_grace_period` is 120 s. → [ADR-0004](docs/decisions/0004-qbittorrent-cap-kill.md)
- **`slskd`** must not be restarted to fix a dropped Soulseek login. A restart
  re-collides with slsknet's ghost session and the 5000 ms login timeout never
  clears. The only cure is leaving it **down 15–30 min**, then a cold start.
  → [ADR-0009](docs/decisions/0009-slskd-healthcheck.md)

> **`autoheal` will restart an unhealthy container out from under you within
> ~30 s.** If a container you are debugging restarts unexpectedly, that is
> probably autoheal, not a crash — `docker logs autoheal` says so. To silence
> it for one service, remove its `autoheal=true` label; do **not** stop
> autoheal, because a stopped autoheal is invisible and went unnoticed for a
> month once. → [ADR-0010](docs/decisions/0010-autoheal-timeouts.md)

---

## Repository layout

One Compose **project**, many files, wired with the `include:` top-level
element. It stays one project because four `depends_on` edges cross module
boundaries and `depends_on` only resolves within a project.

```
~/nas/
├── compose.yaml                  # name:, networks:, include: — nothing else
├── compose.override.yaml         # optional local tweaks (gitignored)
├── .env / .env.example           # every variable documented in the example
├── Makefile                      # operational targets
├── compose/
│   ├── _fragments.yaml           # shared service shape, pulled in via `extends`
│   ├── infra.yaml                # swag, dockerproxy, watchtower, autoheal, ntfy
│   ├── media-download.yaml       # qbittorrent, qui, slskd, prowlarr, byparr
│   ├── media-manage.yaml         # sonarr, radarr, lidarr, bazarr, whisper,
│   │                             #   lingarr, cleanuparr, recyclarr
│   ├── media-serve.yaml          # jellyfin, jellyseerr
│   └── storage.yaml              # nextcloud
├── webapps/<app>/compose.yaml    # one per locally-built app, next to its Dockerfile
├── docs/decisions/               # 20 ADRs — the incident history
├── scripts/                      # host-side Python/Bash ops tooling (not deployed)
└── qbittorrent/custom-cont-init.d/   # LSIO init hook, bind-mounted read-only
```

Three things about this layout are load-bearing:

1. **`nas-network` is declared exactly once**, in `compose.yaml`
   (172.30.0.0/24, gateway `.1`, `enable_ip_masquerade`). Modules only
   reference it.
2. **`include` resolves each file's relative paths against its own directory**,
   which is why `webapps/*/compose.yaml` can say `build.context: .` and why
   `compose/infra.yaml` says `../nginx-cache`.
3. **Shared service shape comes from `compose/_fragments.yaml` via `extends`**,
   in a linear chain — a service extends exactly one fragment:

   ```
   svc-base            security_opt, restart, logging 10m/2
   └─ svc-hardened     + cap_drop: ALL
      └─ svc-hardened-tz   + TZ
         └─ svc-lsio-env   + PUID/PGID
            └─ svc-lsio    + CHOWN,SETUID,SETGID,DAC_OVERRIDE
               └─ svc-arr  + UMASK=022 + the /ping healthcheck cadence
   ```

   Anything that is a **deliberate per-service exception** stays local, next to
   the comment explaining it. Do not hoist an exception into a fragment.

Full reasoning, including what was rejected and why:
[ADR-0000](docs/decisions/0000-compose-layout.md).

### Where state lives

| Path | Contents |
|---|---|
| `${CONFIG_DIRECTORY}/<service>` | Per-service config + SQLite. Bind mount, not a named volume, so host tooling and backups can read it |
| `${SHARE_DIRECTORY}/` | `movies/ series/ music/ books/ downloads/ playlists/ nextcloud-data/` — **lowercase**, the compose files depend on it |
| `${SHARE_DIRECTORY}` mounted at `/data` | Single-mount view for sonarr/radarr/lidarr/bazarr. Hardlinks cannot cross a mount point → [ADR-0002](docs/decisions/0002-single-mount-data-hardlinks.md) |
| `logs/` | Cron job output (gitignored, pruned weekly) |

> `*.db` files under `${CONFIG_DIRECTORY}` are **WAL-mode SQLite**. Copying only
> the `.db` without `-wal`/`-shm` reads back stale values — a just-saved `1`
> shows as `0`. Copy all three or query the app's API instead.

---

## Service reference

`wt` = auto-updated by Watchtower. `NO` means **deliberately opted out**, never
forgotten — [ADR-0006](docs/decisions/0006-watchtower-opt-outs.md) says why for each.

### Control plane — `compose/infra.yaml`

| Service | Image | Ports | wt | Notes |
|---|---|---|---|---|
| `swag` | lscr.io/…/swag | `443`, `80` | yes | TLS + auto-proxy. Waits on `4eva-rootpage:healthy` |
| `dockerproxy` | tecnativa/docker-socket-proxy | — | **NO** | **The only container allowed to mount the Docker socket.** [ADR-0013](docs/decisions/0013-dockerproxy-sole-socket-holder.md) |
| `watchtower` | containrrr/watchtower | — | **NO** | Daily 04:00. Never self-updates |
| `autoheal` | willfarrell/autoheal | — | **NO** | Restarts unhealthy `qbittorrent`/`slskd` |
| `ntfy` | binwiederhier/ntfy | `127.0.0.1:8410` | yes | Push alerts. Needs a pre-chowned config dir |

### Download path — `compose/media-download.yaml`

| Service | Image | Ports | wt | Notes |
|---|---|---|---|---|
| `qbittorrent` | lscr.io/…/qbittorrent **pinned** | `127.0.0.1:8080`, `6881` tcp+udp | **NO** | Tag pinned, floor ≥ 5.2.2. `mem_limit 4g`. Needs `CAP_KILL` |
| `qui` | ghcr.io/autobrr/qui | `127.0.0.1:7476` | yes | UI *over* qBittorrent — no torrent engine of its own |
| `slskd` | slskd/slskd | `127.0.0.1:5030`, `50300` | yes | Soulseek. Healthcheck is Soulseek-**independent** on purpose |
| `prowlarr` | lscr.io/…/prowlarr | `127.0.0.1:9696` | yes | Indexer aggregator |
| `byparr` | ghcr.io/thephaseless/byparr | `127.0.0.1:8191` | yes | FlareSolverr-compatible CF solver |

**No VPN.** Both P2P services egress over the home IP; inbound needs `6881`
and `50300` forwarded on the router. → [ADR-0019](docs/decisions/0019-no-vpn-home-ip.md)

### Library management — `compose/media-manage.yaml`

| Service | Image | Ports | wt | Notes |
|---|---|---|---|---|
| `sonarr` | lscr.io/…/sonarr | `127.0.0.1:8989` | yes | TV |
| `radarr` | lscr.io/…/radarr | `127.0.0.1:7878` | yes | Movies |
| `lidarr` | lscr.io/…/lidarr **:nightly** | `127.0.0.1:8686` | yes | Music. `/data` in use → [ADR-0003](docs/decisions/0003-lidarr-data-mount-staged.md) |
| `bazarr` | lscr.io/…/bazarr | `127.0.0.1:6767` | yes | Subtitles + subcleaner post-processing |
| `whisper` | onerahmet/…-whisper-asr | `127.0.0.1:9000` | yes | CPU ASR, `small` model. `bazarr` depends on it |
| `lingarr` | lingarr/lingarr | `127.0.0.1:9876` | yes | Subtitle translation. Healthcheck disabled upstream |
| `cleanuparr` | ghcr.io/cleanuparr/cleanuparr | `127.0.0.1:11011` | yes | **Armed deletion engine** → [ADR-0017](docs/decisions/0017-cleanuparr-armed.md) |
| `recyclarr` | recyclarr/recyclarr:8 | — | yes | TRaSH profiles into sonarr/radarr, own cron |

### Playback and storage

| Service | Image | Ports | wt | Notes |
|---|---|---|---|---|
| `jellyfin` | lscr.io/…/jellyfin **pinned** | `8096`, `8920`, `7359/udp`, `1900/udp` | **NO** | QSV via `/dev/dri`. `mem_limit 10g` + 2 leak mitigations → [ADR-0008](docs/decisions/0008-jellyfin-memory-mitigations.md) |
| `jellyseerr` | ghcr.io/fallenbagel/jellyseerr | `127.0.0.1:5056` | yes | Requests |
| `nextcloud` | lscr.io/…/nextcloud | `127.0.0.1:8087` | yes | Whole share at `/external/*`. Log budget 25m/3 |

### Locally built — `webapps/*/compose.yaml`

All four are Watchtower-opt-out **by construction**: it cannot pull a local image.

| Service | Source | Ports | Notes |
|---|---|---|---|
| `4eva-rootpage` | `webapps/4eva-rootpage` | `127.0.0.1:8088` | Apex landing page + `/ops.html` dashboard |
| `lidarr-bulk` | `webapps/lidarr-bulk` | `127.0.0.1:3000` | Bulk Lidarr ops; AI/Spotify tabs hide when unset |
| `ongehoord` | `webapps/ongehoord` (+ nested submodule) | — | Nuxt preview, basic auth at the proxy. **buildx only** |
| `playlist-generator` | `webapps/jellyfin-playlist-generator` (submodule) | — | Nuxt + FastAPI + nginx |
| `playlist-generator-db` | pgvector/pgvector:pg16 | — | Never auto-updated: no engine bump under live data |

### Startup order

`depends_on` edges, four of which cross module files — the reason this is one project:

```
4eva-rootpage ─(healthy)→ swag ────────→ nextcloud
dockerproxy ──→ watchtower, autoheal
qbittorrent ──→ prowlarr, qui ──→ sonarr, radarr, lidarr
slskd ────────→ lidarr ─────────→ lidarr-bulk
prowlarr, whisper ──→ bazarr
sonarr, radarr ─────→ recyclarr
jellyfin, sonarr, radarr ──→ jellyseerr
sonarr, radarr, lidarr, qbittorrent(healthy) ──→ cleanuparr
playlist-generator-db ─(healthy)→ playlist-generator
```

---

## URL map

Every internal WebUI binds `127.0.0.1` only. The public surface is SWAG, plus
the P2P ports and Jellyfin's LAN ports. Enforced by `make check`.

A `swag=enable` label only publishes a subdomain if a matching
`<service>.subdomain.conf` exists — `make check` asserts that every labelled
service has one, because `lingarr` carried the label with no conf and quietly
served SWAG's default page instead. Proxy-confs this repo owns live in
`swag/proxy-confs/` and are bind-mounted read-only, so they are
version-controlled rather than backup-controlled; the rest still live only in
the gitignored SWAG config dir and `make check` warns about them by name.

| URL | Service |
|---|---|
| `4eva.me` | `4eva-rootpage` (apex, via the mounted `root.conf`) |
| `4eva.me/ops.html` | Live stack dashboard, fed by `media_ops_status.py` |
| `jellyfin.` · `jellyseerr.` | Playback and requests |
| `sonarr.` · `radarr.` · `lidarr.` · `bazarr.` · `prowlarr.` · `lingarr.` | The *arr suite |
| `qui.` | qBittorrent UI (**qBittorrent's own subdomain is disabled**) |
| `slskd.` | Soulseek daemon UI |
| `cleanuparr.` · `lidarr-bulk.` · `playlist-generator.` · `ongehoord.` · `nextcloud.` · `ntfy.` | Rest |

Intentionally public ports: `443`, `80`, `8096`, `8920`, `7359`, `1900`,
`6881`, `50300`. Anything else published on `0.0.0.0` fails `make check`.

Reaching a loopback UI from elsewhere, without exposing it:

```bash
ssh -L 8989:127.0.0.1:8989 <host>    # then http://localhost:8989
```

---

## Rules that will bite you

These are asserted mechanically by `scripts/check-invariants.sh`. Each failure
prints the ADR that explains why the rule exists — **read it before changing
the rule.** Compose lines carrying `INVARIANT:` are the same contract.

| Rule | Why | ADR |
|---|---|---|
| `qbittorrent` keeps `CAP_KILL` | s6 (root) must signal `qbittorrent-nox` (uid 1000). Without it every stop is a 120.3 s SIGKILL instead of 6.2 s | [0004](docs/decisions/0004-qbittorrent-cap-kill.md) |
| `qbittorrent` tag stays pinned, ≥ 5.2.2 | 5.2.0/5.2.1 can't prove their lockfile is stale after a recreate and refuse to start | [0005](docs/decisions/0005-qbittorrent-pinned-tag.md) |
| Watchtower is `MONITOR_ONLY` | Its recreate is not atomic; a failed remove leaves **no container at all** (13 h, then 7 days). The capability is removed, not defended | [0020](docs/decisions/0020-watchtower-replaced-and-demoted.md) |
| `memswap_limit == mem_limit` wherever `mem_limit` is set | Otherwise it balloons into host swap and thrashes everything else first | [0007](docs/decisions/0007-qbittorrent-memory-cap.md) |
| slskd's healthcheck stays Soulseek-**independent** | A login-aware healthcheck + autoheal = permanent restart spiral | [0009](docs/decisions/0009-slskd-healthcheck.md) |
| autoheal stop timeout ≥ 120 s, `CURL_TIMEOUT` > that | Otherwise restarts are cut off mid-stop and pile up three deep | [0010](docs/decisions/0010-autoheal-timeouts.md) |
| No `QBITTORRENT_USER`/`PASS` on the container | The image never read them; it only leaked them into `docker inspect` | [0011](docs/decisions/0011-qbittorrent-credentials.md) |
| Only `dockerproxy` mounts `/var/run/docker.sock` | The socket is root on the host | [0013](docs/decisions/0013-dockerproxy-sole-socket-holder.md) |
| sonarr/radarr/lidarr/**bazarr** mount `/data` | Hardlinks can't cross a mount point (cost 0.96 TiB); bazarr needs it to resolve *arr paths | [0002](docs/decisions/0002-single-mount-data-hardlinks.md), [0015](docs/decisions/0015-bazarr-no-data-mount.md) |
| Jellyfin's volume mappings are **not** changed | Owner instruction, and 3 systems are calibrated to `/data/movies` | [0016](docs/decisions/0016-jellyfin-paths-are-load-bearing.md) |
| Every service: `cap_drop: ALL`, `no-new-privileges`, capped logs, loopback UI | The hardening baseline. **No exceptions** — the last two waivers were closed with measured sets on 2026-09-02 | [0001](docs/decisions/0001-hardening-baseline.md), [0018](docs/decisions/0018-capability-gaps.md) |

Start at [`docs/decisions/README.md`](docs/decisions/README.md) for the full index.

---

## Updating services

There are two update paths on purpose, because Watchtower's recreate is not
atomic and has twice left no container at all.

### Detected, not applied (16 services)

Watchtower runs on `WATCHTOWER_SCHEDULE` (default `0 0 4 * * *`), checks the 16
labelled containers for newer images, and **reports** what it finds to ntfy. It
is `WATCHTOWER_MONITOR_ONLY=true` and never stops, removes or creates anything:
its recreate is not atomic and a failed remove leaves no container at all. That
capability is gone rather than defended against. The image is
`nickfedor/watchtower`, a maintained drop-in fork — `containrrr/watchtower` was
archived in December 2025. → [ADR-0020](docs/decisions/0020-watchtower-replaced-and-demoted.md)

Applying an update is `make pull && make up`, or one of the watched
single-service targets below.

Monitor-only still *pulls* the images it checks, and `WATCHTOWER_CLEANUP` only
removes an image after a container is restarted with it — so pulled images
accumulate until the weekly `docker image prune -f` cron (Sundays 03:00).

### Deliberate (the rest)

```bash
make pull-jellyfin          # pull + up -d + wait for healthy
make update-qbittorrent     # refuses to run until you confirm the tag was bumped
make pull                   # pull every non-local image (no recreate)
pnpm update                 # pull + up -d, everything (trips on ongehoord)
```

`make update-qbittorrent` prints the current pinned tag and the reason it is
pinned, then waits for confirmation before pulling. Both targets end by
watching for `healthy` and shout loudly if the container is **missing entirely**
— that is the ADR-0006 failure mode, and `restart: unless-stopped` cannot fix
it because there is nothing left to restart.

### Verifying after any update

```bash
pnpm verify:update   # scripts/post_update_verifier.py — also runs nightly at 04:30
make check
docker compose ps
```

### Rebuilding a local app

```bash
docker compose up -d --build lidarr-bulk
docker compose up -d --build playlist-generator
webapps/ongehoord/redeploy.sh          # needs buildx --network=host
make submodules                        # refresh ongehoord/src + playlist-generator
cd webapps/4eva-rootpage && pnpm install && pnpm run build   # dist/ is mounted into SWAG
```

---

## Monitoring and alerting

Before 2026-09-01 **nothing on this box reported failure** — Jellyfin was
OOM-killed five times in 48 h and qBittorrent sat dead for 14 h, both found by
accident. Four layers now cover it:

| Layer | What it catches | Blind to |
|---|---|---|
| Docker healthchecks | A service answering wrong | A service that no longer exists |
| `autoheal` | Unhealthy `qbittorrent`/`slskd`, restarts within ~30 s | Anything unlabelled |
| `scripts/stack_watchdog.py` (`*/5`) | A service defined in compose with **no container at all**, plus unhealthy ones | The host itself |
| `scripts/heartbeat.py` (`*/10`) → healthchecks.io | **The host being down** | — |
| `make verify-runtime` (daily 06:15) | Running containers drifting from the invariants — a missing container, a lost capability, a stray `compose.override.yaml` | Anything the config alone can prove (that is `make check`'s job) |
| `scripts/offsite_backup.sh` (daily 02:00) | Config surviving the loss of this machine | Media — 4.6 T is not backed up anywhere, by choice |

All alerts go to self-hosted `ntfy` (`nas-alerts`), published over loopback so
contents never leave the box; only the phone's subscription goes out through
SWAG. `deny-all` by default, accounts created by hand.
→ [ADR-0012](docs/decisions/0012-ntfy-alerting.md)

```bash
# what's unhealthy right now
docker compose ps --format '{{.Name}}\t{{.Status}}' | grep -iv healthy

# the watchdog's own view
. .venv/bin/activate && python scripts/stack_watchdog.py

# live dashboard
open https://4eva.me/ops.html
```

`scripts/slskd_login_watch.py` (`*/15`) watches Soulseek login state
**alert-only** — it never restarts slskd, for the reason in ADR-0009.

---

## Scheduled jobs

27 cron entries, all wrapped in `scripts/cron_job.py`, which reports failures
and staleness to ntfy — a job that stops running is itself an alert.

> **Every cron line must `cd /home/tom/nas` first.** The scripts resolve `.env`
> and `logs/` relative to the working directory.

| When | Job |
|---|---|
| `*/5` | `stack_watchdog`, `media_ops_status`, `qbittorrent_settings_enforce` |
| `2-59/5` | `lidarr_jellyfin_bridge` (Lidarr has no working path mapping) |
| `*/10` | `heartbeat` (off-box dead-man's switch) |
| `*/15` | `slskd_login_watch` |
| `5,20,35,50` | `lidarr_monitor_sweep --no-search` |
| `12,27,42,57` | `lidarr_backlog_drip` |
| `:07 :22 :37 :52` | Tubifarry/slskd unclog chain — **shares one flock**, do not run these concurrently |
| `:17` | `wan_shaper.sh apply` (scoped sudo) |
| daily | `config_backup` 01:00 · `offsite_backup` 02:00 (commented until a destination is set) · `slskd_rescan` 03:30 · `post_update_verifier` 04:30 · `process_soulseek_imports` 05:30 · `verify-runtime` 06:15 |
| every 6 h | `playlist-sync` |
| weekly | `log_pruner` · `docker prune` · `album_art` · per-library Jellyfin scans (Fri/Sat/Sun 05:05) |

```bash
crontab -l                          # the real list
tail -f logs/stack_watchdog.log     # or any logs/<job>.log
```

---

## Troubleshooting

### A service is missing entirely (no container)

This is the Watchtower failure mode. `restart: unless-stopped` cannot help.

```bash
docker compose ps -a | grep -i exit
docker compose up -d <service>
docker logs watchtower --since 24h | grep -i "failed\|did not receive"
```

### qBittorrent won't start / crash-loops

Check `CAP_KILL` first — it was the root cause of the whole recurring cycle,
not the stale lockfile that everyone kept deleting.

```bash
make check                                    # asserts KILL is present
docker exec qbittorrent capsh --print | grep -i kill
docker logs qbittorrent --tail 50             # rapidly incrementing PIDs = the s6 tight loop
make measure-qbittorrent-stop                 # baseline 6.2 s at 128 torrents
```

The init script clears a lockfile **only after proving it stale** (0-byte file,
hostname mismatch, or dead PID). Never make that an unconditional `rm -f`.

### slskd is logged out of Soulseek

**Do not restart it.**

```bash
. .venv/bin/activate && python scripts/slskd_login_watch.py   # exit 1 = out past grace
docker compose stop slskd
sleep 1200                                    # 15–30 min so slsknet reaps the ghost session
docker compose up -d slskd
```

### Imports are copying instead of hardlinking

Hardlinks cannot cross a mount point. Both sides must be under `/data`.

```bash
docker exec sonarr sh -c 'ln /data/downloads/<f> /data/series/<f>.probe && echo ok; rm -f /data/series/<f>.probe'
docker exec sonarr stat -c '%h %n' /data/series/<show>/<file>   # %h > 1 = hardlinked
```

**Both ends means both ends.** Moving the root folder into `/data` is only half
of it — the import *source* is whatever path the download client reports, and
`link()` refuses across a mount point even when `st_dev` is identical. If `%h`
is still `1`, look at the import history's `droppedPath` before anything else:

```bash
K=$(sed -n 's/^API_KEY_LIDARR=//p' .env)
curl -s -H "X-Api-Key: $K" 'http://127.0.0.1:8686/api/v1/history?pageSize=3&eventType=3' \
  | python3 -c "import sys,json;[print(r['data'].get('droppedPath')) for r in json.load(sys.stdin)['records']]"
```

A `/downloads/...` answer means it needs a remote path mapping to
`/data/downloads/`, which is what Lidarr has. Lidarr was migrated on 2026-09-02
by an **offline SQLite prefix rewrite** (`scripts/lidarr_repath_db.py`), not by
`PUT /api/v1/artist/editor` — that call wiped 150,187 `TrackFiles` rows and must
never be used for this. → [ADR-0003](docs/decisions/0003-lidarr-data-mount-staged.md)

### Jellyfin doesn't see new or deleted media

Two independent mechanisms, and both have to be right: `mapFrom`/`mapTo`
decides **where** the call goes, the `onXxx` toggles decide **whether** one is
made at all. The *arr "Test" button proves nothing — it returns `200` while
exercising an Emby API Jellyfin doesn't implement.

```bash
docker logs sonarr 2>&1 | grep -i "MediaBrowser.*Scheduling library update"
docker logs sonarr 2>&1 | grep "Library/Media/Updated"       # want 204.NoContent
docker logs jellyfin 2>&1 | grep "LibraryManager: Removing item"
```

Also check the opposite direction — **files on disk with no Jellyfin item**,
which a Jellyfin→disk sweep cannot see. A wrongly-matched series hides media
that looks perfect in Sonarr and on disk.

### Subtitles aren't appearing

Bazarr resolves *arr paths verbatim, so it needs `/data` mounted:

```bash
docker exec bazarr python3 -c "
import sqlite3,os
c=sqlite3.connect('file:/config/db/bazarr.db?mode=ro',uri=True)
r=[x[0] for x in c.execute('select path from table_episodes')]
print(sum(1 for p in r if p and not os.path.exists(p)),'of',len(r),'unresolvable')"
```

Expect `0 of N`. → [ADR-0015](docs/decisions/0015-bazarr-no-data-mount.md)

Bazarr tracks only movies that **have a file** — 24 of Radarr's 37 — so a lower
count there is correct, not a sync fault.

### Host-wide OOM kills

```bash
journalctl -k --since "24h ago" | grep -i "oom\|killed process"
docker stats --no-stream
```

Jellyfin leaks to ~22–24 GiB anon-RSS and gets killed by a **global** OOM
killer every 2–12 h, taking unrelated containers' healthchecks down as
collateral. `mem_limit 10g` contains the blast radius; it is not a fix.

### Something's wrong and you don't know what

```bash
make check                    # invariants
make lint                     # does the model still render
pnpm verify:update            # post-update verifier
pnpm audit:perms              # ownership/permissions across config + share
docker compose ps -a
tail -50 logs/stack_watchdog.log
```

---

## Development

### Conventions

`AGENTS.md` is binding — Python style, shell style, Compose rules, the env-var
contract, and the exit-code convention (`0` success / `1` partial / `2` fatal,
side effects in `main()`, pure logic elsewhere).

### Adding a service

```bash
$EDITOR compose/<the-right-module>.yaml     # group by blast radius
```

- `extends` the narrowest fitting fragment from `compose/_fragments.yaml`
- bind the WebUI to `127.0.0.1`; SWAG reaches it over `nas-network`
- add a healthcheck, and `labels: [swag=enable]` to publish it
- label it for Watchtower **unless** there's a reason not to — then say so in a
  one-line comment and add it to `WATCHTOWER_OPTOUT` in the checker
- document any new `.env` variable in `.env.example` **and** `AGENTS.md`

```bash
make lint && make check
docker compose up -d <service>
```

For a locally-built app, put `compose.yaml` next to its Dockerfile under
`webapps/<app>/` and add it to `compose.yaml`'s `include:`.
`scripts/project_service_adder.py` scaffolds this, and its output already
satisfies `make check`.

### Testing

```bash
make lint                                            # docker compose config -q
pnpm lint            | pnpm lint:fix                 # eslint
pnpm py:lint                                         # ruff check scripts
pnpm scripts:test                                    # import/env smoke harness
. .venv/bin/activate && pytest -q scripts/tests      # unit tests
```

CI (`.github/workflows/ci.yml`) gates on `docker compose config`, `pnpm lint`,
and `ruff` + the smoke harness + `pytest` across Python 3.11/3.12/3.13. Match
it locally before pushing.

### Docs

- `docs/decisions/` — ADRs. Add one when a decision constrains future change.
- `docs/*.md` — deep investigation writeups (`qbittorrent-crash-fix.md`,
  `jellyfin-playback-audit.md`, `arr-qbittorrent-pollution.md`,
  `cleanuparr-configuration.md`).
- Root-level `*-README.md`, `OPTIMIZATION-*.md`, `RADARR_NAMING_*.md`,
  `JELLYFIN-NO-TRANSCODING-*.md` are **historical**. Reference material, not
  runbooks — verify against live config before acting on them.

The pre-split `docker-compose.yml` is recoverable:
`git show a4beac9^:docker-compose.yml`.

---

## Known gaps

Honest list of things that are wrong or unfinished, all tracked:

- **Jellyfin's memory leak is not root-caused.** Three mitigations contain it;
  none fixes it. → [ADR-0008](docs/decisions/0008-jellyfin-memory-mitigations.md)
- **No update notification for `jellyfin` or `qbittorrent`.** Both are pinned
  and both are unlabelled, and Watchtower reports against the tag a container
  was started from — so a pinned tag is silent even if relabelled. Closing this
  needs a version-aware watcher (DIUN / WUD / Renovate against the compose
  files), not a Watchtower setting. → [ADR-0020](docs/decisions/0020-watchtower-replaced-and-demoted.md)
- **The off-box config backup is built but not yet pointed anywhere.**
  `scripts/offsite_backup.sh` is written, tested end to end against a local
  repository (retention proven to prune, restore verified byte-identical), and
  its cron entry is installed **commented out**. Two operator actions close it:
  `sudo apt-get install -y restic`, and a `RESTIC_REPOSITORY` in `.env`. Then
  uncomment the `#PENDING-DESTINATION` line in `crontab -e`.
- **Media is not backed up at all.** 4.6 T under `${SHARE_DIRECTORY}`, by
  choice. The off-box backup above covers **config only**.
