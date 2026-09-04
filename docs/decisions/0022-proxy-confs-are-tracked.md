# ADR-0022 — Proxy-confs live in this repo, and the label is not what routes

**Date:** 2026-09-02
**Status:** accepted
**Companion to:** ADR-0020 (which added the first tracked conf, for lingarr)

## The finding that reframes this

`CLAUDE.md` and `README.md` both said that adding `labels: [swag=enable]` is what
publishes a service on its subdomain, via the linuxserver SWAG auto-proxy mod.
**That mod is not installed on this host.** No `DOCKER_MODS` is set on the swag
container; `S6_STAGE2_HOOK=/docker-mods` is just the base image's hook.

So what actually routes a subdomain is the **presence of a
`<service>.subdomain.conf`** in `/config/nginx/proxy-confs/`. The label is
documentation, not mechanism.

That means the two can drift apart in either direction, silently, and both had:

- **lingarr** carried the label with no conf. `lingarr.4eva.me` served SWAG's
  default "Welcome to your SWAG instance" page — answering `200`, which is
  exactly why it went unnoticed (ADR-0020).
- **slskd** had a conf and no label: a public surface the compose file did not
  declare. The label has now been added, since the route is intended.

## Decision

**Every enabled proxy-conf is tracked in `swag/proxy-confs/` and bind-mounted
read-only into SWAG**, following the pattern already used for
`4eva-rootpage.root.conf` and lingarr. Sixteen confs in total.

Before this, they lived only in gitignored `${CONFIG_DIRECTORY}/swag/`, so the
nightly `config_backup.py` run was the only thing standing between a config-dir
loss and reconstructing them by hand. The risk was not evenly spread:

| Shape                      | Confs                                                                              | Recoverable without the backup?  |
| -------------------------- | ---------------------------------------------------------------------------------- | -------------------------------- |
| No upstream sample at all  | `cleanuparr`, `lidarr-bulk`, `lingarr`, `ongehoord`, `playlist-generator`, `slskd` | **No** — hand-written            |
| Heavy local edits          | `ntfy` (56 lines), `jellyfin` (22), `jellyseerr` (2)                               | **No** — the edits are the value |
| Identical to SWAG's sample | `bazarr`, `lidarr`, `nextcloud`, `prowlarr`, `qui`, `radarr`, `sonarr`             | Yes, by re-copying the sample    |

Nine of the sixteen existed nowhere else.

## Mounted file-by-file, not as a directory

SWAG writes **372** `*.conf.sample` files into `/config/nginx/proxy-confs/` on
every start. A read-only mount over the whole directory breaks that, so each conf
is mounted individually. Verbose, but explicit and safe.

A side effect worth knowing when debugging: Docker creates a **0-byte file** on
the host at the mount path (`${CONFIG_DIRECTORY}/swag/nginx/proxy-confs/<x>.conf`)
to serve as the mount point. The host copy is empty and irrelevant; the container
reads the tracked file. Verified — the container sees 1305 bytes where the host
file is 0.

## No secrets are being committed

Checked before tracking: the confs referenced `auth_basic_user_file` **paths**
(`/config/nginx/.htpasswd-ongehoord`) and never contained credentials. The
htpasswd files themselves stayed in the gitignored config dir, which was correct.

Moot since 2026-09-04: there is no `auth_basic` and no htpasswd file anywhere in
the stack. Every protected route sits behind one tinyauth door and the single
credential is a `0600` file rendered from `.env`, never a conf. ADR-0034.

## Invariant

`scripts/check-invariants.sh` asserts both directions:

- `swag-labels-are-routed` — every `swag=enable` service has a conf. A conf found
  only in the gitignored config dir is a **warning**, not a pass.
- `swag-routes-are-declared` — every conf has a matching `swag=enable` label, so
  an undeclared public route cannot appear again.

Both degrade to warnings where the SWAG config dir is unreadable (CI).
