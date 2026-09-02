# ADR-0013 — Only `dockerproxy` touches `/var/run/docker.sock`

**Date:** 2026-09-02 (records a standing convention)
**Status:** accepted; the exposed endpoint set is **amended** by
[ADR-0025](0025-watchtower-retired.md) — `IMAGES`, `NETWORKS`, `DELETE` and
`INFO` were dropped with watchtower, leaving `CONTAINERS`, `POST`, `PING`,
`VERSION` for autoheal alone

## Decision

`dockerproxy` (tecnativa/docker-socket-proxy) is the **only** container in this
stack permitted to mount `/var/run/docker.sock`, and it mounts it `:ro`.
Watchtower and autoheal reach the Docker API over TCP through it
(`tcp://dockerproxy:2375`), never the raw socket.

**Never mount `/var/run/docker.sock` into any other service.** Checked by
`scripts/check-invariants.sh`.

## Why

The Docker socket is root on the host. A container with it has full control of
the machine. Watchtower and autoheal both legitimately need to stop, remove and
create containers — so the answer is not to deny them access but to narrow it
to the endpoints they actually use.

## The exposed surface, and why each is needed

`CONTAINERS=1`, `IMAGES=1`, `NETWORKS=1`, `PING=1`, `VERSION=1`, `INFO=1`,
`POST=1`, `DELETE=1`.

`NETWORKS=1` is required for Watchtower to disconnect/reconnect containers on
the custom `nas-network` during the recreate flow — without it the recreate
fails. `CONTAINERS` + `POST` is all autoheal needs.

## Hardening

`read_only: true` with `tmpfs` for `/run` and `/tmp`, `cap_drop: ALL`,
`no-new-privileges`. Only reachable inside `nas-network` — no host publish.

## Related

`dockerproxy` is Watchtower-opt-out: Watchtower must not restart its own
dependency (ADR-0006).
