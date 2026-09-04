---
name: nas-public-surface
description: Use when publishing, unpublishing or changing any route on this NAS stack - a proxy-conf, a swag=enable label, the tinyauth door, a new service that needs a subdomain, or anything touching auth. What routes a subdomain is the conf file, not the label; the conf reaches the container by inode, so a git checkout can silently un-deploy it; and auth_basic does not stack with forward auth, it preempts it.
---

# The public surface

Seventeen subdomains plus the apex. Thirteen sit behind one tinyauth door; four must
never. Every rule below is here because it already went wrong.

Read `docs/decisions/0022-proxy-confs-are-tracked.md` and
`docs/decisions/0034-one-door-tinyauth.md` before changing any of it.

## The two mechanisms, and which one is real

**The conf publishes the subdomain. The label does not.** No `DOCKER_MODS` is set, so
linuxserver's auto-proxy mod is not installed: `swag=enable` is documentation and
`swag/proxy-confs/<name>.subdomain.conf` is mechanism. Both directions have drifted —
`lingarr` had the label and no conf (SWAG's default page, answering **200**), `slskd` had
a conf and no label. `make check` reconciles them both ways.

`tinyauth` is the one service whose route name differs from its service name (`auth.`).
That lives in `ROUTE_ALIASES` in `check-invariants.sh`, explicitly, so the two cannot
quietly agree on the wrong name. Do not add a second alias without an ADR.

## A conf edit is a deploy, and `reload` is not how you apply it

Each conf is bind-mounted **as a single file**, because a read-only mount over the whole
`proxy-confs` directory breaks SWAG's startup (it rewrites 372 `*.conf.sample` files into
it at every start). Docker binds a single file **by inode**. So anything that _replaces_
the host file rather than rewriting it in place detaches the mount:

`git checkout` · `git revert` · `git stash pop` · prettier · `sed -i` · most editors

The container then keeps serving the old inode while `git diff` is clean, `nginx -t`
passes, and `nginx -s reload` changes nothing. Measured on
`ongehoord.subdomain.conf`; the only tell was `nginx -T` showing comment text that no
longer existed in the repo.

```bash
make swag-apply                      # recreate swag, then PROVE the confs took
scripts/check-swag-conf-drift.sh     # 20 confs, sha256 repo vs container
```

`make verify-runtime` runs that drift check and escalates a mismatch to `nas-critical`,
because under ADR-0022 the conf _is_ the mechanism: a stale conf is a route that has lost
its door while everything looks fine.

## `auth_basic` does not stack with forward auth — it preempts it

`ngx_http_auth_basic_module` runs **ahead of** `ngx_http_auth_request_module` in nginx's
access phase. So basic auth's `401` is what `error_page 401 = @tinyauth_login` converts,
and the auth subrequest is **never made**. Measured, all three combinations:

| Request                                    | Result                  | Did tinyauth see `/api/auth/nginx`? |
| ------------------------------------------ | ----------------------- | ----------------------------------- |
| valid tinyauth session, no `Authorization` | `302` to the login page | **no**                              |
| valid basic-auth credentials, no session   | `302`                   | yes, then `401`                     |
| both                                       | the app's own response  | yes, `200`                          |

The first row is the normal browser case, so "both doors for a while" is not two doors —
it is basic auth wearing the login page as its error handler, and it locks everyone out.
`make check`'s `no-auth-basic` rejects a live `auth_basic` in any conf this repo tracks
**or in the playlist-generator submodule's** `nginx/app.conf`, which is baked into its
image at build time. Commented-out lines are ignored; SWAG's samples ship them in all 17.

## Who is behind the door, and who must never be

The rule: **anything with a native mobile or desktop client, a sync protocol, or an API
consumed from outside this box cannot sit behind forward auth** — a `302` to a login page
is not something a sync client can follow.

| Decision    | Routes                                                                                                        | Why                                                                                                     |
| ----------- | ------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `protect`   | the \*arr suite, `qui`, `slskd`, `cleanuparr`, `lidarr-bulk`, `playlist-generator`, `ongehoord`, `jellyseerr` | browser-only UIs                                                                                        |
| `never`     | `jellyfin`                                                                                                    | TV/phone/DLNA clients; the LAN ports (8096/8920/7359/1900) bypass SWAG entirely                         |
| `never`     | `nextcloud`                                                                                                   | desktop + mobile WebDAV sync                                                                            |
| `never`     | `ntfy`                                                                                                        | the phone subscribes with token auth — **a door on the alert channel makes a broken door a silent one** |
| `never`     | `auth`                                                                                                        | it is the thing that hands out the session                                                              |
| public      | the apex                                                                                                      | landing page                                                                                            |
| path-scoped | `/ops.html` **and** `/ops-status.json`                                                                        | live stack status. Gate both — the JSON _is_ the data                                                   |

`DOOR` and `DOOR_PENDING` in `check-invariants.sh` hold this, and **both directions are
asserted**: a `protect` route losing its include and a `never` route gaining one are both
failures. `DOOR_PENDING` must stay empty. A published conf missing from `DOOR` fails too —
silence is how eleven of these ended up with no authentication at all.

**`protect` means the include is in `location /`, not everywhere.** Six confs carry an
ungated `location ~ .../api` on purpose: the \*arr API's authentication is its API key, and
gating it would break every native client (nzb360, LunaSea) for no gain. That is a
path-scope, not an oversight.

Before adding a route to `protect`, verify — do not assume — that nothing reaches it
through SWAG. Grep the **live** app configs and app SQLite, not just the repo:

```bash
cd .docker-config && grep -rIl --exclude-dir=log --exclude="*.log*" "4eva\.me" sonarr radarr lidarr bazarr prowlarr jellyseerr cleanuparr recyclarr
for f in */*.db; do h=$(grep -ac "4eva\.me" "$f"); [ "$h" != 0 ] && echo "$f: $h"; done
```

## Failing closed is the design — and it is 500, not 502

The auth location uses a **variable upstream plus the Docker resolver**
(`set $upstream_tinyauth tinyauth; proxy_pass http://$upstream_tinyauth:3000/...`) because
nginx resolves a _literal_ `proxy_pass` hostname at startup and refuses to start on a name
it cannot resolve. A literal there would mean one dead auth container takes Jellyfin,
Nextcloud, ntfy and the apex down with it.

Measured by detaching tinyauth from `nas-network`:

| Route                         | Answer                |
| ----------------------------- | --------------------- |
| protected routes, `/ops.html` | **500**               |
| `jellyfin`                    | `302` (its own login) |
| `ntfy`, `nextcloud`, apex     | `200`                 |

`500`, not `502` — nginx returns `500` when the `auth_request` subrequest itself fails.
So **a 5xx on a protected route means the door is jammed shut, not open.**
`scripts/check-door-live.sh` says exactly that, and distinguishes it from a `2xx`, which
means the door really is open. It runs inside `make verify-runtime` at `nas-critical`.

`swag` has `depends_on: tinyauth: service_healthy`, which is **ordering only** — it stops
swag starting into a window where every protected door is 500. It is not what keeps the
unprotected routes up; the variable upstream is.

## "The password is definitely right and it still says no"

That is the **lockout**, not the credential. `LOGINMAXRETRIES=3` within
`LOGINTIMEOUT=300s`, per identifier, and once tripped tinyauth returns **`401` to
the correct password too** with nothing in the response to say why. The only tell
is `Account locked due to too many failed login attempts failedAttempts=N` in
`docker logs tinyauth`.

```bash
make tinyauth-unlock     # prints the recent failures, then restarts to clear it
```

The counter is **in memory** (there is no lockout table in `tinyauth.db`), so a
restart clears it; sessions are in SQLite and survive, so nobody already logged
in is disturbed. Protected routes 500 for the few seconds it is down.

**Never probe the live door with a deliberately wrong password** — not to "check
whether the limiter is clear", because the attempt that trips the lock also
returns `401`, so a `401` is not evidence the limiter is clear. It is how this
lockout was caused. Test wrong-password rejection against a throwaway instance
with the same hash instead; `scripts/tinyauth_set_password.sh` does exactly that
and deliberately makes no wrong-password attempt against the live door.

## Logging out and the browser cache

Logout is server-side and immediate: the session row is deleted and the same
cookie `401`s straight away. But a protected page can keep rendering after logout
if its upstream sends no `Cache-Control` — the browser heuristically caches the
shell, the request never reaches nginx, and `auth_request` never runs. `Ctrl+F5`
bounces correctly. Measured on `playlist-generator` (no `Cache-Control` at all);
sonarr is immune because it sends `no-cache, no-store` itself.

`swag/tinyauth-location.conf` therefore carries
`add_header Cache-Control "no-cache" always;` for every protected route.
**`no-cache`, not `no-store`** — the browser still caches and revalidates, so
assets come back `304` and the revalidation passes through `auth_request`, which
is the point. If you ever see a logged-out tab still working, check that line
before suspecting the session store.

## tinyauth specifics that cost time to learn

- **The credential is a file, never an env var.** `.env` holds `TINYAUTH_USER` and
  `TINYAUTH_PASSWORD_HASH`; `make tinyauth-users` renders `secrets/tinyauth-users` (`0600`,
  gitignored) which the container mounts **`:ro`**. ADR-0011 (env vars leak into
  `docker inspect`), ADR-0033 (`:ro`, so a container cannot rewrite its own credential).
- **The hash must stay single-quoted in `.env`.** It contains `$`, and `.env` is
  `.`-sourced by shell under `set -u`, where `$2a$10$…` is not a mangled value but
  `$2: unbound variable` **and an abort** — it silently cost `make verify-runtime` its last
  six assertions. Asserted. Make's `$(call …)` re-expands it too; read `.env` with `sed`
  inside the recipe's own shell, or better, do not source `.env` at all.
- **`docker compose up -d tinyauth` does NOT apply a new password.** Compose compares the
  service _config_, not bind-mounted file _contents_, and tinyauth parses the users file
  only at start. It is `docker compose restart tinyauth`.
- **Rotate with `scripts/tinyauth_set_password.sh`.** It proves the hash against a
  throwaway tinyauth before writing anything, revokes sessions, and rolls `.env` back if
  the live door rejects it. `TINYAUTH_ROTATE_FORCE_VERIFY_FAIL=1` re-proves the rollback.
- **A password change alone does not log anyone out.** Sessions live in
  `${CONFIG_DIRECTORY}/tinyauth/tinyauth.db` keyed by uuid with no reference to the
  password. Revoke them (the script does, by default) while the container is stopped.
- **`TINYAUTH_LABELPROVIDER=none` is load-bearing.** The default `auto` discovers per-app
  ACLs from Docker **labels**, i.e. it wants the socket. `dockerproxy` is the sole socket
  holder (ADR-0013) narrowed to autoheal's four endpoint groups (ADR-0025). Per-app rules
  are `TINYAUTH_APPS_[NAME]_*` variables. Asserted, all three parts.
- **`TINYAUTH_APPURL` must be `subdomain.domain.tld` or `domain.tld`.** It rejects an IP
  (`ip addresses not allowed`) and rejects bare `localhost`; it derives the cookie domain
  from it.
- Login endpoint is **`POST /api/user/login`** (`/api/login` 404s). Unauthenticated
  `/api/auth/nginx` returns `401` plus an `X-Tinyauth-Location` header with the login URL —
  the tracked conf prefers that header over SWAG's own computed URL, with the computed one
  as a fallback so an empty variable never produces a `302` to nowhere.
- `user create --interactive` needs a real TTY (bubbletea) and cannot be piped.

## Adding a new published service

1. Write `swag/proxy-confs/<name>.subdomain.conf`; add the `swag=enable` label.
2. Bind-mount the conf read-only in `compose/infra.yaml`'s swag block.
3. Decide its door and add it to `DOOR` — `protect` or `never`, with a reason. There is
   no third option, and leaving it out fails `make check`.
4. If `protect`: `include /config/nginx/tinyauth-server.conf;` at server level and
   `include /config/nginx/tinyauth-location.conf;` inside `location /`. A location include
   without the server include means the `401` has nowhere to go.
5. Add it to `PROTECT`/`NEVER` in `scripts/check-door-live.sh`.
6. `make swag-apply && make check && scripts/check-door-live.sh`.
7. Anonymous must answer `302` to `https://auth.<domain>/login`, and a session must reach
   the app. A `200` anonymous means the door is open.
