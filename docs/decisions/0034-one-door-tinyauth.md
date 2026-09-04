# ADR-0034 — One door: tinyauth in front of every browser-only surface

**Date:** 2026-09-04
**Status:** accepted
**Related:** ADR-0011 (a credential in `environment:` leaks into `docker inspect`),
ADR-0013 (dockerproxy is the sole socket holder), ADR-0014 (non-LSIO images need a
pre-chowned config dir), ADR-0022 (the proxy-conf is the mechanism, tracked in-repo),
ADR-0024 (diun watches repositories, so a pin stays visible), ADR-0025 (dockerproxy
narrowed to autoheal's four endpoint groups), ADR-0033 (a `0600` file mounted `:ro`)

Sixteen published subdomains, and every one of them did its own thing: eleven had
**no authentication at all** in front of them, one had hand-rolled nginx basic auth
(`ongehoord`), one had basic auth baked into its own container (`playlist-generator`),
one had a bespoke Nuxt session login (`lidarr-bulk`), and three had real
application-level auth (`jellyfin`, `nextcloud`, `ntfy`). One forward-auth server
replaces the middle three and covers the eleven.

## Decision

`ghcr.io/tinyauthapp/tinyauth`, **pinned**, serving `auth.${PUBLIC_DOMAIN}`, with
**one** credential in `.env`. Every browser-only route includes SWAG's
`tinyauth-location.conf`; nothing else does.

## Why tinyauth and not Authelia / Authentik / Keycloak / Pocket ID / LDAP

There is one household and one credential. Authelia, Authentik and Keycloak all want
a users database, a session store and a configuration language of their own; the
value they add over tinyauth here is a directory this stack does not have. Tinyauth's
whole fit is that its configuration is **environment variables in the `.env` this
repo already treats as the source of truth**, so `make check` can assert it the same
way it asserts everything else. Pocket ID would be a _second_ server for the same
job; if passkeys are wanted later, that is a provider swap behind the same
forward-auth plumbing, not another door. LDAP needs a directory.

## The classification: `protect`, `path-scoped`, `never`

The rule that decides the column: **anything with a native mobile or desktop client,
a sync protocol, or an API consumed from outside this box cannot sit behind forward
auth** — a `302` to a login page is not something a sync client can follow.

| Surface                                                                                                                                                           | Decision                        | Why                                                                                                                       |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `jellyfin.`                                                                                                                                                       | **never**                       | TV/phone/DLNA clients, and the LAN ports (8096/8920/7359/1900) bypass SWAG entirely. Real auth of its own                 |
| `nextcloud.`                                                                                                                                                      | **never**                       | Desktop and mobile sync over WebDAV. Real auth and its own sessions                                                       |
| `ntfy.`                                                                                                                                                           | **never**                       | The phone subscribes with token auth. Putting the alert channel behind the new door makes a broken door a **silent** door |
| `auth.`                                                                                                                                                           | **never**                       | It is the thing that hands out the session                                                                                |
| `4eva.me` apex                                                                                                                                                    | **public**                      | Landing page                                                                                                              |
| `4eva.me/ops.html` + `/ops-status.json`                                                                                                                           | **path-scoped**                 | Publishes live stack status. Both, not just the page — the JSON _is_ the data                                             |
| everything else — `sonarr` `radarr` `lidarr` `bazarr` `prowlarr` `lingarr` `qui` `slskd` `cleanuparr` `lidarr-bulk` `playlist-generator` `ongehoord` `jellyseerr` | **protect (`location /` only)** | Browser-only UIs                                                                                                          |

**`location /` only, and that is deliberate.** Six of these confs carry a second,
ungated `location ~ .../api` block (sonarr, radarr, lidarr, bazarr, prowlarr; added
here for jellyseerr). The door is a **browser** door; the `*arr` API's authentication
is its API key, and gating `/api` would break every native client that uses one
(nzb360, LunaSea) for no gain — an API key is not weaker than a password typed into
the same browser. This is a path-scope, not an oversight; `make check`'s door reconciliation
asserts the auth include is present in `location /` of every `protect` conf and
absent from every `never` one, in both directions.

**Verified, not assumed, before any door closed:** no `*arr`-to-`*arr`, cleanuparr,
recyclarr, bazarr, jellyseerr or `scripts/*` integration is configured with a
`https://<service>.${PUBLIC_DOMAIN}` URL. Grepped across every live app config dir
and every app SQLite database, not just the repo: zero hits outside ntfy's own
message cache. Every in-stack integration resolves a container name on
`nas-network`, and every host script uses loopback. That is what makes the `protect`
column safe.

## The design, and the four things that would each quietly undo it

**1. One cookie.** `TINYAUTH_APPURL=https://auth.${PUBLIC_DOMAIN}` with
`TINYAUTH_AUTH_SUBDOMAINSENABLED=true` (already the upstream default in v5, stated
because it is the point) and `TINYAUTH_AUTH_SECURECOOKIE=true` because every route is
HTTPS. Measured: the session cookie comes back as
`Domain=4eva.me; Path=/; HttpOnly; Secure; SameSite=Lax`, so one login covers every
protected subdomain **and the apex** — which is what makes the `/ops.html`
path-scoping work with the same session. The wildcard `*.4eva.me` DNS record and the
`DNS:*.4eva.me` SAN on the live certificate already cover `auth.`; both confirmed
against the running host, neither changed.

**2. The credential is a file, never an environment variable.** `.env` holds
`TINYAUTH_USER` and `TINYAUTH_PASSWORD_HASH` — the one place a human edits.
`make tinyauth-users` renders `secrets/tinyauth-users` (mode `0600`, gitignored) and
the container mounts it **`:ro`** at `/secrets/users` via
`TINYAUTH_AUTH_USERSFILE`. `:ro` specifically, per ADR-0033: a container that can
rewrite its own credential can escalate its own access. `make check` asserts that
`TINYAUTH_AUTH_USERS` appears in no environment block anywhere, that the file is
mounted `:ro`, that it is `0600`, and that its contents still match `.env` byte for
byte.

That last assertion earned itself immediately. The bcrypt hash contains `$`, and the
first version of `make tinyauth-users` used Make's `$(call getenv,…)`, which expands
at parse time — so `$2a$10$…` was re-read by Make and then by `bash -u` and the
target died with `$2: unbound variable`. Reading the value inside the recipe's own
shell fixes it. Proven end to end: `docker exec tinyauth cat /secrets/users` returns
the hash intact, and the hash appears **nowhere** in `docker compose config`.

**3. `TINYAUTH_LABELPROVIDER=none`.** This one is load-bearing. Tinyauth defaults to
`auto`, which discovers per-app ACLs from **Docker labels** — which means reaching the
Docker socket. ADR-0013 makes `dockerproxy` the sole socket holder and ADR-0025
narrowed it to `CONTAINERS`/`POST`/`PING`/`VERSION` for autoheal alone. Tinyauth gets
**no socket and no dockerproxy route**; any per-app rule is a
`TINYAUTH_APPS_[NAME]_*` variable instead. Asserted, all three parts.

**4. Protected routes fail closed; unprotected ones keep serving.** v5 refuses to
start on a bad `TINYAUTH_*` variable, which is a gift and a loaded gun: one typo
could close every door at once. So the auth location uses a **variable upstream plus
the Docker resolver** — `set $upstream_tinyauth tinyauth; proxy_pass
http://$upstream_tinyauth:3000/api/auth/nginx;` — because SWAG resolves a literal
`proxy_pass` hostname at startup and refuses to start on a name it cannot resolve.
With the variable, nginx still starts when tinyauth is absent: protected routes
return `502`, and `jellyfin` / `nextcloud` / `ntfy` / the apex keep serving. **That
asymmetry is the design.** `swag` additionally gets
`depends_on: tinyauth: condition: service_healthy`, which is ordering only — it stops
swag starting into a window where every protected door is `502`; it is not what keeps
the unprotected ones up.

**The redirect: tinyauth's header, with SWAG's computed URL as a fallback.**
nginx's `auth_request` cannot follow a `302` itself, so something has to issue
one. Both mechanisms exist here — SWAG's sample builds
`https://tinyauth.$domain/login?redirect_uri=…` in a `@tinyauth_login` named
location, and tinyauth returns `X-Tinyauth-Location` on the 401 with the same
URL derived from `TINYAUTH_APPURL` plus `login_for=app`. The tracked conf
**prefers the header** (one source of truth for the login URL) and keeps the
computed URL as a fallback with `auth.` substituted for `tinyauth.`, because an
empty `$tinyauth_login_url` would make `return 302` emit a header with no URL —
a broken door that still answers `302`. `make check` asserts the fallback's
hostname label and `TINYAUTH_APPURL` name the same host, since two places
naming the login page is how a redirect loop happens.

**Identity headers are unspoofable by construction.** The four
`proxy_set_header Remote-*` lines are unconditional: `proxy_set_header`
_replaces_ whatever the client sent, and nginx omits a header whose value is
empty, so a request arriving with `Remote-User: admin` reaches the app with no
`Remote-User` at all. `make check` asserts all four are present and that the
conf contains no `if`.

**Deliberately NOT set: `TINYAUTH_AUTH_TRUSTEDPROXIES`.** It exists only to make
IP-based ACLs work, and there are none. Container IPs on `nas-network` are not
static, and trusting `172.30.0.0/24` means trusting every container on the bridge.
Tinyauth logs `Trusted proxies are not configured, IP access controls will NOT work`
at every start — that warning is **expected**, and this paragraph exists so nobody
"fixes" it. `TINYAUTH_AUTH_IP_BYPASS` for the LAN as a break-glass is likewise
refused: a permanent hole is not a rollback plan. The rollback plan is `git revert`.

**Audit on, brute-force defaults kept.** `TINYAUTH_LOG_STREAMS_AUDIT_ENABLED=true`
(off by default upstream). `LOGINMAXRETRIES=3` and `LOGINTIMEOUT=300` keep their
upstream defaults — tightened or kept, never loosened. `TINYAUTH_ANALYTICS_ENABLED`
is turned **off**: it defaults to `true` and sends version information off the box,
which no other service here does.

## Hardening

`extends: svc-hardened-tz` — the narrowest fitting fragment, since tinyauth does not
honour `PUID`/`PGID`. `cap_drop: ALL`, `no-new-privileges:true`, capped logs, and
`127.0.0.1:3005:3000` for the loopback publish. **3005, not 3000**: `lidarr-bulk`
already owns `127.0.0.1:3000`, which `docker compose config` renders happily and
which only fails at `up` time as a bind error on whichever container starts second.
`make check`'s existing `port-collision` assertion caught it before the first commit.

The image's own `tinyauth` user is uid/gid **1000**, identical to `${PUID}:${PGID}`,
so `user: '${PUID}:${PGID}'` matches upstream's intent rather than fighting it — but
there is no root init to chown `/data`, so `${CONFIG_DIRECTORY}/tinyauth` must
already be `${PUID}:${PGID}` or it crash-loops on `permission denied`. Added to
`make bootstrap` alongside qui/ntfy/diun/beszel. ADR-0014.

**Healthcheck: `tinyauth healthcheck`**, the command the image already ships, which
GETs its own `http://127.0.0.1:3000` and nothing else. No OAuth provider, no network,
no second container — ADR-0009's requirement satisfied by construction rather than by
a hand-written probe. Asserted. `autoheal=true` is set: it is self-contained over
SQLite, so it qualifies, and it declares no `stop_grace_period`, so ADR-0010's
relationship (autoheal stop timeout `150s` ≥ the longest monitored grace period,
`CURL_TIMEOUT` `180s` above that) is unchanged by adding it.

**Pinned to a concrete `v5.1.3`**, not `v5`, and listed in `MANUAL_UPDATE_ONLY`:
_a bad auth container closes every protected door at once, so this update is chosen,
never inherited._ `diun` still reports newer tags, because it watches the
**repository** (ADR-0024) — a `POLICIES` entry admits only plain `v<semver>` releases,
excluding the `-distroless` variants and the steady stream of `-rc`/`-beta`/`-alpha`
tags this repo publishes.

## Where the prompt for this work was wrong about v5

Recorded because the next person will read the same docs.

- `TINYAUTH_AUTH_SUBDOMAINSENABLED` **already defaults to `true`** in v5. Setting it
  is documentation, not a change.
- `user create --docker` **no longer doubles the `$`** in the hash. In v5 both
  `--docker` and the plain form emit the same YAML snippet; `--docker` existed for the
  era when the output was a `TINYAUTH_USERS=` env line.
- The login endpoint is **`POST /api/user/login`**. `/api/login` returns `404`.
- The documented defaults for `resources.path` and the OIDC key paths (`./resources`,
  `./tinyauth_oidc_key`) are **not** what the container uses: `tinyauth config` reports
  `/data/resources` and `/data/oidc/key.pem`, so a single `/data` bind mount covers
  all state.
- There is no `TINYAUTH_SECRET` in v5.
- Unauthenticated `/api/auth/nginx` returns **`401` with an
  `X-Tinyauth-Location` header** carrying the full login URL derived from
  `TINYAUTH_APPURL`. Both redirect mechanisms therefore exist — SWAG's own
  `@tinyauth_login` named location and tinyauth's header — and the tracked
  `swag/tinyauth-server.conf` says which one this stack uses and why.

## Consequences

- Losing the tinyauth container closes every **protected** door and leaves the rest
  serving. That makes it a user-visible single point of failure, so
  `scripts/stack_watchdog.py` treats it as one: `nas-critical` after the same
  5-minute escalation the other user-visible services get (ADR-0033).
- One credential means one credential. A second `.env` variable, a per-app secret or
  a "temporary" second password would undo the entire exercise.
- SSH is not behind this door and never will be — confirmed listening on `:22` before
  the first door closed. Locking yourself out of the web surface is recoverable; the
  fix is `git revert` over SSH.
