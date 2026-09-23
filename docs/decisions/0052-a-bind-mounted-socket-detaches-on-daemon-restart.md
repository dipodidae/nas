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

## Consequences

- Every `systemctl restart docker` and every Docker package upgrade detaches
  this mount. The watchdog now catches it within five minutes instead of three
  days.
- `dockerproxy` still has no healthcheck. Adding one would be circular —
  autoheal is its only client and depends on it — so the assertion lives
  outside both, in the watchdog.
- The autoheal healthcheck's stated purpose (ADR-0010: assert _reachability_
  of the Docker API, not the presence of a process) was right and did fire.
  What it could not do was name the cause, and `cannot exec in a stopped state`
  is what a healthcheck reports when the container it probes is losing a race
  with its own restarts.
