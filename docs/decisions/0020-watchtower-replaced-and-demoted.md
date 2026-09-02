# ADR-0020 — Watchtower is replaced and demoted to monitor-only

**Date:** 2026-09-02
**Status:** superseded by [ADR-0025](0025-watchtower-retired.md) — watchtower is
gone entirely, and the notification gap this record identified is closed by
[ADR-0024](0024-diun-version-aware-notification.md)
**Amends:** ADR-0006, which defended against this failure per-service

## Decision

Two changes to one service.

1. **Image:** `containrrr/watchtower:latest` → `nickfedor/watchtower:1.22.0`.
   The upstream was archived on 2025-12-17 with the banner "This project is no
   longer maintained". The fork keeps the `com.centurylinklabs.watchtower.*`
   label namespace and the `WATCHTOWER_*` environment contract, so every label
   and every deliberate omission in this repo keeps its meaning.
2. **Mode:** `WATCHTOWER_MONITOR_ONLY=true`. Watchtower detects and reports new
   images. It never stops, removes or creates a container again.

`DOCKER_API_VERSION=1.40` is dropped with the swap: the fork autonegotiates and
this host serves API 1.55.

## Why the demotion, and why it is not already moot

ADR-0006 handled the non-atomic recreate by opting six services out, one at a
time, each with a comment explaining itself. That defence has three problems:

1. It is opt-_out_, so the dangerous default applies to every new service until
   someone remembers.
2. It only protects the services someone thought to protect. The 16 still
   labelled were all exposed to the same failure — qbittorrent was simply the
   one that drew the short straw.
3. It required a second rule (`WATCHTOWER_TIMEOUT` ≥ the longest
   `stop_grace_period`) that exists solely because Watchtower does the stopping.

It would be convenient if the archive had made this self-solving — if Watchtower
could no longer talk to Docker 29 and the capability were already gone. It has
not. On 2026-09-02 at 04:01, running against Engine 29.7.2 / API 1.55, it
stopped, removed and recreated `qui` with `Failed=0 Scanned=17 Updated=1`, from
a container with `restarts=0`. The capability is live. Monitor-only removes it.

## What is lost

Unattended patching. 16 services no longer update themselves at 04:00.

That is an acceptable trade at this scale: one host, one operator, a stack where
losing qBittorrent for 13 hours or 7 days both actually happened, and where
`docker compose up -d` is a one-command recreate that does not abandon
containers.

## What monitor-only does not do

Per Watchtower's documentation, monitor-only **still pulls** images — HEAD
digest checks let it skip a pull when nothing changed, but it pulls whenever the
repository digest differs from the local one. And `--cleanup` only removes an
old image _after_ a container is restarted with a new one, so under monitor-only
it never fires and pulled images accumulate.

`WATCHTOWER_CLEANUP=true` is therefore left set but inert. The accumulation is
bounded by the existing weekly `docker image prune -f` cron (Sundays 03:00),
which is now load-bearing rather than housekeeping; `make verify-runtime` does
not check crontab, so ADR-0012's job table is where that is recorded. Verified
present on 2026-09-02, together with a baseline to re-check against:

```
$ crontab -l | grep -c 'docker image prune'     1
$ docker images | wc -l                        34   (47 total, 26 active)
$ docker system df                             39.46GB, 11.75GB reclaimable
```

If the image count climbs past roughly double that baseline after a week, the
weekly prune is not keeping up with monitor-only's pulls and the cron should
move to daily.

## The notification blind spot this creates

Watchtower reports updates for the tag a container was started from. Two
consequences worth stating rather than discovering:

- A **pinned** tag never reports an update. After ADR-0006's qbittorrent pin and
  the jellyfin pin added the same day as this record, both are pinned.
- An **unlabelled** container is never checked. `jellyfin` and `qbittorrent` are
  both unlabelled (ADR-0006).

So the two services whose updates most want to be chosen deliberately get no
notification at all — and relabelling them would not help, because the pin
silences them anyway. Closing that needs a _version-aware_ watcher (DIUN,
WUD, or Renovate against the compose files), which is a different tool and its
own decision. Recorded as an open gap in `README.md` rather than half-solved
here.

## What stays true from ADR-0006

The per-service labels stay as they are. They now express "do not even tell me
about updates to this" rather than "do not touch this", which is still
meaningful for the four locally-built images Watchtower cannot pull anyway.
`scripts/stack_watchdog.py` remains the detector for a missing container,
because Watchtower is no longer the only thing that could cause one.

`WATCHTOWER_TIMEOUT` also stays. It is inert under monitor-only, but leaving it
costs nothing and it must be correct again the moment anyone reverts this.
