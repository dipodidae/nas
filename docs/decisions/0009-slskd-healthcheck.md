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

- on slskd's healthcheck: *"deliberately Soulseek-INDEPENDENT (just spiders the
  web UI)"*
- on autoheal: *"Used for slskd, whose Soulseek login can drop while its web
  server stays up (see slskd's **login-aware** healthcheck)"*

**The slskd comment was correct; the autoheal comment was stale and is now
fixed.** Settled from two independent pieces of evidence, not by preference:

1. The healthcheck command itself is a bare web-UI spider — there is no login
   probe in it.
2. `scripts/slskd_login_watch.py`'s docstring states the design directly:
   *"So the container healthcheck is deliberately liveness-only (web UI spider)
   and autoheal never restarts slskd for a login drop. This script is the
   separate, alert-only path… It NEVER restarts or stops slskd."*

The autoheal comment now says so, and points here.

## Do not reintroduce

A login-aware healthcheck on the autoheal path. Not for slskd, not for any
service whose recovery requires *staying down*.

## Amendment 2026-09-02 — `start_period` must outlast a full share scan

The same failure shape as the login spiral, reached from a different direction:
an autoheal restart that cannot fix the condition and actively prevents
recovery.

slskd does not bind `:5030` until its startup share scan completes — the probe
gets `connection refused`, not a slow answer. This library is 19,433 shared
directories and the scan takes about 18 minutes. The healthcheck was
`start_period: 120s` with `retries: 5` at `interval: 60s`, so the container was
marked unhealthy at roughly 9 minutes, autoheal restarted it, and the scan began
again at 0%. Measured on 2026-09-02: autoheal restarted slskd every ~9 minutes
for over an hour, each restart at 30-40% of the scan, and slskd was never once
reachable in that window.

`start_period` is now `30m` — the measurement plus headroom for a growing
library. The accepted cost is that a genuinely dead slskd goes undetected for
half an hour, which this ADR already tolerates: for slskd, staying down is
usually the cure rather than the emergency.

The general rule, which the next service inherits: **`start_period` is a
property of the slowest legitimate startup, not a round number.** A probe that
fires before the service can possibly answer converts autoheal into a treadmill.
