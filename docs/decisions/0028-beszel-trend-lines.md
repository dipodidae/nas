# ADR-0028 — Beszel, and why its agent does not use host networking

**Date:** 2026-09-02
**Status:** accepted
**Related:** ADR-0009, ADR-0012, ADR-0013, ADR-0023, ADR-0025

## The gap

Six monitoring layers, all of which answer _"is it broken right now"_:
healthchecks, `autoheal`, `stack_watchdog.py`, `make verify-runtime`, the ops
dashboard, ntfy. None of them can show a **trend**.

That is not an aesthetic complaint. Five Jellyfin OOM kills in 48 h were found
_by accident_ — someone noticed an episode stuttering — and the memory
investigation that followed (ADR-0008) had to reconstruct the shape of the
problem from a kernel log and a hand-written per-minute sampler. A graph is the
difference between noticing a sawtooth and being told about the sixth kill.

## Decision

`henrygd/beszel` (hub, 31.3 MB) + `henrygd/beszel-agent` (agent, 9.4 MB), both
pinned to `0.18.8`, on `nas-network`, hub UI on loopback only, alerts to the
existing `nas-alerts` ntfy topic.

`scripts/stack_watchdog.py` **stays**. It detects _a service defined in compose
with no container at all_, which is the actual historical failure mode and which
Beszel cannot see — Beszel graphs what exists.

## The deviation that matters: no host networking

**Beszel's own documentation says the agent must use `network_mode: host`** to
collect network-interface statistics. It does not, here.

This is not rule-following for its own sake. Following the upstream layout would
have broken **two** rules to buy one metric:

1. AGENTS.md forbids host networking without justification.
2. A host-networked container **cannot resolve `dockerproxy` by service DNS**.
   It sits in the host's network namespace, and `dockerproxy` has no host
   publish — deliberately, because ADR-0013 says the Docker API proxy is
   "reachable inside nas-network only". So a host-networked agent would force
   publishing the Docker API on the host, which is a strictly worse trade than
   losing a graph.

**What is given up:** network-interface stats. **What that costs:** very little.
The only interesting interface on this box is the WAN link, and it is already
measured properly by `scripts/wan_shaper.sh`, which verifies qdisc _and_ rate
_and_ DSCP marks rather than merely that an interface exists — the repo's own
rule about checks proving the property rather than the component.

CPU, memory, disk and per-container metrics all work without host networking.
Verified: `status=up`, 29 containers in a sample, `qbittorrent mem=1858.57MB`,
`jellyfin mem=567.3MB`.

`make check` now asserts that **nothing** in the stack uses host/container
networking, privileged mode, or a host PID/IPC/UTS namespace — because the
pressure to add one arrives with an upstream doc attached, as it did here.

## Docker access

`DOCKER_HOST=tcp://dockerproxy:2375`. No socket mount; ADR-0013 holds.

Worth noting: the endpoint set narrowed in ADR-0025 **already covered this**.
Listing containers and reading their stats are both `CONTAINERS` reads, so
adding a second Docker client required no re-widening of the proxy. That is
evidence the narrowing was cut at the right place rather than to the bone.

`DOCKER_TIMEOUT=10s` — the default is about 2.1 s, which a busy box exceeds, and
a timed-out poll shows up as a **gap in the graph**, i.e. precisely the thing
this service exists to provide.

## Two measured details that would otherwise bite

**The images are distroless.** There is no `/bin/sh` in either, so every
`CMD-SHELL` healthcheck fails with `stat /bin/sh: no such file or directory` and
the container reports `unhealthy` while working perfectly. Observed on the hub
before it was fixed. Both healthchecks use the exec form with each binary's own
health subcommand (`/beszel health --url ...`, `/agent health`). Do not
"simplify" these back to a shell string.

**`TOKEN` is deliberately absent.** It belongs to the agent-dials-hub WebSocket
topology; here the hub dials the agent over SSH on `LISTEN`, so the agent needs
only the hub's public key. `make check` correctly rejected a credential-shaped
env var the process does not read (ADR-0011) — the fix was to remove it, not to
add an allowlist entry.

## The agent's media mount is narrower than it looks

`${SHARE_DIRECTORY}/downloads:/extra-filesystems/media:ro`, **not** the whole
share. `statfs` reports whole-filesystem totals regardless of which
subdirectory is mounted, so this yields byte-identical numbers — `disk=69.77%`
before and after narrowing it — while not handing a metrics agent read access
to every film and album on the disk. The agent still logs
`Detected disk name=sda1 device=/dev/sda1`.

Trending that disk matters because it is 4.6 TB with no redundancy and, as
ADR-0023 records, **no SMART at all**. Usage and I/O trend is the only
quantitative signal it has.

## SMART is not enabled in Beszel

Beszel can read SMART. It is not enabled, for two reasons: the media disk
answers none (ADR-0023), and doing so would require device passthrough, which
`make check` restricts to `scrutiny` alone. The assertions from ADR-0023 would
reject it — correctly.

## No autoheal label, no public route

A dashboard is not worth an auto-restart path (ADR-0009). No `swag=enable` and
no proxy-conf: the hub is loopback-only on `127.0.0.1:8090`, reached over an SSH
tunnel like the \*arr WebUIs.

## Alerting

Beszel's per-user webhook is set to `ntfy://…@ntfy:8410/nas-alerts`, and its
`emails` list is deliberately **empty** — one channel (ADR-0012). Email would
also have needed SMTP this box does not have, so it would have been a silently
dead second channel, which is worse than none.

## Pairing, for the next person

The hub generates `${CONFIG_DIRECTORY}/beszel/id_ed25519` on first start. The
agent authorises the hub by its **public** key:

```
ssh-keygen -y -f ${CONFIG_DIRECTORY}/beszel/id_ed25519    # -> BESZEL_KEY in .env
docker exec beszel /beszel superuser create <email> <password>
```

The hub also needs a record in its `users` collection (separate from
`_superusers`) before a system can be registered, and a system row pointing at
`beszel-agent:45876`. All of that is one-time and recorded here rather than
rediscovered.

## Dozzle: proposed, not adopted

Cross-container log search was considered and left out of this pass. The honest
reason is that it would not fix what it appears to: the `10m`/`2` json-file log
budget means Docker keeps only minutes of history for a chatty service, and
Dozzle can only search what Docker kept. Wanting retention is a separate
decision, and probably a "no" on a disk with no redundancy.
