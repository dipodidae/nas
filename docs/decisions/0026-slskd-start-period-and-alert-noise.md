# ADR-0026 — slskd's start_period must outlast a forced rescan, and a startup must not page anyone

**Date:** 2026-09-02
**Status:** accepted
**Extends:** ADR-0009 (slskd's healthcheck and the autoheal restart spiral)
**Related:** ADR-0010, ADR-0012

## The incident, caught 19 minutes before it started

At 19:35 on 2026-09-02, `lidarr-backlog-drip` pushed a priority-5 alert:

```
exit 2 after 0s
ERROR: slskd unreachable at localhost:5030: [Errno 104] Connection reset by peer
```

The alert was real but its diagnosis was wrong, and the underlying situation was
worse than the alert suggested.

**What was actually happening.** slskd had been recreated at 18:24 and was
running a **forced full share rescan**. It has no HTTP listener at all during
that: `/proc/net/tcp6` inside the container was empty, and `/proc/net/tcp` held
only Docker's DNS resolver — verified against a control (`lidarr` showed its
`:8686` listener in the same read, so the method was sound). Everything that
talks to slskd therefore got a connection reset.

**Why it was about to become permanent.** slskd's `start_period` was 90m,
expiring at 19:54:48. Five failed probes later, at ~19:59:48, the container
would be marked `unhealthy`, and `autoheal` restarts unhealthy containers within
30 s. The scan would not finish until 20:17. So autoheal was ~19 minutes from
restarting slskd mid-scan — and that restart does not merely waste the work:

```
Share cache successfully restored from backup
Error initializing shares: Previous share scan was marked as suspect
Performing a forced re-scan of shares
```

An **interrupted scan records itself as suspect**, and the next start then
force-rescans _even though the on-disk cache restored perfectly_. So
restart → suspect → full rescan → 90m expires mid-scan → restart is a closed
loop in which slskd is never up again. This is the same loop ADR-0009's compose
comment already describes, reached by a different door.

**What was done.** `autoheal` was stopped by hand — the only non-destructive
lever, because labels are immutable and changing one needs the recreate we were
trying to avoid. AGENTS.md prefers removing the `autoheal=true` label for this
reason; that option did not exist here, and the note now says so. The scan was
allowed to finish, which wrote a **valid, non-suspect** cache; slskd came back
at 20:17 with its listener bound and logged in to Soulseek. It was never
restarted, so no ghost session (ADR-0009) was created.

`stack_watchdog.py` alerted twice about the stopped autoheal, every five
minutes, exactly as designed — which is what makes stopping it an acceptable
move rather than the invisible month-long absence of 2026-07-29.

## Fix 1 — `start_period: 90m` → `4h`

The number is measured, from slskd's own log rather than an extrapolation:

```
Scan found 194358 files (and 1 were filtered) in 6787113ms
```

**6,787,113 ms = 113 minutes** for 19,433 directories / 194,358 files. The 90m
window expired with the scan at **92%** — it was 23 minutes short.

This is the third time this value has been wrong, and the pattern is worth
naming: the stock `120s + 5×60s` failed at ~9 min; `30m` failed because it was
extrapolated from the first 39% while those directories were still warm in page
cache; `90m` was measured correctly off a completed run and then **the share
grew past it**. So the failure mode is not carelessness, it is that a
measurement of a growing thing expires. 4h is 2.1× the current measurement,
which buys room, and it costs nothing warm: Docker ends `start_period` at the
first successful probe, so a fast start leaves the window immediately.

`make check` now asserts it against a declared floor with the measurement
attached (`START_PERIOD_FLOOR`), so the next person raising it has the number in
front of them — and the rule stays: **read it off a completed run, never
extrapolate.**

### The trade-off, and why it is watched rather than avoided

A long `start_period` is a long **blind** window: Docker counts no failures
inside it, so a genuinely dead web server is indistinguishable from a slow one,
and both `autoheal` and `make verify-runtime` stay quiet for up to four hours.

That is covered by **observation, not action** — the ADR-0009 pattern.
`scripts/stack_watchdog.py` gained `check_stuck_starting()`, which alerts when
any container has been `health=starting` past `--starting-max-min` (default
150 min) and **never restarts anything**. For slskd a restart is the one thing
that makes the situation strictly worse, so the watchdog tells a human and stops
there.

## Fix 2 — a routine startup must not page anyone

Every slskd-dependent cron job exited **2** for the whole two-hour window, and
`cron_job.py` treats 2 as fatal, so a routine, expected, self-resolving startup
produced priority-5 alerts with a skull on them at every cron tick.

**That is worse than no alerting**, because it trains you to swipe the
notification away — and this stack exists precisely because things went
unnoticed. So the distinction is made rather than the noise muted:

`scripts/slskd_state.py` answers "is slskd unreachable because it is broken, or
because it is still coming up?", and the affected scripts return **1** (partial,
inside `cron_job.py`'s `--ok-codes 0,1` default) instead of 2 while it is
initializing. The log still records it; a genuinely broken slskd still exits 2
and still shouts.

**Deliberately not** `--ok-codes 0,2` or a wider tolerance on those jobs: that
would silence the real failure too. The point is to distinguish, not to mute.

### Two signals, because each has a hole

`is_initializing()` accepts _either_ `health=starting` _or_ a share-scan line in
the recent container log:

- **`health=starting` alone is insufficient** — and this incident proves it. The
  window expired while the scan was still running at 92%, so at the moment it
  mattered most the container reported `unhealthy`, not `starting`. Verified
  live: with slskd at 94% and `health=unhealthy`, the helper still correctly
  answered "initializing" because the scan log said so.
- **the scan log alone is insufficient** — a container up for days with a stale
  scan line in its last 40 lines is not starting.

Either signal is accepted, because a false "initializing" costs one downgraded
alert on a job that reruns in minutes, while a false "broken" costs a restart
that makes it permanently worse. It reads the _container log_ rather than the
API on purpose: the API is exactly what is unavailable, so asking the thing
that is down whether it is starting cannot work.

Applied to `lidarr_backlog_drip.py` and `slskd_login_watch.py`, the two on short
cron windows that actually fired. The other slskd-dependent scripts
(`slskd_cleanup.py`, `slskd_complete_sweep.py`, `process_soulseek_imports.py`,
`slskd_incomplete_sweep.py`, `lidarr_stuck_download_reaper.py`) run hourly or
daily and are flock-serialised; they should adopt the same helper, and that is
recorded as outstanding rather than claimed as done.

## One thing deliberately left pending

The `4h` value is in the compose file and asserted by `make check`, but the
**running** slskd container still carries the old 90m, because applying it needs
a recreate and slskd had just logged in to Soulseek. A fast restart there risks
the ghost-session spiral whose only cure is leaving it down 15–30 minutes
(ADR-0009), and there is no urgency: the value only matters at the _next_ cold
start. It takes effect on the next natural recreate.

If slskd must be recreated before then, do it deliberately and expect a fast
start — the cache is now valid and non-suspect.

## Verification

```
scripts/slskd_login_watch.py    exit=1   (was 2)
scripts/lidarr_backlog_drip.py  exit=1   (was 2)
cron_job.py wrapping either     exit=0   -> no ntfy push
```

`make check` → `start-period-floor  slskd 14400s >= 14400s`, and the assertion
was seen to FAIL at the old 5400s before the change. The three standing alerts
(`autoheal:down`, `container:autoheal:down`, `container:slskd:unhealthy`) all
reported `[RESOLVED]` once autoheal was restarted against a healthy slskd.
