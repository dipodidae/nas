# qBittorrent: recurring crash with no automatic recovery

**Date:** 2026-09-01
**Commit:** `9c13b9e fix(qbittorrent): grant CAP_KILL so shutdown is actually graceful`
**Status:** fixed and verified; see [Known risks](#known-risks) for what is still open.
**Related:** `docs/jellyfin-playback-audit.md` — the other half of the OOM story,
and required reading before touching Jellyfin memory.

## Symptom

qBittorrent died regularly. After a crash — or after any `docker compose up -d`
recreate — it would not come back on its own. `restart: unless-stopped` did not
help, autoheal did not help, and a human had to start it by hand.

The last occurrence before this fix: down from **04:04 to 17:38 local on
2026-09-01**, 13.5 hours, until it was started manually.

## Root cause

**The container could never shut down cleanly, and everything else followed from
that.** The stale lockfile that gets blamed for this class of problem was a
symptom, not the cause.

`cap_drop: ALL` removed `CAP_KILL`. s6-overlay runs as root and has to signal
`qbittorrent-nox`, which runs as `abc` (uid 1000). Signalling a process owned by
a different uid requires `CAP_KILL`. Without it the kernel refuses with `EPERM`:

```
$ docker exec -u 0 qbittorrent kill -TERM 162
sh: can't kill pid 162: Operation not permitted
```

`CapEff` was `0x00000000000000c3` — CHOWN(0), DAC_OVERRIDE(1), SETGID(6),
SETUID(7). Bit 5 (`CAP_KILL`) absent.

So on every stop: s6 never forwarded the SIGTERM, Docker waited out the entire
`stop_grace_period`, then SIGKILLed. qBittorrent was killed mid-flight every
single time, which is what orphaned the lockfile and the ipc-socket, and what
left `torrents.db` unflushed.

Measured on this box:

| | stop time | shutdown logged | residue |
|---|---|---|---|
| without `CAP_KILL` | **120.3 s** | nothing | lockfile + ipc-socket orphaned |
| with `CAP_KILL` | **6.0 s** | `Saving resume data completed.` | qbit removes both itself |

`stop_grace_period: 120s` had never once been used gracefully. It was a
120-second delay in front of a guaranteed hard kill.

## What was and wasn't wrong

Five hypotheses were investigated. Two were real, one was real but already
patched upstream, one was the opposite of what was assumed, and one was worse
than assumed. A sixth cause was not in the original theory at all.

### A. Stale lockfile — real, but not the version bug

Upstream has two relevant bugs:

- [#24164](https://github.com/qbittorrent/qBittorrent/issues/24164) — a 0-byte
  5.1.x lockfile makes 5.2.0 abort. Fixed in 5.2.1 (PR #24218).
- [#24357](https://github.com/qbittorrent/qBittorrent/issues/24357) — the new
  5-line lockfile records the container hostname, which changes on every Docker
  recreate, so `QLockFile` cannot prove the lock is stale and refuses to start.
  Fixed in 5.2.2 (PR #24363).

**This box runs 5.2.3 / libtorrent 2.0.14, so both were already fixed.** The
lockfile was still real, though: the s6 crash-loop signature is in the rotated
logs at **2026-08-08 15:57:47 → 15:58:07**, PIDs climbing 899 → 1059 by 8 per
second, then resolving. That is
[#24405](https://github.com/qbittorrent/qBittorrent/issues/24405)'s tell.

Note the gap this exposes: `custom-cont-init.d` runs **once per container
start**. If qbit dies and s6 restarts the *service* inside a still-running
container, the init script does not re-run and cannot clear anything.

### B. libtorrent memory — real and severe

libtorrent 2.x mmaps torrent data; Linux keeps those pages as page cache, and
Docker `mem_limit` does not contain it because the pressure is in the kernel's
cache rather than the app's cgroup accounting.

Both disk IO modes were set to `1` (Enable OS cache). The container's cgroup
**peaked at 21.1 GB** (from journald's scope accounting on teardown), and the
host took four **global** (`CONSTRAINT_NONE`) OOM kills in two days:

```
Aug 31 10:10:40  Killed process (jellyfin) anon-rss:23843220kB
Aug 31 19:40:40  Killed process (jellyfin) anon-rss:23523020kB
Aug 31 21:57:16  Killed process (jellyfin) anon-rss:22763820kB
Sep 01 05:34:58  Killed process (jellyfin) anon-rss:23483348kB
```

These were host-wide, not cgroup-constrained, so qBittorrent was a
co-contributor rather than a bystander.

**The Jellyfin side of this is solved — and `Sep 01 05:34:58` above is the last
kernel OOM kill on this box.** None since. It was never a Jellyfin application
leak: two heap dumps showed the managed .NET heap flat at 226–235 MB while
`anon` swung 217 MB → 1.46 GB → 573 MB. The ~23 GB was two independent *native*
mechanisms, both now mitigated in `docker-compose.yml`:

1. **glibc malloc arena fragmentation** (dotnet/runtime#122027) — glibc sizes
   its arena count from the *host's* core count, not the cgroup quota, so
   `mem_limit` cannot restrain it. `MALLOC_ARENA_MAX=2` cut peak from >2.08 GB
   to 1.56 GB on identical load, and finished in under half the time.
2. **`memfd:doublemapper` accumulation** — .NET's W^X double-mapping of JIT'd
   code (dotnet/runtime#89776, #121455), >1,000 mappings at a fresh restart and
   growing independently of the arena cap. `DOTNET_EnableWriteXorExecute=0`
   takes it to zero at no time cost.

Full eight-pass record in `docs/jellyfin-playback-audit.md`; **read that before
touching anything in this area** — several plausible-sounding causes are ruled
out there with evidence (hardware transcoding, ffprobe, the #16729 realtime
monitor cascade, a managed heap leak) and should not be revisited without new
data. Note also its measurement lesson, which applies directly to the 21.1 GB
figure below: `memory.current`/`mem_peak` include page cache, and the
OOM-relevant number is `anon`.

The "Physical memory usage limit" option was removed in 5.2.0, so it is not the
lever. The lever is `DiskIOReadMode` / `DiskIOWriteMode`.

### C. Ungraceful shutdown — real, and the actual root cause

See [Root cause](#root-cause) above.

### D. Init script not running — **false**, the opposite of the assumption

LSIO does print the ownership warning:

```
║        Some of the contents of the folder /custom-cont-init.d           ║
║            are not owned by root, which is a security risk.             ║
```

…but it is **advisory only on this image**. The script demonstrably ran, and
demonstrably worked:

```
[custom-init] Files found, executing
[custom-init] 01-clear-stale-lockfile.sh: executing...
[init-qbit-lockfile] removing stale lockfile: /config/qBittorrent/lockfile
```

The real defect was different: the script did an **unconditional `rm -f`**. That
is safe for one container over one config dir, but it silently destroys the
protection the lock exists to provide if two containers are ever pointed at the
same `/config`. It also only checked two of the three known lockfile paths and
ignored the ipc-socket entirely.

### E. autoheal — worse than assumed

autoheal was not merely unlabelled. It was **dead since 2026-07-29** — exit code
6, `RestartCount=5`, permanently capped out by the `on-failure:5` policy that
`/usr/local/sbin/nas-restart-guard.sh` re-applies every 15 minutes via
`docker update`.

That guard script only *warns* to the journal and never restarts anything:

```
nas-restart-guard[3355817]: WARNING: autoheal is not running (capped-out crash loop? ...)
```

So the stack had no self-healing at all for five weeks, silently.

### F. Watchtower — not in the original theory, and the reason it never came back

qBittorrent carried `com.centurylinklabs.watchtower.enable=true`, so watchtower
recreated it nightly at 04:00 — and recreate is precisely the operation that
broke it. On 2026-09-01 the recreate failed outright:

```
04:01:50  Stopping /qbittorrent (3170efaf0058) with SIGTERM
04:04:30  Container failed to exit within 10s of kill - trying direct SIGKILL
04:04:32  cannot remove container: could not kill container:
          tried to kill container, but did not receive an exit event
04:04:32  Session done  Failed=1  Scanned=17  Updated=0
```

Watchtower's stop → remove → create is **not atomic**. The remove failed, it
logged `Failed=1`, and it moved on **without creating a replacement**.
qBittorrent simply ceased to exist. `restart: unless-stopped` cannot help when
there is no container left to restart, and autoheal cannot heal a container that
is not there.

`WATCHTOWER_TIMEOUT=150s` was already set and live — it was not enough, because
the process needed ~180 s to die while flushing 21 GB of page cache (that
container scope recorded 249 GB written to disk over its life).

**This generalises past qBittorrent.** The stop → remove → create sequence is
non-atomic for every one of the 17 containers watchtower scans; any of them can
be left deleted with no replacement. Removing the label fixes qbit and nothing
else. The class is covered by `scripts/stack_watchdog.py`, which enumerates
`docker compose config --services` and raises a `critical` alert when a defined
service has no container at all — not merely when one is unhealthy. That
distinction is the whole point: "unhealthy" and "does not exist" look identical
to anything that only inspects running containers.

## What changed

All in `docker-compose.yml` (qbittorrent service only) and the init script.

| Change | Why |
|---|---|
| `cap_add: KILL` | **The fix.** Lets s6 signal the uid-1000 process. Verified minimal — FOWNER/FSETID were tested as unnecessary and are deliberately not granted. |
| `image:` pinned to `5.2.3_v2.0.14-ls473` | Floor of ≥ 5.2.2 for #24357. Verified present in the registry (HTTP 200) before committing. |
| watchtower label **removed** | Kills the nightly recreate trigger from cause F. Updates are now a deliberate tag bump. |
| `hostname: qbittorrent` | Stable across recreates, so the lockfile's hostname line is meaningful instead of a random container ID. |
| init script rewritten | Proves staleness before deleting; checks all three lockfile paths; clears the ipc-socket. |
| `mem_limit: 4g` + `memswap_limit: 4g` | Backstop for cause B. Scoped exception to the no-mem_limit policy, same pattern as the jellyfin block. |
| `DiskIOReadMode` / `DiskIOWriteMode` = `DisableOSCache` | The actual fix for cause B. Lives in `qBittorrent.conf`, not the repo. |
| dropped `QBITTORRENT_USER` / `QBITTORRENT_PASS` | LSIO never read them ([#228](https://github.com/linuxserver/docker-qbittorrent/issues/228), closed as not planned); they only leaked credentials into `docker inspect`. Still in `.env` for `scripts/`. |
| added `TORRENTING_PORT=6881` | Supported by LSIO, matches the published port. |
| healthcheck `--max-time 10` | `GET /` was measured returning **200** unauthenticated on 5.2.3, so `curl -f` was already correct; only the timeout bound was missing. |
| `autoheal=true` label | Backstop only. autoheal itself was restarted. |

### The init script's staleness rules

A lock is deleted only when it can be **proven** stale:

1. the file is 0 bytes → pre-5.2.0 format (#24164)
2. the hostname line ≠ this container's hostname → written elsewhere (#24357)
3. the recorded PID is not a live `qbittorrent-nox`

Otherwise it is left alone and qBittorrent decides for itself.

Pinning the hostname makes rule 2 stop firing, which makes **rule 3
load-bearing** — that is the rule that actually fires on this box now.

### The two-container safety property is NOT proven

This needs stating plainly, because the operating notes below tell you to trust
the script's judgement.

The rewrite was motivated by preserving the lock when a live instance holds it.
That property holds **within one container** and is tested there. It does **not**
hold across two containers sharing one `/config`, and pinning the hostname is
what breaks it:

- PIDs in the lockfile are **namespace-local**. A PID written by container A is
  meaningless when read inside container B.
- With `hostname: qbittorrent` pinned, both containers present the same
  hostname, so rule 2 (the only cross-host signal) never fires.
- Container B therefore sees a matching hostname and a PID that means nothing in
  its own namespace, concludes "not a live `qbittorrent-nox`", and **deletes a
  live peer's lock**.

The standalone unit test passes because it is single-namespace, which is exactly
the case that cannot expose this.

This is not a regression introduced here — upstream's own
PID + hostname + machine-id check has the same hole, and the previous
unconditional `rm -f` was strictly worse (it deleted the lock unconditionally in
*every* case, including single-container). The rewrite is still an improvement.
But "a lock held by a live instance is preserved" is true for one container and
unproven for two, and the machine-id line is the only field that could
distinguish them — it is currently read by neither upstream nor this script.

**Practical upshot:** never point two containers at one qBittorrent `/config`.
That was always true; it is just no longer defended against.

## Verification

| # | Test | Result |
|---|---|---|
| 1 | Clean start from stopped | healthy in **10 s**, WebUI HTTP 200 |
| 2 | `time docker compose stop` | **5.98 s**, `Saving resume data completed.`, no lockfile, no ipc-socket |
| 3 | 3× `up -d --force-recreate` | healthy in 5–10 s each; container ID changed every time, hostname stayed `qbittorrent` |
| 4 | `docker kill -s SIGKILL` → `up -d` | recovered **unattended** (see below) |
| 5 | lockfile hostname | reads `qbittorrent`, not a container ID |
| 6 | torrent count | **128 / 128**, 0 errored |

Test 4 is the one that matters, and the init script logged exactly the expected
rule:

```
[init-qbit-lockfile] removing stale lock /config/qBittorrent/lockfile: recorded PID 162 is not a live qbittorrent-nox
[init-qbit-lockfile] removing stale ipc-socket /config/qBittorrent/ipc-socket
[init-qbit-lockfile] cleared 1 stale lock(s)
```

The script's five decision paths were also unit-tested standalone against
synthetic lockfiles, including the safety case: a lock held by a live
`qbittorrent-nox` is preserved. Note that this test is **single-namespace**, so
it proves the single-container case only — see
[The two-container safety property is NOT proven](#the-two-container-safety-property-is-not-proven).

Torrent count was 127 at the start of the session and 128 at the end; the *arr
stack added one mid-session. Nothing was lost.

### Backups taken before any change

```
/home/tom/nas/.docker-config/qbittorrent.bak.1788278865                        (41M, 164/164 files)
/home/tom/nas/.docker-config/qbittorrent/qBittorrent/qBittorrent.conf.bak.1788279295
```

Safe to delete once you are satisfied the fix holds.

## Known risks

- **The 21.1 GB figure is cgroup accounting, which counts page cache.** Anon RSS
  could not be separated retroactively for a dead container, so "qbit
  contributed to the OOM" is a strong inference, not a direct measurement. The
  remedy is correct either way.
- **`mem_limit: 4g` — the OOM risk is smaller than it looks, and the real risk
  is elsewhere.** A cgroup reclaims file-backed pages before it OOM-kills, so
  the cache-driven growth that produced the 21.1 GB figure will *not* trip the
  limit; it will just cap the cache. What can OOM-kill is anonymous growth, and
  that sits at ~78 MiB steady state (with `memswap_limit == mem_limit` there is
  no swap to spill into either). So 4 g is comfortable. The actual exposure is
  the other direction: capping cache at 4 g while libtorrent 2.x is mmap-heavy
  can stall on writeback under load. **That shows up as degraded torrent
  throughput, not as a crash** — so watch throughput, not just the OOM counter.
- **qBittorrent no longer auto-updates.** Deliberate. Bumping the tag is now a
  manual step.
- **`nas-restart-guard`'s `on-failure:5` cap should be fixed, not just
  monitored.** A supervisor that gives up permanently is worse than none: it is
  what killed autoheal silently for five weeks. Origin: it was installed by
  `/home/tom/fix-nas-all.sh` on 2026-06-15 after a hard freeze, as blast-radius
  control for a watchtower crash loop that the *same script* root-caused and
  fixed (`WATCHTOWER_ROLLING_RESTART` was incompatible with the stack's
  dependencies). So the cap outlived the loop it was capping. That script's own
  item 4 shows the author knew a permanent cap needs a "notice it disappeared"
  companion and built one — as a journal warning, which nothing reads.
  `scripts/stack_watchdog.py` now supplies that function properly, which is the
  precondition for relaxing the cap. Suggested shape: keep the cap as the
  default, exclude `autoheal` and `watchtower` (the two self-healing containers)
  and give them `unless-stopped`. The restart-storm goal and `unless-stopped` on
  those two are not in conflict.
- **The fix is unproven against the real failure.** It was reproduced by
  simulation (SIGKILL) rather than by waiting for a natural watchtower recreate.
  Removing the watchtower label should mean that trigger never fires again, but
  the next few days are the real test.

## Operating notes

**Updating qBittorrent** is now deliberate:

```bash
# check what's current, then edit the image: tag in docker-compose.yml
docker compose up -d qbittorrent
docker inspect qbittorrent --format '{{.State.Health.Status}}'
```

Watch it come back healthy before walking away.

**Re-measure shutdown** if the torrent count grows a lot:

```bash
time docker compose stop qbittorrent
```

Raise `stop_grace_period` only if the measured time approaches it. At 128
torrents it is ~6 s against a 120 s budget.

**If it ever fails to start again**, the first thing to check is whether the
init script fired and what it decided:

```bash
docker compose logs qbittorrent | grep init-qbit-lockfile
```

**Do not** reintroduce an unconditional `rm -f` of the lockfile. If the script
says it is keeping a lock, that is the safety property working — find out why a
live `qbittorrent-nox` is holding it. Conversely, if it says it is *removing* a
lock, that judgement is only trustworthy while exactly one container is using
this `/config`; see the caveat above.

## Stale documentation

The qBittorrent bullet in `CLAUDE.md` predates this work and is now wrong in two
places: it states the problem "is NOT an ownership/permissions problem" (it was
exactly a capability problem), and it describes the old unconditional-delete
init script. It should be rewritten to point here.
