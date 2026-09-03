# 4eva NAS

Single-host homelab media + storage stack. ~26 containers, one Docker Compose
project, one reverse proxy, one 10 TB disk.

> **Not a cluster.** There is no orchestrator, no scheduler and no failover:
> every service runs on one box (a Minisforum MS01) and "managing the cluster"
> means managing one Compose project. The word appears nowhere else in this
> repo on purpose — if a service is down, nothing else is going to pick it up.

|                 |                                                                 |
| --------------- | --------------------------------------------------------------- |
| Host            | Minisforum MS01, 30 GiB usable RAM, Intel QSV for transcoding   |
| Kernel / Docker | 7.0.0-30-generic / Docker 29.7.2, Compose v5.5.0                |
| Media disk      | `/mnt/drive`, ext4, 9.1 T (51 % used)                           |
| Config disk     | `${CONFIG_DIRECTORY}` on the OS NVMe (42 % worn, SMART-watched) |
| Services        | 32 (4 built locally; ALL watched by diun, 10 manual-update)     |
| Public entry    | SWAG on `:80`/`:443` + wildcard TLS via Cloudflare DNS-01       |

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
make check          # 49 invariant assertions

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

| Module                        | Safe to restart | Notes                                           |
| ----------------------------- | --------------- | ----------------------------------------------- |
| `compose/media-manage.yaml`   | **Freely**      | Stateless HTTP apps over SQLite                 |
| `compose/media-serve.yaml`    | Deliberately    | User-visible; Jellyfin is slow to stop          |
| `compose/storage.yaml`        | Deliberately    | Nextcloud holds live sync sessions              |
| `compose/infra.yaml`          | **Carefully**   | `dockerproxy` is `autoheal`'s only Docker route |
| `compose/media-download.yaml` | **Carefully**   | See below                                       |

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
│   ├── infra.yaml                # swag, dockerproxy, autoheal, diun, scrutiny, ntfy
│   ├── media-download.yaml       # qbittorrent, qui, slskd, prowlarr, byparr
│   ├── media-manage.yaml         # sonarr, radarr, lidarr, bazarr, whisper,
│   │                             #   lingarr, cleanuparr, recyclarr
│   ├── media-serve.yaml          # jellyfin, jellyseerr
│   └── storage.yaml              # nextcloud
├── webapps/<app>/compose.yaml    # one per locally-built app, next to its Dockerfile
├── docs/decisions/               # 32 ADRs — the incident history
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

| Path                                    | Contents                                                                                                                                                 |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `${CONFIG_DIRECTORY}/<service>`         | Per-service config + SQLite. Bind mount, not a named volume, so host tooling and backups can read it                                                     |
| `${SHARE_DIRECTORY}/`                   | `movies/ series/ music/ books/ downloads/ playlists/ nextcloud-data/` — **lowercase**, the compose files depend on it                                    |
| `${SHARE_DIRECTORY}` mounted at `/data` | Single-mount view for sonarr/radarr/lidarr/bazarr. Hardlinks cannot cross a mount point → [ADR-0002](docs/decisions/0002-single-mount-data-hardlinks.md) |
| `logs/`                                 | Cron job output (gitignored, pruned weekly)                                                                                                              |

> `*.db` files under `${CONFIG_DIRECTORY}` are **WAL-mode SQLite**. Copying only
> the `.db` without `-wal`/`-shm` reads back stale values — a just-saved `1`
> shows as `0`. Copy all three or query the app's API instead.

---

## Service reference

`wt` = **update notification** only; nothing here auto-updates any more
([ADR-0025](docs/decisions/0025-watchtower-retired.md)). Every image is watched
by `diun`; `NO` in this column marks a service that must be updated **by hand**,
listed with its reason in `MANUAL_UPDATE_ONLY` in the checker.

### Control plane — `compose/infra.yaml`

| Service        | Image                         | Ports            | wt     | Notes                                                                                                                        |
| -------------- | ----------------------------- | ---------------- | ------ | ---------------------------------------------------------------------------------------------------------------------------- |
| `swag`         | lscr.io/…/swag                | `443`, `80`      | yes    | TLS + auto-proxy. Waits on `4eva-rootpage:healthy`                                                                           |
| `dockerproxy`  | tecnativa/docker-socket-proxy | —                | **NO** | **The only container allowed to mount the Docker socket.** [ADR-0013](docs/decisions/0013-dockerproxy-sole-socket-holder.md) |
| `autoheal`     | willfarrell/autoheal          | —                | **NO** | Restarts unhealthy `qbittorrent`/`slskd`                                                                                     |
| `ntfy`         | binwiederhier/ntfy            | `127.0.0.1:8410` | yes    | Push alerts. Needs a pre-chowned config dir                                                                                  |
| `beszel`       | henrygd/beszel                | `127.0.0.1:8090` | **NO** | Trend lines: CPU/RAM/disk/per-container history [ADR-0028](docs/decisions/0028-beszel-trend-lines.md)                        |
| `beszel-agent` | henrygd/beszel-agent          | —                | **NO** | Collector. **No host networking** — see the ADR before "fixing" that                                                         |
| `diun`         | crazymax/diun                 | —                | **NO** | Update **notification** only; no Docker API access at all [ADR-0024](docs/decisions/0024-diun-version-aware-notification.md) |
| `scrutiny`     | ghcr.io/analogj/scrutiny      | `127.0.0.1:8086` | **NO** | SMART. Only holder of `SYS_ADMIN` + a raw disk. Covers the **NVMe only** [ADR-0023](docs/decisions/0023-smart-monitoring.md) |

### Download path — `compose/media-download.yaml`

| Service       | Image                            | Ports                            | wt     | Notes                                                        |
| ------------- | -------------------------------- | -------------------------------- | ------ | ------------------------------------------------------------ |
| `qbittorrent` | lscr.io/…/qbittorrent **pinned** | `127.0.0.1:8080`, `6881` tcp+udp | **NO** | Tag pinned, floor ≥ 5.2.2. `mem_limit 4g`. Needs `CAP_KILL`  |
| `qui`         | ghcr.io/autobrr/qui              | `127.0.0.1:7476`                 | yes    | UI _over_ qBittorrent — no torrent engine of its own         |
| `slskd`       | slskd/slskd                      | `127.0.0.1:5030`, `50300`        | yes    | Soulseek. Healthcheck is Soulseek-**independent** on purpose |
| `prowlarr`    | lscr.io/…/prowlarr               | `127.0.0.1:9696`                 | yes    | Indexer aggregator                                           |
| `byparr`      | ghcr.io/thephaseless/byparr      | `127.0.0.1:8191`                 | yes    | FlareSolverr-compatible CF solver                            |

**No VPN.** Both P2P services egress over the home IP; inbound needs `6881`
and `50300` forwarded on the router. → [ADR-0019](docs/decisions/0019-no-vpn-home-ip.md)

### Library management — `compose/media-manage.yaml`

| Service      | Image                         | Ports             | wt  | Notes                                                                               |
| ------------ | ----------------------------- | ----------------- | --- | ----------------------------------------------------------------------------------- |
| `sonarr`     | lscr.io/…/sonarr              | `127.0.0.1:8989`  | yes | TV                                                                                  |
| `radarr`     | lscr.io/…/radarr              | `127.0.0.1:7878`  | yes | Movies                                                                              |
| `lidarr`     | lscr.io/…/lidarr **:nightly** | `127.0.0.1:8686`  | yes | Music. `/data` in use → [ADR-0003](docs/decisions/0003-lidarr-data-mount-staged.md) |
| `bazarr`     | lscr.io/…/bazarr              | `127.0.0.1:6767`  | yes | Subtitles + subcleaner post-processing                                              |
| `whisper`    | onerahmet/…-whisper-asr       | `127.0.0.1:9000`  | yes | CPU ASR, `small` model. `bazarr` depends on it                                      |
| `lingarr`    | lingarr/lingarr               | `127.0.0.1:9876`  | yes | Subtitle translation. Healthcheck disabled upstream                                 |
| `cleanuparr` | ghcr.io/cleanuparr/cleanuparr | `127.0.0.1:11011` | yes | **Armed deletion engine** → [ADR-0017](docs/decisions/0017-cleanuparr-armed.md)     |
| `recyclarr`  | recyclarr/recyclarr:8         | —                 | yes | TRaSH profiles into sonarr/radarr, own cron                                         |

### Playback and storage

| Service      | Image                          | Ports                                  | wt     | Notes                                                                                                                     |
| ------------ | ------------------------------ | -------------------------------------- | ------ | ------------------------------------------------------------------------------------------------------------------------- |
| `jellyfin`   | lscr.io/…/jellyfin **pinned**  | `8096`, `8920`, `7359/udp`, `1900/udp` | **NO** | QSV via `/dev/dri`. `mem_limit 10g` + 2 leak mitigations → [ADR-0008](docs/decisions/0008-jellyfin-memory-mitigations.md) |
| `jellyseerr` | ghcr.io/fallenbagel/jellyseerr | `127.0.0.1:5056`                       | yes    | Requests                                                                                                                  |
| `nextcloud`  | lscr.io/…/nextcloud            | `127.0.0.1:8087`                       | yes    | Whole share at `/external/*`. Log budget 25m/3                                                                            |

### Locally built — `webapps/*/compose.yaml`

All four are excluded from update notification **by derivation**: a locally-built
image has no registry to watch, so `diun`'s manifest emitter skips them.

| Service                 | Source                                            | Ports            | Notes                                                  |
| ----------------------- | ------------------------------------------------- | ---------------- | ------------------------------------------------------ |
| `4eva-rootpage`         | `webapps/4eva-rootpage`                           | `127.0.0.1:8088` | Apex landing page + `/ops.html` dashboard              |
| `lidarr-bulk`           | `webapps/lidarr-bulk`                             | `127.0.0.1:3000` | Bulk Lidarr ops; AI/Spotify tabs hide when unset       |
| `ongehoord`             | `webapps/ongehoord` (+ nested submodule)          | —                | Nuxt preview, basic auth at the proxy. **buildx only** |
| `playlist-generator`    | `webapps/jellyfin-playlist-generator` (submodule) | —                | Nuxt + FastAPI + nginx                                 |
| `playlist-generator-db` | pgvector/pgvector:pg16                            | —                | Never auto-updated: no engine bump under live data     |

### Startup order

`depends_on` edges, four of which cross module files — the reason this is one project:

```
4eva-rootpage ─(healthy)→ swag ────────→ nextcloud
dockerproxy ──→ autoheal   (its only client — ADR-0025)
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

**What publishes a subdomain is the conf, not the label.** The linuxserver
auto-proxy mod is not installed here, so `swag=enable` is documentation and
`swag/proxy-confs/<service>.subdomain.conf` is the mechanism. Both had drifted:
`lingarr` had the label and no conf (and quietly served SWAG's default page,
answering `200`); `slskd` had a conf and no label, a public route the compose
file never declared. `make check` now asserts both directions.

All 16 confs are tracked in `swag/proxy-confs/` and bind-mounted read-only —
nine of them existed nowhere else, six being hand-written with no upstream
sample. → [ADR-0022](docs/decisions/0022-proxy-confs-are-tracked.md)

| URL                                                                                            | Service                                                      |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------ |
| `4eva.me`                                                                                      | `4eva-rootpage` (apex, via the mounted `root.conf`)          |
| `4eva.me/ops.html`                                                                             | Live stack dashboard, fed by `media_ops_status.py`           |
| `jellyfin.` · `jellyseerr.`                                                                    | Playback and requests                                        |
| `sonarr.` · `radarr.` · `lidarr.` · `bazarr.` · `prowlarr.` · `lingarr.`                       | The \*arr suite                                              |
| `qui.`                                                                                         | qBittorrent UI (**qBittorrent's own subdomain is disabled**) |
| `slskd.`                                                                                       | Soulseek daemon UI                                           |
| `cleanuparr.` · `lidarr-bulk.` · `playlist-generator.` · `ongehoord.` · `nextcloud.` · `ntfy.` | Rest                                                         |

Intentionally public ports: `443`, `80`, `8096`, `8920`, `7359`, `1900`,
`6881`, `50300`. Anything else published on `0.0.0.0` fails `make check`.

Reaching a loopback UI from elsewhere, without exposing it:

```bash
ssh -L 8989:127.0.0.1:8989 <host>    # then http://localhost:8989
```

---

## Rules that will bite you

These are asserted mechanically by `scripts/check-invariants.sh` — **49
assertions** over 32 services, run by `make check`, by the pre-commit hook, and
by CI so a violation cannot merge. Each failure prints the ADR that explains why
the rule exists — **read it before changing the rule.** Compose lines carrying
`INVARIANT:` are the same contract.

About a third of these degrade to warnings without the live host (no `.env`, no
containers, no qBittorrent API). `make verify-runtime` is the other half: it
asserts the _running_ containers match, on a daily cron, and covers what config
alone cannot prove.

| Rule                                                                                                                                                                                                              | Why                                                                                                                                                                                                                                                                                   | ADR                                                                                                                |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| `diun/manifest.yml` covers every image-pulling service, and matches the compose model                                                                                                                             | Diun watches what the manifest lists and nothing else, so a service added without regenerating it silently loses update notification. Drift in a _notification_ config is the worst kind: the thing that would have told you is the thing that stopped. `make diun-manifest`          | [0024](docs/decisions/0024-diun-version-aware-notification.md)                                                     |
| Only `scrutiny` holds `CAP_SYS_ADMIN` or a raw disk device, `:r`, on `/dev/nvme0` (the **controller**, not the `nvme0n1` namespace — `smartctl --scan` returns empty for a namespace and the UI still looks fine) | Measured, not copied: `SYS_ADMIN` alone reads the NVMe, `SYS_RAWIO` alone cannot, and both together are no better. So `SYS_RAWIO` is refused, as `FOWNER`/`FSETID` were on qBittorrent                                                                                                | [0023](docs/decisions/0023-smart-monitoring.md)                                                                    |
| No two services publish the same host port                                                                                                                                                                        | `docker compose config` renders a collision without complaint; only `up` fails, as a bind error on whichever container starts second — which reads as "that service is broken". Scrutiny's upstream example publishes host 8080, which `qbittorrent` owns and three scripts depend on | [0023](docs/decisions/0023-smart-monitoring.md)                                                                    |
| `qbittorrent` keeps `CAP_KILL`, and nothing wider                                                                                                                                                                 | s6 (root) must signal `qbittorrent-nox` (uid 1000). Without it every stop is a 120.3 s SIGKILL instead of 6.2 s. `FOWNER`/`FSETID` were verified _not_ needed — do not widen the grant                                                                                                | [0004](docs/decisions/0004-qbittorrent-cap-kill.md)                                                                |
| An nginx whose master and workers differ in uid needs `CAP_KILL`                                                                                                                                                  | `kill()` across a uid boundary needs the capability; **root is not enough**. Without it `nginx -s reload` leaks stale-config workers and a graceful stop ends in SIGKILL. Covers `swag` and `playlist-generator`                                                                      | [0021](docs/decisions/0021-nginx-cap-kill.md)                                                                      |
| `qbittorrent` tag stays pinned, ≥ 5.2.2                                                                                                                                                                           | 5.2.0/5.2.1 can't prove their lockfile is stale after a recreate and refuse to start                                                                                                                                                                                                  | [0005](docs/decisions/0005-qbittorrent-pinned-tag.md)                                                              |
| `jellyfin` tag stays pinned too                                                                                                                                                                                   | A Jellyfin regression is discovered mid-playback. An update must be _chosen_. `diun` reports when there is one to choose, which is what the pin used to cost                                                                                                                          | [0006](docs/decisions/0006-watchtower-opt-outs.md), [0024](docs/decisions/0024-diun-version-aware-notification.md) |
| No service carries a `com.centurylinklabs.watchtower.*` label, and `dockerproxy` exposes only `CONTAINERS`/`POST`/`PING`/`VERSION`                                                                                | Watchtower is retired. `IMAGES`/`NETWORKS`/`DELETE` existed only for its recreate flow — the one that leaves **no container at all** (13 h, then 7 days) — so narrowing the proxy removes the capability that caused the incident, not just an unused grant                           | [0025](docs/decisions/0025-watchtower-retired.md)                                                                  |
| `memswap_limit == mem_limit` wherever `mem_limit` is set                                                                                                                                                          | Otherwise it balloons into host swap and thrashes everything else first                                                                                                                                                                                                               | [0007](docs/decisions/0007-qbittorrent-memory-cap.md)                                                              |
| qBittorrent's disk-IO stays `DisableOSCache` — checked against the **live session**                                                                                                                               | `mem_limit: 4g` is only the backstop; this is the actual fix for the 21.1 GB cgroup peak, and it lives in qBittorrent's own config where the WebUI can silently revert it. Read from the API, not `qBittorrent.conf`                                                                  | [0007](docs/decisions/0007-qbittorrent-memory-cap.md)                                                              |
| No autoheal-monitored healthcheck may probe a **dependency**                                                                                                                                                      | autoheal turns a failing probe into a restart, so a probe that depends on anything outside the container restarts the wrong thing — forever. slskd's Soulseek login is the live instance; the rule is generic                                                                         | [0009](docs/decisions/0009-slskd-healthcheck.md)                                                                   |
| `start_period` must outlast the slowest legitimate startup                                                                                                                                                        | slskd doesn't bind `:5030` until its ~18 min share scan finishes. At 120 s it went unhealthy at ~9 min, autoheal restarted it, and the scan began again at 0% — a loop it never escaped                                                                                               | [0009](docs/decisions/0009-slskd-healthcheck.md)                                                                   |
| autoheal's stop timeout ≥ the longest monitored `stop_grace_period`, `CURL_TIMEOUT` > that                                                                                                                        | Otherwise restarts are cut off mid-stop and pile up three deep. **Computed from the live model**, over the monitored services only, honouring per-container overrides                                                                                                                 | [0010](docs/decisions/0010-autoheal-timeouts.md)                                                                   |
| No `QBITTORRENT_USER`/`PASS` on the container                                                                                                                                                                     | The image never read them; it only leaked them into `docker inspect`                                                                                                                                                                                                                  | [0011](docs/decisions/0011-qbittorrent-credentials.md)                                                             |
| Only `dockerproxy` mounts `/var/run/docker.sock`                                                                                                                                                                  | The socket is root on the host. Asserted in config **and** against the running containers                                                                                                                                                                                             | [0013](docs/decisions/0013-dockerproxy-sole-socket-holder.md)                                                      |
| sonarr/radarr/lidarr/**bazarr** mount `/data`                                                                                                                                                                     | Hardlinks can't cross a mount point (cost 0.96 TiB); bazarr needs it to resolve \*arr paths. **Both ends of the link must be inside it** — a root folder alone is not enough                                                                                                          | [0002](docs/decisions/0002-single-mount-data-hardlinks.md), [0015](docs/decisions/0015-bazarr-no-data-mount.md)    |
| Jellyfin's volume mappings are **not** changed — sources included                                                                                                                                                 | Owner instruction, and 3 systems are calibrated to `/data/movies`. Repointing the _source_ breaks the same three while leaving the target intact                                                                                                                                      | [0016](docs/decisions/0016-jellyfin-paths-are-load-bearing.md)                                                     |
| Every `swag=enable` service has a tracked proxy-conf, and every conf has a label                                                                                                                                  | The **conf** is what routes, not the label — the auto-proxy mod is not installed. `lingarr` had a label and no conf (served SWAG's default page, answering `200`); `slskd` had a conf and no label. All 16 confs are tracked, nine of which existed nowhere else                      | [0022](docs/decisions/0022-proxy-confs-are-tracked.md)                                                             |
| Every service: `cap_drop: ALL`, `no-new-privileges`, capped logs, loopback UI                                                                                                                                     | The hardening baseline. **No exceptions** — the last two waivers were closed with measured sets on 2026-09-02                                                                                                                                                                         | [0001](docs/decisions/0001-hardening-baseline.md), [0018](docs/decisions/0018-capability-gaps.md)                  |
| No script and no compose file holds an ntfy **topic** string; every publisher names a **lane**                                                                                                                    | Six lanes, and severity carried by the priority, only survives if the routing is one lookup. A bare topic literal still publishes and a wrong lane still returns `200`, so both failures are silent. Containers interpolate the same `${NTFY_TOPIC_*}` variables the router reads     | [0033](docs/decisions/0033-ntfy-topic-taxonomy.md)                                                                 |
| Every lane has a 1–5 priority, and **`nas-critical` is neither delayable nor cooldown-suppressible**                                                                                                              | Asserted in the source, not by convention: `cooldown_seconds()` pins it to zero and `build_message()` strips `X-Delay` for it. There is no cooldown value that is correct for the one lane where a swallowed message is the failure mode itself                                       | [0033](docs/decisions/0033-ntfy-topic-taxonomy.md)                                                                 |
| The \*arr ntfy token is a `0600` **file** mounted `:ro`, never an `environment:` entry                                                                                                                            | A credential in an environment block leaks into `docker inspect`. `:ro` specifically because a container that can rewrite its own credential can escalate its own ACL — and this token is scoped so a compromised \*arr cannot reach `nas-critical` at all                            | [0011](docs/decisions/0011-qbittorrent-credentials.md), [0033](docs/decisions/0033-ntfy-topic-taxonomy.md)         |
| `scripts/arr_notify.sh` is mounted `:ro` into exactly sonarr, radarr and lidarr                                                                                                                                   | It runs inside the import pipeline, so it must not be modifiable from there — and bazarr, lingarr, recyclarr and whisper deliberately notify nothing at all                                                                                                                           | [0033](docs/decisions/0033-ntfy-topic-taxonomy.md)                                                                 |

Start at [`docs/decisions/README.md`](docs/decisions/README.md) for the full index.

---

## Updating services

**Nothing in this stack applies an update.** Detection and application are fully
separated, because the thing that used to do both had a non-atomic recreate that
twice left no container at all.
→ [ADR-0025](docs/decisions/0025-watchtower-retired.md)

### Detected (every image, including the pinned ones)

`diun` reads a manifest generated from the compose model and watches each
image's **repository**, so it can answer "what is newer than this pin" — which
is why it covers `jellyfin` and `qbittorrent`, the two Watchtower structurally
could not report on. It runs at 04:10 and pushes to `nas-updates` at **priority 1** — its own lane,
because an available update is the lowest-value message this stack produces:
nothing is broken, nothing is waiting, and a human applies it deliberately
([ADR-0033](docs/decisions/0033-ntfy-topic-taxonomy.md)).

`diun` has **no route to the Docker API at all**, not even through
`dockerproxy`. `make check` asserts the manifest covers every image-pulling
service and still matches the compose model — drift in a notification config is
silent, so it is asserted rather than trusted.
→ [ADR-0024](docs/decisions/0024-diun-version-aware-notification.md)

```bash
make diun-manifest      # regenerate after adding a service, then commit it
```

### Applied (always by hand)

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

| Layer                                             | What it catches                                                                                                                                                                                                  | Blind to                                                         |
| ------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Docker healthchecks                               | A service answering wrong                                                                                                                                                                                        | A service that no longer exists                                  |
| `autoheal`                                        | Unhealthy `qbittorrent`/`slskd`, restarts within ~30 s                                                                                                                                                           | Anything unlabelled                                              |
| `scripts/stack_watchdog.py` (`*/5`)               | A service defined in compose with **no container at all**, plus unhealthy ones                                                                                                                                   | The host itself                                                  |
| `scripts/heartbeat.py` (`*/10`) → healthchecks.io | **The host being down**                                                                                                                                                                                          | —                                                                |
| `make verify-runtime` (daily 06:15)               | Running containers drifting from the invariants — a missing container, a lost capability, a stray `compose.override.yaml`, **the ntfy grants, and every \*arr / jellyseerr / cleanuparr notification connector** | Anything the config alone can prove (that is `make check`'s job) |
| `scripts/offsite_backup.sh` (daily 02:00)         | Config surviving the loss of this machine                                                                                                                                                                        | Media — 4.6 T is not backed up anywhere, by choice               |

### Six lanes, not one topic

Everything publishes to self-hosted `ntfy` over loopback, so contents never
leave the box; only the phone's subscription goes out through SWAG. `deny-all`
by default, accounts created by hand.

**Severity is carried by the priority; audience by the topic.** Naming a topic
for its audience is what makes each one configurable on the phone once and then
left alone — a phone can mute a topic but cannot un-mute half of one, which is
why `nas-errors`/`nas-warnings` was rejected.

| Topic           | prio | What lands here                                                                                                                     | Phone setting          |
| --------------- | ---- | ----------------------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| `nas-critical`  | 5    | A compose service with **no container at all**, host OOM kill, disk error, failed config backup, user-visible service down >5 min   | bypass Do Not Disturb  |
| `nas-attention` | 4    | Needs a human today: \*arr health, manual interaction, import/download failure, slskd logged out, disk >90 %, a cleanuparr deletion | default                |
| `nas-media`     | 3    | New stuff you can watch, with quality, size and a tap through to Jellyfin                                                           | normal                 |
| `nas-requests`  | 4    | Jellyseerr approvals, declines, failures, issues                                                                                    | bypass Do Not Disturb  |
| `nas-infra`     | 2    | Recoveries, first cron failures, drift, the 09:00 digest                                                                            | min importance, silent |
| `nas-updates`   | 1    | `diun`, and nothing else                                                                                                            | min importance, silent |

**Nothing publishes directly.** `scripts/notify.py` is the only thing that knows
a topic name; callers name a _lane_. `make check` asserts that no script and no
compose file holds a topic string, that every lane has a priority, and that
`nas-critical` can be neither delayed by quiet hours nor swallowed by a cooldown.

Noise controls, which are the point rather than a refinement: alerts fire on a
state **change** (a `*/5` job cannot send the same message 288 times a day);
an unhealthy container starts in `nas-infra` and escalates to `nas-attention` at
15 min, or `nas-critical` at 5 min if it is user-visible; cooldowns are 6 h on
`nas-attention` and 1 h on `nas-infra` and **none** on `nas-critical`; quiet
hours 23:00–08:00 delay only the three chatter lanes. Suppressed messages are
counted and reported by the 09:00 digest — that number is what keeps the windows
honest.

```bash
make notify-test    # one message per lane, read back with the phone's own token
make notify-acl     # the live ntfy users, grants and token labels (redacted)
```

→ [ADR-0033](docs/decisions/0033-ntfy-topic-taxonomy.md) ·
[ADR-0012](docs/decisions/0012-ntfy-alerting.md)

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

28 cron entries, all wrapped in `scripts/cron_job.py`, which reports failures
and staleness to ntfy — a job that stops running is itself an alert. A job's
**first** failure goes to `nas-infra` (most self-heal on the next tick) and a
second consecutive one to `nas-attention`; `config-backup` and `offsite-backup`
pass `--fail-lane critical`, because a backup failure is only ever discovered
when you need the backup. Successes never notify.

> **Every cron line must `cd /home/tom/nas` first.** The scripts resolve `.env`
> and `logs/` relative to the working directory.

| When              | Job                                                                                                                                                                                                                                 |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `*/5`             | `stack_watchdog`, `media_ops_status`, `qbittorrent_settings_enforce`                                                                                                                                                                |
| `2-59/5`          | `lidarr_jellyfin_bridge` (Lidarr has no working path mapping)                                                                                                                                                                       |
| `*/10`            | `heartbeat` (off-box dead-man's switch)                                                                                                                                                                                             |
| `*/15`            | `slskd_login_watch`                                                                                                                                                                                                                 |
| `5,20,35,50`      | `lidarr_monitor_sweep --no-search`                                                                                                                                                                                                  |
| `12,27,42,57`     | `lidarr_backlog_drip`                                                                                                                                                                                                               |
| `:07 :22 :37 :52` | Tubifarry/slskd unclog chain — **shares one flock**, do not run these concurrently                                                                                                                                                  |
| `:17`             | `wan_shaper.sh apply` (scoped sudo)                                                                                                                                                                                                 |
| daily             | `config_backup` 01:00 · `offsite_backup` 02:00 (commented until a destination is set) · `slskd_rescan` 03:30 · `post_update_verifier` 04:30 · `process_soulseek_imports` 05:30 · `verify-runtime` 06:15 · **`notify_digest` 09:00** |
| every 6 h         | `playlist-sync`                                                                                                                                                                                                                     |
| weekly            | `log_pruner` · `docker prune` · `album_art` · per-library Jellyfin scans (Fri/Sat/Sun 05:05)                                                                                                                                        |

```bash
crontab -l                          # the real list
tail -f logs/stack_watchdog.log     # or any logs/<job>.log
```

---

## Troubleshooting

### A service is missing entirely (no container)

This was the Watchtower failure mode, and nothing in the stack can cause it any
more ([ADR-0025](docs/decisions/0025-watchtower-retired.md)) — but a failed
`docker compose up -d` can still leave a service with no container, so the
watchdog keeps looking. `restart: unless-stopped` cannot help.

```bash
docker compose ps -a | grep -i exit
docker compose up -d <service>
docker logs diun --since 24h | grep -i "error\|failed"
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
of it — the import _source_ is whatever path the download client reports, and
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
made at all. The \*arr "Test" button proves nothing — it returns `200` while
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

Bazarr resolves \*arr paths verbatim, so it needs `/data` mounted:

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
- add a healthcheck; to publish it, commit `swag/proxy-confs/<name>.subdomain.conf`,
  bind-mount it into swag, **and** add `labels: [swag=enable]` — the conf is what
  routes, the label is what `make check` reconciles it against ([ADR-0022](docs/decisions/0022-proxy-confs-are-tracked.md))
- run `make diun-manifest` and commit the result, so update notification covers
  it — `make check` fails if you don't ([ADR-0024](docs/decisions/0024-diun-version-aware-notification.md))
- if it must be updated by hand (a pinned tag, a database engine), pin the tag
  and add it to `MANUAL_UPDATE_ONLY` in the checker with the reason
- do **not** add a `com.centurylinklabs.watchtower.*` label — there is no
  watchtower, and `make check` rejects one ([ADR-0025](docs/decisions/0025-watchtower-retired.md))
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

- **`pnpm lint` is red on three files this work did not touch.** 76 prettier
  errors in `TRIAGE-2026-09-03.md`, `docs/decisions/0032-alert-noise-ownership.md`
  and `docs/removed-indexers/torrent-core-2026-09-03.json` — table alignment and
  `*emphasis*` vs `_emphasis_`, no content issues. They pre-date the ntfy
  taxonomy branch and belong to in-flight work elsewhere, so they were left
  alone rather than bundled into an unrelated commit. `pnpm lint:fix` clears
  them; **CI's lint gate is red until someone does.**

- **`arr_notify.sh`'s payload variable names are verified by the first real
  import, not in advance.** The \*arr Custom Script **Test** passes exactly one
  variable, `<app>_eventtype=Test`, so it proves invocation and confirms no
  payload name; the names are not literals in the shipped DLLs either. The
  script therefore dumps its whole environment once on the first real event
  (marker-gated, in `/config/logs/arr_notify.log`) and every field degrades to
  `imported` rather than rendering blank. Check that log after the next import
  and correct anything thin.
  → [ADR-0033](docs/decisions/0033-ntfy-topic-taxonomy.md)

- **Jellyseerr's "now available" message has no tag and jellyseerr's own
  title.** Its native ntfy agent hardcodes `priority = 3` and sets no tags, so
  the configurable webhook agent went to `nas-requests` (which must be priority 4) and the native agent to `nas-media` (whose priority is 3 anyway). The lost
  `popcorn` tag is decoration; the priority is the contract.
  → [ADR-0033](docs/decisions/0033-ntfy-topic-taxonomy.md)

- **Nothing sweeps the library for monitored-but-missing media, and the obvious
  tool is disqualified.** Cleanuparr handles the clean side; the fill side is
  covered only by Sonarr's and Radarr's own built-in periodic searches plus the
  two music-specific cron jobs. `Huntarr` was evaluated and **rejected**: its
  upstream was archived on 2026-02-23 after unauthenticated auth bypasses that
  leaked stored passwords and **every integrated \*arr API key**, and no image
  is pullable any more. The requirement for a replacement is an actively
  maintained upstream with credentials that cannot be read unauthenticated.
  → [ADR-0029](docs/decisions/0029-huntarr-rejected.md)

- **Update notification now covers pinned tags, and applying an update is still
  manual on purpose.** `diun` watches every image in the compose model from a
  generated, asserted manifest — including `jellyfin` and `qbittorrent`, which
  Watchtower structurally could not report on. Nothing in the stack can apply an
  update any more; that is `docker compose pull <svc> && docker compose up -d
<svc>`, or `make pull-jellyfin` / `make update-qbittorrent`.
  → [ADR-0024](docs/decisions/0024-diun-version-aware-notification.md)

- **Jellyfin's memory blowup is narrowed, not proven fixed.** The ffprobe
  fan-out hypothesis is **eliminated**: all six OOM kills named a single
  `task=jellyfin` process holding 22.8–24.0 GB of anonymous memory, and **zero**
  of them coincided with a library scan. No recurrence in 30 h and peak anon is
  down from 23 GB to 2.20 GB, so `mem_limit` is reclassified as defence in depth
  and `MALLOC_ARENA_MAX` + W^X are the probable fix. What is still missing is an
  A/B: the sampler's history starts after the mitigations landed, and proving
  causation means provoking another host-wide OOM.
  → [ADR-0008](docs/decisions/0008-jellyfin-memory-mitigations.md),
  [investigation](docs/jellyfin-memory-investigation.md)
- **The off-box config backup is built but not yet pointed anywhere.**
  `scripts/offsite_backup.sh` is written, tested end to end against a local
  repository (retention proven to prune, restore verified byte-identical),
  restic 0.18.1 is installed, and the cron entry is installed **commented out**.
  One action closes it: set `RESTIC_REPOSITORY` in `.env` (and the `AWS_*` keys
  for an S3-compatible destination), `restic init`, then uncomment the
  `#PENDING-DESTINATION` line in `crontab -e`. Until then the script exits `2`
  saying so, rather than pretending to have a backup.
- **Media is not backed up at all.** 4.6 T under `${SHARE_DIRECTORY}`, by
  choice. The off-box backup above covers **config only**.
- **The media disk cannot be SMART-monitored, and never will be on this
  hardware.** `scrutiny` now trends the NVMe, but `/dev/sda`'s USB bridge
  answers no SMART under any `smartctl -d` type — re-verified 2026-09-02 from
  the host and from inside the container with the capability granted and the
  device passed in. Its only signals are `stack_watchdog.py`'s kernel-log sweep
  (6 h window), the ext4 superblock error counter (durable), and a read-only
  remount. Closing this properly needs different hardware, not different
  software. → [ADR-0023](docs/decisions/0023-smart-monitoring.md)
- **The kernel journal retains only ~3 days on this host** (2 boots, 40 MB
  measured 2026-09-02). `stack_watchdog.py` reads `journalctl -k` for OOM kills
  and disk errors, and ADR-0008's evidence base has the same horizon, so
  anything older is simply gone. The ext4 counter above was added because it is
  the one disk signal that outlives this window.
