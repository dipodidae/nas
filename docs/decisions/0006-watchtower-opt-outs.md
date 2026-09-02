# ADR-0006 — Watchtower's recreate is not atomic; who is opted out

**Date:** 2026-09-01
**Status:** accepted
**Background:** `docs/qbittorrent-crash-fix.md` §F "Watchtower — not in the
original theory, and the reason it never came back"

## The failure mode

Watchtower's update is stop → remove → create, and it is **not atomic**. When
the remove fails, Watchtower logs `Failed=1` and **moves on without creating a
replacement** — leaving no container at all.

`restart: unless-stopped` cannot help: there is nothing left to restart.
autoheal cannot heal what does not exist.

This is not theoretical. On **2026-09-01 04:01** the remove of qbittorrent
failed with *"tried to kill container, but did not receive an exit event"*,
Watchtower moved on, and **qbittorrent simply ceased to exist for 13 hours**
until a human noticed.

The risk scales with how slow a service is to stop, because a slow stop is
exactly what makes the remove time out.

## Decision

Watchtower acts only on containers carrying
`com.centurylinklabs.watchtower.enable=true`. The following are **deliberately
unlabeled** — not forgotten:

| Service | Why |
|---|---|
| `qbittorrent` | Slow stop, and the 13h outage above. Tag is pinned anyway (ADR-0005), so there is nothing to update. |
| `jellyfin` | The worst service in the stack to lose this way, and slow to stop. |
| `dockerproxy` | Watchtower must not restart its own dependency. |
| `watchtower` | Must not self-update. |
| `autoheal` | Control plane. |
| `playlist-generator-db` | Never auto-update a database engine out from under its data; a pg major bump needs a dump/restore, not a recreate. |
| `4eva-rootpage`, `lidarr-bulk`, `ongehoord`, `playlist-generator` | Locally-built images — Watchtower cannot pull them, so a label would add recreate risk for no benefit. |

Slow, important services are updated by a deliberate
`docker compose pull <svc> && docker compose up -d <svc>` instead —
`make pull-jellyfin`, `make update-qbittorrent`.

**Amended 2026-09-02: jellyfin's tag is now pinned too**
(`10.11.11ubu2604-ls47`, the digest that was already running, so applying it
changed nothing). Both slow-to-stop services now behave identically — an update
is chosen, never taken by surprise, because a Jellyfin regression is discovered
mid-playback rather than on a Tuesday morning.

The consequence is that pinning *and* the label omission above together make
these two services completely invisible to Watchtower: an unlabelled container
is never checked, and a pinned tag never reports an update even if relabelled.
That is a real blind spot and it is accepted rather than overlooked — see
ADR-0020 §"The notification blind spot this creates" for why closing it needs a
version-aware watcher rather than a Watchtower setting.

## `WATCHTOWER_TIMEOUT=150s`

Watchtower's own SIGTERM→SIGKILL timeout is separate from Compose's
`stop_grace_period` and **defaults to 10s**, which is far too short for
qbittorrent to flush `torrents.db` (needs up to 120s). A too-short timeout here
force-killed qbittorrent mid-shutdown on **2026-08-19**, hit a
docker/containerd race, and left the container dead for **7 days** with no auto
recovery.

**Invariant:** `WATCHTOWER_TIMEOUT` >= the longest `stop_grace_period` in the
stack. Same class of trap as ADR-0010.

## Detection for the general case

`scripts/stack_watchdog.py` (cron `*/5`) alerts when a service defined in the
compose files has **no container at all** — not merely when one is unhealthy.
That is the detector for this failure mode. See ADR-0012.

## Machine-readable opt-out

The distinction between "deliberately unlabeled" and "forgot the label" is kept
as an explicit allowlist in `scripts/check-invariants.sh`, not as an `x-`
field in the compose files — see ADR-0000 for why.
