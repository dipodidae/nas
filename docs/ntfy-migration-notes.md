# ntfy migration notes — Phase 0 baseline

**Scratch file.** Folded into `docs/decisions/0033-ntfy-topic-taxonomy.md` and
deleted at the end of the migration. Nothing here is a secret: every token and
password is referenced by its `.env` key, never by value.

Captured 2026-09-03, before any change.

## The ntfy instance

| Fact                     | Value                                                       |
| ------------------------ | ----------------------------------------------------------- |
| container name           | `ntfy`                                                      |
| listen address           | `:8410` (`NTFY_LISTEN_HTTP`, `compose/infra.yaml`)           |
| **in-network URL**       | `http://ntfy:8410`                                          |
| host publish             | `127.0.0.1:8410:8410` — loopback only                        |
| public URL               | `https://ntfy.${PUBLIC_DOMAIN}` via SWAG proxy-conf          |
| `auth-default-access`    | `deny-all`                                                   |
| cache                    | 48 h (`NTFY_CACHE_FILE`, `NTFY_CACHE_DURATION`)              |

**The brief's `http://ntfy` (port 80) does not exist here.** ntfy runs as
`${PUID}:${PGID}` (ADR-0014 — no LSIO root init to chown `/var/lib/ntfy`), and a
non-root process cannot bind `:80`, which is exactly why `NTFY_LISTEN_HTTP=:8410`
is set and SWAG proxies to it. Every in-container publisher therefore uses
`http://ntfy:8410`, and every host publisher `http://127.0.0.1:8410` (ADR-0012 —
alert contents never leave the box).

Verified from inside consumer containers:

```
$ docker exec sonarr curl -fsS -o /dev/null -w '%{http_code}' http://ntfy:8410/v1/health
200          # radarr, lidarr: also 200
```

`curl` **and** `wget` are both present in `sonarr`, `radarr`, `lidarr` (LSIO
images). `jellyseerr` has `wget` only; `cleanuparr` has `curl` only. So
`scripts/arr_notify.sh` can rely on `curl` in the three containers it is mounted
into, and still probes for `wget` as a fallback.

## Principals and grants before the migration

```
admin     admin role   read-write everywhere
arr       user         write-only  nas-alerts, nas-media
phone     user         read-only   nas-cleanuparr, nas-alerts, nas-media
watchdog  user         write-only  nas-cleanuparr, nas-alerts, nas-media
*         anonymous    no access (server config: deny-all)
```

Tokens (`ntfy token list`, values redacted):

- `admin` — one short-lived web-UI session token
- `arr` — one labelled `diun (update notifications)`, never expires → `NTFY_DIUN_TOKEN`
- `phone` — one short-lived web-UI session token

Three topics were in use: `nas-alerts`, `nas-media`, `nas-cleanuparr`.

`.env` keys already present: `NAS_ALERT_WEBHOOK`, `NAS_ALERT_USER`,
`NAS_ALERT_PASSWORD`, `NTFY_ARR_USER`, `NTFY_ARR_PASSWORD`, `NTFY_PHONE_USER`,
`NTFY_PHONE_PASSWORD`, `NTFY_ADMIN_USER`, `NTFY_ADMIN_PASSWORD`,
`NTFY_DIUN_TOKEN`, `NTFY_WEB_PUSH_PUBLIC_KEY`, `NTFY_WEB_PUSH_PRIVATE_KEY`.

## Every publish call site, before

| # | Publisher                        | Where                                        | Target                                      |
| - | -------------------------------- | -------------------------------------------- | ------------------------------------------- |
| 1 | `stack_watchdog.py`              | `notify()` ~L1046, `NAS_ALERT_WEBHOOK`       | `nas-alerts` (the only delivery function)   |
| 2 | `cron_job.py`                    | imports `Alert`/`notify` from the watchdog    | `nas-alerts`                                |
| 3 | `slskd_login_watch.py`           | `post_webhook()` L121, `SLSKD_ALERT_WEBHOOK` | `nas-alerts` (unauthenticated POST!)        |
| 4 | `scrutiny`                       | `compose/infra.yaml` `SCRUTINY_NOTIFY_URLS`  | `nas-alerts`, shoutrrr userinfo auth        |
| 5 | `diun`                           | `compose/infra.yaml` `DIUN_NOTIF_NTFY_*`     | `nas-alerts`, priority 3, token auth        |
| 6 | sonarr `ntfy — alerts` (id 2)    | Ntfy connector                                | `nas-alerts`, `onManualInteractionRequired` |
| 7 | sonarr `ntfy — media` (id 3)     | Ntfy connector                                | `nas-media`, `onUpgrade,onImportComplete`   |
| 8 | radarr `ntfy — alerts` (id 3)    | Ntfy connector                                | `nas-alerts`, `onManualInteractionRequired` |
| 9 | radarr `ntfy — media` (id 4)     | Ntfy connector                                | `nas-media`, `onDownload,onUpgrade`         |
|10 | lidarr `ntfy — alerts` (id 7)    | Ntfy connector                                | `nas-alerts`, `onHealthIssue,onHealthRestored` |
|11 | prowlarr `ntfy — alerts` (id 1)  | Ntfy connector                                | `nas-alerts`, **no triggers at all**        |
|12 | jellyseerr webhook agent         | `settings.json`, `types: 137`                 | `nas-alerts`, `?auth=` query param          |
|13 | cleanuparr `ntfy (nas-cleanuparr)` | `cleanuparr.db` / `/api/configuration/notification_providers` | `nas-cleanuparr` |
|14 | bazarr `ntfy` notifier           | `bazarr/db/bazarr.db` `table_settings_notifier` | `nas-media`, userinfo auth in the URL     |

Nothing else publishes. `config_backup.py`, `offsite_backup.sh`,
`post_update_verifier.py` and `log_pruner.py` have **no** notify path of their
own — they are wrapped by `cron_job.py`, which is what pushes on a fatal exit.
That is why the routing table's "config backup failed → `nas-critical`" has to be
expressed as a per-job lane override on the wrapper, not as a call inside those
scripts.

## Baseline runtime

`docker compose ps`: **32 containers**, all running. One pre-existing
`unhealthy`: `playlist-generator` (unrelated to this work, present before the
branch was cut). `git status` had, before the branch: a modified
`webapps/jellyfin-playlist-generator` submodule pointer and two untracked files
(`scripts/playlist_sync_stage.py`, `scripts/tests/test_playlist_sync_stage.py`) —
somebody else's in-flight work, left untouched.

## Live API routes worth writing down

- \*arr connectors: `GET/POST/PUT /api/v3/notification` (`v1` for Lidarr and
  Prowlarr), schema at `/api/v3/notification/schema`.
- cleanuparr: `GET/PUT /api/configuration/notification_providers` — found by
  grepping `/app/wwwroot/main-*.js` inside the container, because every unknown
  `/api/...` path returns the SPA's `index.html` with **HTTP 200**, so probing
  cannot distinguish a wrong route from an empty one.
- jellyseerr: `GET/POST /api/v1/settings/notifications/{agent}`. Its webhook
  `jsonPayload` must be **double-encoded** (AGENTS.md).
- bazarr: no usable notifier API; `table_settings_notifier` in
  `${CONFIG_DIRECTORY}/bazarr/db/bazarr.db`, or the UI.
