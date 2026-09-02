# ADR-0000 — One Compose project, many files

**Date:** 2026-09-02
**Status:** accepted
**Supersedes:** the single 1408-line `docker-compose.yml`

## Decision

`compose.yaml` at the repo root declares only the project name, the network,
and an `include:` list. Services live in five modules under `compose/` and in
one file per locally-built app under `webapps/`.

## Why one project and not several

Four `depends_on` edges cross module boundaries:

- `swag` → `4eva-rootpage` (`service_healthy`)
- `cleanuparr` → `qbittorrent` (`service_healthy`)
- `playlist-generator` → `playlist-generator-db` (`service_healthy`)
- `lidarr` → `slskd`

`depends_on` only resolves inside one project. Splitting into separate projects
would silently drop all four. `include:` merges the modules into a single
project, so they keep working — verified in the post-refactor
`docker compose config`.

## Why `include:` and not `-f a.yml -f b.yml` or `extends` for the modules

`-f` merging resolves every relative path against the **first** file's
directory. `include` resolves each included file's relative paths against
**that file's own** directory. That is the whole reason `webapps/*/compose.yaml`
can sit next to its Dockerfile and say `build.context: .`.

Both behaviours were confirmed empirically rather than assumed:

- root `.env` **does** reach included files (interpolation uses the top-level
  project environment), and
- relative paths in an included file **do** resolve against that file's
  directory.

Those two coexist, which is what makes the layout work.

## Why the network is declared once, in the root

`nas-network` (172.30.0.0/24, gateway .1, `enable_ip_masquerade`) is declared
only in `compose.yaml`. Modules merely list it under each service's
`networks:`. This was the uncertain part of the design, so it was tested first:
a module that references an undeclared network resolves against the root
declaration without warning or error. No `external: true` and no
`make bootstrap`-created network is required. The fallback was not needed.

## Why `extends` and not a per-file `x-` anchor header

YAML anchors are file-scoped and cannot be aliased across files. A split layout
using anchors therefore needs the anchor header copy-pasted into all five
modules plus all four webapp files — reintroducing exactly the duplication this
refactor removes, and with nine places to forget to update.

`extends` is the only cross-file mechanism Compose has. Its merge semantics were
verified on `docker compose v5.5.0` before being relied on:

- list keys (`cap_add`, …) **append** local values after inherited ones
- map keys (`environment`, `labels`, `healthcheck`, `logging.options`) **merge**,
  local wins per key
- a fragment may extend another fragment, so composite layers work

Because `extends` targets exactly one service, the fragments form a linear
chain rather than a mix-and-match set:

```
svc-base            security_opt, restart, logging 10m/2
└─ svc-hardened     + cap_drop: ALL
   └─ svc-hardened-tz   + TZ
      └─ svc-lsio-env   + PUID/PGID
         └─ svc-lsio    + CHOWN,SETUID,SETGID,DAC_OVERRIDE
            └─ svc-arr  + UMASK=022 + the /ping healthcheck cadence
```

`compose/_fragments.yaml` is deliberately **absent** from `include:`, so its
fragments are never instantiated and need no `image:`. The consequence is that
`docker compose -f compose/_fragments.yaml config` fails on its own — expected,
and not how it is ever used.

Two side effects of the append-not-prepend rule are worth knowing:

- `swag` needs `NET_BIND_SERVICE` **first** in its capability list to keep the
  on-disk order unchanged, so it extends `svc-lsio-env` and declares all five
  capabilities locally.
- `lingarr` takes only three of the four LSIO capabilities (no `DAC_OVERRIDE`),
  and `lidarr-bulk` takes the four capabilities but no `PUID`/`PGID`. Both
  therefore extend a lower layer and declare capabilities locally.

## What was deliberately NOT deduplicated

Anything whose value is a per-service exception stays local, next to the comment
explaining it: qbittorrent's `KILL`, nextcloud's `NET_BIND_SERVICE` and its
25m/3 log budget, jellyseerr's `FOWNER`, swag's `NET_BIND_SERVICE`, every
`mem_limit`/`memswap_limit` pair, every `stop_grace_period`. Hoisting an
exception into a fragment is how an exception quietly becomes the default.

## Why `webapps/playlist-generator/` is a wrapper directory

The app's source is a git submodule (`webapps/jellyfin-playlist-generator`, its
own GitHub repo). A `compose.yaml` inside it would commit stack-local config
into a foreign repo, and `git submodule update --remote` could clobber it. So
the compose file lives in a sibling wrapper directory and points
`build.context` at the submodule. `webapps/ongehoord/` already had this shape
for its own nested submodule, so this is consistent rather than novel.

## Rejected: compose `profiles:`

Considered for `whisper` and `lingarr`. Rejected on two counts. A service with
`profiles:` is excluded from the default `up` **by definition**, which is the
one thing the brief forbade; and `bazarr` has `depends_on: whisper`, so
profiling whisper would break bazarr's startup. It would also have changed
`docker compose config` output, breaking the no-op proof.

## Rejected: named volumes for `${CONFIG_DIRECTORY}`

Rejected. The host-visible bind mounts are load-bearing for operations, not an
accident: `scripts/config_backup.py` and the cron jobs read those trees
directly, the WAL-mode SQLite reads documented in `AGENTS.md` depend on being
able to copy `app.db`/`-wal`/`-shm` from the host, and the `qui`/`ntfy`
pre-chown gotcha (ADR-0014) is diagnosable precisely because the directory is
an ordinary path you can `ls`. Named volumes would also have been a semantic
change, violating the no-op constraint. The one thing they would buy —
not having to pre-chown — is worth less than host-side backup and inspection.

## Rejected: an `x-no-watchtower` marker field

The brief asked whether the bare `# Locally-built image: Watchtower cannot
pull, so opt-out.` comments deserve to become a machine-readable marker so the
invariant checker can tell "deliberately unlabeled" from "forgot".

They do — but not as a Compose field. A service-level `x-` key **does** appear
in `docker compose config` output (verified), so adding one to six services
would have produced six diff hunks against the no-op baseline, and it risks
perturbing the config hash Compose uses to decide whether `up -d` must recreate
a container. The distinction is instead maintained as an explicit, commented
allowlist inside `scripts/check-invariants.sh` — version-controlled, greppable,
and incapable of touching a running container. Each opted-out service also
carries a one-line `INVARIANT: no watchtower label` comment, so the intent is
readable in both places.
