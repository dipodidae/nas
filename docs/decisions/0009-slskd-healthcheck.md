# ADR-0009 — slskd's healthcheck is deliberately Soulseek-INDEPENDENT

**Date:** 2026-09-01
**Status:** accepted
**Authority:** `scripts/slskd_login_watch.py` module docstring

## The trap

slskd's web server can be up while its Soulseek login is dead. It is tempting
to make the container healthcheck probe `isLoggedIn` so a logged-out slskd gets
restarted by autoheal. **That creates a permanent restart spiral.**

The login handshake times out after a hardcoded **5000 ms** when slsknet's
central server still holds a stale session for the username — the classic
"ghost session" after a fast restart. A restart re-presents the same username
immediately, re-collides with the ghost, and perpetuates the
**32 → 64 → 128 s** backoff. It never recovers; the container shows
"Up N minutes" forever.

**The only cure** is to leave slskd DOWN for **15–30 minutes** so slsknet reaps
its own stale session, then cold-start (`docker compose up -d slskd`). An
autoheal restart is the exact opposite of the cure.

## Decision

- The healthcheck is liveness-only: `wget --spider http://localhost:5030/`.
  It must **never** probe `isLoggedIn`.
- `autoheal=true` stays on slskd, but it therefore only restarts slskd if the
  **web server itself** is dead.
- Login state is watched separately and **alert-only** by
  `scripts/slskd_login_watch.py` (cron `*/15`), which never restarts or stops
  slskd. Exit codes: `0` logged in or within grace, `1` logged out beyond
  grace (alert raised), `2` fatal.

## Contradiction resolved (2026-09-02)

The old `docker-compose.yml` contained two comments that disagreed:

- on slskd's healthcheck: _"deliberately Soulseek-INDEPENDENT (just spiders the
  web UI)"_
- on autoheal: _"Used for slskd, whose Soulseek login can drop while its web
  server stays up (see slskd's **login-aware** healthcheck)"_

**The slskd comment was correct; the autoheal comment was stale and is now
fixed.** Settled from two independent pieces of evidence, not by preference:

1. The healthcheck command itself is a bare web-UI spider — there is no login
   probe in it.
2. `scripts/slskd_login_watch.py`'s docstring states the design directly:
   _"So the container healthcheck is deliberately liveness-only (web UI spider)
   and autoheal never restarts slskd for a login drop. This script is the
   separate, alert-only path… It NEVER restarts or stops slskd."_

The autoheal comment now says so, and points here.

## Do not reintroduce

A login-aware healthcheck on the autoheal path. Not for slskd, not for any
service whose recovery requires _staying down_.

## Amendment 2026-09-02 — the share scan, and the cache that made it repeat

The same failure shape as the login spiral, reached from a different direction:
an autoheal restart that cannot fix the condition and actively prevents
recovery.

slskd does not bind `:5030` until its startup share scan completes — the probe
gets `connection refused`, not a slow answer. The healthcheck was
`start_period: 120s` with `retries: 5` at `interval: 60s`, so the container was
marked unhealthy at roughly 9 minutes, autoheal restarted it, and the scan began
again at 0%. Measured: autoheal restarted slskd every ~9 minutes for over an
hour, each restart landing at 30-40% of the scan, and slskd was never once
reachable in that window.

### The first fix was wrong, and it is worth recording why

`start_period` was raised to **30m**, from a figure of "~18 min" that was
**extrapolated from the first 39% of a scan (7 minutes)**. That extrapolation
was badly wrong: the early directories were warm in the page cache and the rest
were cold. The real scan was still at **95% after 45 minutes**, so autoheal went
on killing it — at 30-minute intervals instead of 9. Better, still broken.

**Do not extrapolate this number. Read it off a completed run.**

### The root cause was not the timeout at all

`shares.cache.storage_mode` in `slskd.yml` was **`memory`**. The share cache was
therefore destroyed on every restart, and slskd redid the entire cold scan at
_every single start_. It also made the neighbouring `retention: 10080` ("rescan
weekly") meaningless — the cache never survived long enough to be retained.

Set to **`disk`**, the cache is persisted and loaded at startup, so only the
first start after the change pays for a cold scan. That removes the cause rather
than widening the tolerance for it, which is the same move ADR-0020 makes for
Watchtower.

`start_period` is now **90m**, sized for the cold case. It costs nothing in the
warm one: Docker ends `start_period` at the first _successful_ probe, so a fast
startup leaves the window immediately rather than staying blind inside it.

The accepted cost is that a genuinely dead slskd could go undetected for up to
90 minutes on a cold start — which this ADR already tolerates, since for slskd
staying down is usually the cure rather than the emergency, and
`scripts/stack_watchdog.py` still catches a _missing_ container every 5 minutes.

The general rule, which the next service inherits: **`start_period` is a
property of the slowest legitimate startup, not a round number — and if that
startup is slow because work is being repeated, fix the repetition first.**
