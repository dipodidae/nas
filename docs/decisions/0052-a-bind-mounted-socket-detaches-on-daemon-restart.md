# ADR-0052 — A bind-mounted Docker socket detaches when the daemon restarts

**Date:** 2026-09-23
**Status:** accepted
**Extends:** ADR-0022 (nginx binds each conf by inode), ADR-0006 / ADR-0010 (autoheal), ADR-0024 (dockerproxy's narrowed grants)

## Context

On Sunday 2026-09-20 at 16:01 the Docker daemon restarted. It unlinked
`/var/run/docker.sock` and created a new one. `dockerproxy` had been running
since 09-17 and bind-mounts that socket, so it kept a mount of the **deleted
inode**:

```
host:         inode 546280   mtime 2026-09-20 16:01:45
dockerproxy:  inode   2825   mtime 2026-09-17 13:45:30
```

HAProxy's frontend kept accepting connections and kept answering — with `SC--`
503 on every request, for three days:

```
dockerfrontend dockerbackend/dockersocket 0/0/-1/-1/0 503 0 - - SC-- ... "GET /containers/json HTTP/1.1"
```

`dockerproxy` never restarted, never went unhealthy (its healthcheck is
`disable: true`), and logged nothing that reads as a fault.

**The visible symptom was three hops away.** `autoheal`'s entrypoint runs under
`set -e -o pipefail` and pipes the container list into `jq`. HAProxy's HTML
error page is not JSON, so every pass died on
`parse error: Invalid numeric literal at line 1, column 20` — column 20 being
the `503` in `<html><body><h1>503`. That produced:

- **3747 restarts** of autoheal;
- a healthcheck reporting `cannot exec in a stopped state`, because the probe
  could not win the race against the crash loop — so the container read
  `running` and `unhealthy` at the same time;
- **nothing on the box being auto-restarted at all**, for three days.

The 22-09 `exit=4` blip and the 23-09 06:45 streak were both this. The alert
fired against `autoheal`. The fault was in `dockerproxy`.

## Decisions

### 1. A bind mount is a mount of an inode, and this is the second class of it

ADR-0022 says it for nginx: SWAG binds each proxy-conf by inode, so a
`git checkout`, a `revert`, prettier or `sed -i` detaches the mount and nginx
serves the old file behind a clean `git diff`. **The same mechanic applies to
`/var/run/docker.sock`, with the daemon itself as the editor.** Anything that
replaces a bind-mounted path rather than writing through it detaches every
container holding it.

The general rule: **a bind mount survives a write, not a replace.** For a file
that means `sed -i` and `git checkout`; for the Docker socket it means
`systemctl restart docker`.

### 2. The repair is a recreate of dockerproxy, then of its dependents

```bash
docker compose up -d --force-recreate dockerproxy autoheal
```

`restart` is enough to re-resolve the mount, but `autoheal` must follow
regardless: it does not recover on its own, because the crash loop is its
steady state, not a transient.

### 3. `make check` cannot see this, so the watchdog asserts it

The compose model is correct throughout — the mount is declared, the container
is `running`, the grants are right. This is squarely
`nas-runtime-vs-repo`: the fault exists only in the live mount namespace.

`stack_watchdog.check_dockerproxy_socket` compares the host's inode for
`/var/run/docker.sock` against `docker exec dockerproxy stat -c %i` on the same
path and pages when they differ. It is pure over the two inode numbers, so the
comparison is tested without a daemon, and it stays silent when either number
is unavailable — a dockerproxy that is simply absent is `check_autoheal`'s
business, and guessing here would page on every stack-down.

It routes to `nas-attention`, matching `autoheal:down`, and escalates on the
usual ladder. It is deliberately **not** `nas-critical`: nothing user-visible
is broken, but the supervisor is useless and that needs a human today.

### 4. Do not make autoheal tolerate the bad response

Making the entrypoint survive non-JSON would turn a loud crash loop into a
silent no-op, which is the failure shape this repo keeps paying for. The crash
loop is the only reason the three days were three days and not three weeks.
Fix the socket, not the reaction to it.

### 5. The mount is re-resolved on every daemon restart, by systemd

Detection within five minutes is not the same as prevention. The trigger is
known and narrow — `docker.service` restarting — so the repair is bound to it:

```
host/systemd/dockerproxy-resync.service   # After=/PartOf=/WantedBy=docker.service
  ExecStart=… docker restart dockerproxy autoheal
```

`live-restore: true` in `/etc/docker/daemon.json` is what makes this reachable
at all: containers deliberately survive a daemon restart, so nothing re-resolves
the mount on its own. **That setting is correct and stays** — it is why Jellyfin
keeps streaming through a Docker upgrade. The mount therefore has to be
re-resolved explicitly.

Three properties of that unit are deliberate.

**A plain `docker restart` is enough, and is what runs.** Docker rebuilds the
container's mount namespace on start. Measured with a scratch bind mount:

| step                             | host inode | container reads |
| -------------------------------- | ---------: | --------------- |
| initial                          |     411116 | 411116          |
| host file replaced (`rm`+create) |     411119 | 411116 ← stale  |
| `docker restart`                 |     411119 | 411119          |

`up -d --force-recreate` also works and is **not** used: that is the
create/remove capability ADR-0025 removed from this stack after Watchtower's
non-atomic recreate left qbittorrent deleted for 13 h. Restart is the
capability autoheal already holds under ADR-0010.

**It waits for the API rather than racing it**, because `docker.service` can
report started before it is accepting connections, and it ends in `|| true`
because on a first boot the containers may not exist yet.

**The repair cannot live in a container.** autoheal is dockerproxy's only
client and depends on it, so a dockerproxy healthcheck plus autoheal is
circular: when dockerproxy is broken, autoheal is exactly what cannot act. It
also does not live in `stack_watchdog.py`, which stays pure detection.

### 6. Mounting the socket's parent directory was rejected

The textbook fix for an inode detach is to mount the _directory_ instead, since
directory mounts resolve entries at lookup time. Here the socket's parent is
`/run` (and `/var/run` is a symlink to it), which on this host holds
`credentials`, `systemd/private`, `sudo`, `samba`, `user`, and the `lxd`,
`multipathd` and `rpcbind` sockets. Mounting that into dockerproxy to save one
`docker restart` is a plain hardening regression against ADR-0001 and against
ADR-0024's narrowing. A dedicated directory would need a second dockerd
listening socket via a `daemon.json`/systemd change — more host surface than
the unit above, for the same outcome.

### 7. The unit is in the repo, and `make verify-runtime` asserts it

ADR-0040's lockd pins live only on the host, so a rebuild has to reapply them
from memory. This one does not repeat that: the unit is `host/systemd/`,
installed by `make install-host-units`, and `make verify-runtime` fails when it
is missing, disabled, or drifted from the repo copy — alongside the live inode
comparison, so the check covers both the fault and the guard against it.

## Consequences

- Every `systemctl restart docker` and every Docker package upgrade detaches
  this mount. The resync unit now re-resolves it as part of the same restart,
  and the watchdog catches it within five minutes if the unit is ever missing.
- `dockerproxy` still has no healthcheck. Adding one would be circular —
  autoheal is its only client and depends on it — so the assertion lives
  outside both, in the watchdog.
- The autoheal healthcheck's stated purpose (ADR-0010: assert _reachability_
  of the Docker API, not the presence of a process) was right and did fire.
  What it could not do was name the cause, and `cannot exec in a stopped state`
  is what a healthcheck reports when the container it probes is losing a race
  with its own restarts.
