# ADR-0021 — An nginx whose master and workers differ in uid needs `CAP_KILL`

**Date:** 2026-09-02
**Status:** accepted
**Same mechanism as:** ADR-0004 (qbittorrent), different service

## The mechanism

nginx's master process runs as root and forks its workers as another uid — in
SWAG's config, `user abc;` (uid 1000). Linux permits `kill()` across a uid
boundary only for a process holding `CAP_KILL`. **Being root is not sufficient**
once `cap_drop: ALL` has taken the capability away; the kernel checks the
capability, not the euid.

SWAG dropped ALL and added five capabilities, none of them `KILL`:

```
$ docker compose config --format json | python3 -c "...['swag']['cap_add']"
['NET_BIND_SERVICE', 'CHOWN', 'SETUID', 'SETGID', 'DAC_OVERRIDE']
$ docker exec swag sh -c 'grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status'
CapPrm: 00000000000004c3      # bits 0,1,6,7,10 -- bit 5 (KILL) clear
```

## The evidence, probed rather than inferred

Signal 0 delivers nothing; it only performs the permission check. From root
inside the running container, against a live worker:

```
master uid: 0
worker 873 uid: 1000
sh: can't kill pid 873: Operation not permitted
```

## What it breaks

- **`nginx -s reload`.** The master accepts the SIGHUP (root→root), forks new
  workers with the new config, then tries to tell the old ones to shut down
  gracefully. That signal is refused. Old workers keep serving the **old**
  config indefinitely, and the worker count grows on every reload.
- **`docker compose stop swag`.** The master cannot signal workers to quit, so
  they are still running when the grace period expires and Docker SIGKILLs the
  container.

## Why it had not surfaced

Nothing had reloaded SWAG since it last started, so the worker count still
equalled `nproc` (16) with no surplus. ADR-0022's lingarr proxy-conf is the
first change that reloads it — which is how this was found: by reading the
capability set while choosing one for `playlist-generator`, not by an outage.

## Decision

Add `KILL` to `swag`'s `cap_add`, appended rather than re-sorted so the on-disk
capability order of the existing five is unchanged. `KILL` is the **minimum**
addition; nothing else about SWAG's capability set changes.

`scripts/check-invariants.sh` asserts it for every service that runs nginx and
drops ALL, so `playlist-generator` inherits the rule when ADR-0018's waiver is
closed rather than needing its own record.

## Corollary

"Measure an existing working nginx rather than trusting a blog recipe" is the
right instinct, and SWAG is the obvious thing to measure — but SWAG carried this
bug, so copying its set would have propagated it. Measure the **mechanism**, not
a neighbour's config.
