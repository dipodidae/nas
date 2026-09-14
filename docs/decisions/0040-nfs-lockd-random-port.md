# ADR-0040 — NFS's lock manager takes a random port, and one boot it took Jellyfin's

**Date:** 2026-09-14
**Status:** accepted
**Same shape as:** ADR-0039, ADR-0035 — the compose model was correct and the healthcheck
was green; the cause was in a layer neither can see

## What was broken

`jellyfin.4eva.me` served **502 Bad Gateway** for about 20 hours after a host reboot.
Nothing in this repo had changed.

## The cause

The kernel NFS lock manager (`lockd`, registered with rpcbind as `nlockmgr`) binds a
**random** UDP/TCP port at boot when `fs.nfs.nlm_udpport` / `nlm_tcpport` are `0`, which is
the default. This host exports three read-only media shares, so `lockd` runs.

On the 18:02 boot it drew **7359** — Jellyfin's client-discovery port. `lockd` won because
it starts with NFS, long before Docker gets to the containers:

```
18:03:41 warning  Failed to allocate port  error="failed to bind host port 0.0.0.0:7359/udp: address already in use"
18:03:41 warning  Failed to reserve existing port mapping for endpoint 8af0080: …address already in use
18:03:44 error    failed to start container … error="failed to set up container networking:
                  driver failed programming external connectivity on endpoint jellyfin:
                  failed to bind host port 0.0.0.0:7359/udp: address already in use"
```

`rpcinfo` is what names the culprit, and it needs no root:

```
$ rpcinfo -p localhost | grep nlockmgr
    100021    1   udp   7359  nlockmgr
    100021    3   udp   7359  nlockmgr
    100021    4   udp   7359  nlockmgr
```

**It is a random draw, so it is a lottery run at every boot** against every port this stack
publishes — 8096, 8920, 1900, 6881, 50300 and the rest. It had simply never come up before.

## Why it looked like a Docker bug for so long

Every cheap signal pointed the wrong way, and the investigation followed them:

- `ss -ulpn` showed `0.0.0.0:7359` bound with **no process name**, because `lockd` is a
  kernel thread holding a kernel socket — there is no userspace fd, so it appears in
  neither `ss` output without root, nor `/proc/<pid>/fd`, nor `lsof`.
- There was **no `docker-proxy`** for 7359, which is exactly what a leaked Docker socket
  would also look like.
- `docker compose stop`, then `rm -f`, then `up -d` all left the port bound **with no
  container in existence at all**.
- It survived `systemctl restart docker` **with the same socket inode (23718)** — which is
  the observation that actually disproves the Docker theory, and the point at which
  `rpcinfo -p` should have been the first command rather than the tenth.

**Lesson: a host port bound with no owning process is a kernel socket, not a leak.** Check
`rpcinfo -p localhost` before blaming the daemon.

## The second trap: a container that is `running` with no network

Once diagnosed, one `docker compose up -d jellyfin` **reported success** —
`Container jellyfin Started` — and produced a container that was `running`,
`health=healthy`, and attached to **nothing**:

```
$ docker port jellyfin                                     # empty
$ docker inspect jellyfin -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
                                                           # empty
$ docker exec swag getent hosts jellyfin                    # empty
```

This is because the container was left in `created` with a half-built sandbox by the
failed boot-time start, and `up -d` merely **starts** an existing container — it does not
rebuild its network. The green healthcheck is the trap: Jellyfin's is
`curl -f http://localhost:8096/…` **from inside the container**, which cannot fail for lack
of a network. So the container said healthy, `compose ps` said Up, and SWAG still 502'd,
because SWAG resolves `jellyfin:8096` over `nas-network` DNS and no such name existed.

**A green healthcheck is not evidence of reachability**, and after a failed start the fix
is `stop && rm -f && up -d --force-recreate`, not `up -d`.

## The fix

`lockd` is pinned to **4045** (the traditional NFS lock-manager port, unused by this
stack), in two places because either can apply first:

- `/etc/modprobe.d/lockd.conf` — `options lockd nlm_udpport=4045 nlm_tcpport=4045`, for a
  cold boot where the module loads fresh.
- `/etc/sysctl.d/30-nfs-lockd-ports.conf` — `fs.nfs.nlm_udpport` / `nlm_tcpport`, for the
  case where `lockd` is already loaded.

Applied with `sysctl -p` + `systemctl restart nfs-server`, then verified:

```
$ rpcinfo -p localhost | grep nlockmgr
    100021    1   udp   4045  nlockmgr      # was 7359
$ ss -ulpn | grep ':7359'                   # empty
```

**These two files live outside this repo** — they are host config, in the ADR-0035 /
`nas-runtime-vs-repo` category of things `make check` structurally cannot see. A host
rebuild must reapply them; that is what this ADR is for.

## Consequences

- The Jellyfin ports stay exactly as documented: `8096/8920/7359/1900`. Dropping the
  `7359:7359/udp` publish would have started Jellyfin and silently killed LAN client
  auto-discovery for as long as nobody re-read the compose file. Explicitly rejected.
- **`stack_watchdog.py` caught the outage and bounded it**: `container:jellyfin:down →
exited, exit=255`, escalated `nas-infra` → `nas-critical` on the third cycle per
  ADR-0033. The 20 h was response time, not detection. No change needed.
- **`check_stuck_starting` was wrong and is fixed.** Docker freezes `Health.Status` at its
  last value when a container dies, so a container that exited during `start_period`
  reports `starting` **forever** while `StartedAt` keeps ageing. Jellyfin, dead 20 h,
  emitted `health=starting for 1286 min` — a warning that grew every cycle and escalated to
  `nas-critical` beside its own `:down` alert, adding noise to exactly the incident it
  should have stayed quiet for. It now requires `State.Status == "running"`: `:down` owns a
  dead container, `stuck-starting` owns a live one that is not progressing. Regression
  tests in `scripts/tests/test_stack_watchdog.py`.

## If a published port is ever "already in use" again

```bash
rpcinfo -p localhost | grep -E "$(PORT)"    # kernel RPC service holding it?
ss -ulpn | grep ":$(PORT)"                  # bound with no process -> kernel socket
ps aux | grep '[d]ocker-proxy' | grep $(PORT)
```

If `rpcinfo` names it, the port moved because `lockd`/`mountd`/`statd` drew it at random —
`mountd` and `statd` are still unpinned here and can do the same thing. Pin the offender
the way `lockd` is pinned above rather than moving the container's port.
