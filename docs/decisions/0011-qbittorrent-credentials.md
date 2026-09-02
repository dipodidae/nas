# ADR-0011 — `QBITTORRENT_USER`/`QBITTORRENT_PASS` stay out of the container

**Date:** 2026-09-01
**Status:** accepted

## Finding

The LSIO qBittorrent image does not read `QBITTORRENT_USER` /
`QBITTORRENT_PASS`. They were never wired up upstream —
**linuxserver/docker-qbittorrent#228, closed as not planned**. Passing them as
container environment did exactly one thing: leaked the credentials into
`docker inspect`.

## Decision

They stay in `.env`, because `scripts/` still uses them to authenticate against
the qBittorrent WebUI API:

- `scripts/qbittorrent_settings_enforce.py`
- `scripts/qbittorrent_stalled_kickstart.py`
- `scripts/slskd_incomplete_sweep.py`
- `scripts/qbit_cleanup_plan.py`
- `scripts/media_ops_status.py`

They have no business being in the container.

## Generalised invariant

`scripts/check-invariants.sh` asserts that no secret-shaped environment
variable is set on a container that does not need it. `.env` is for the host
tooling; a container gets a credential only if the process inside it actually
reads it.

## Related

`qui` **does** need real qBittorrent credentials, because it connects from
nas-network rather than loopback and so cannot use qbit's localhost
auth-bypass. They are entered once by hand in qui's own UI — not passed as
container environment either. ADR-0014.
