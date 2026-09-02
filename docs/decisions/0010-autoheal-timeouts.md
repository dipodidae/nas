# ADR-0010 — autoheal's timeouts must exceed the longest `stop_grace_period`

**Date:** 2026-09-01
**Status:** accepted
**Background:** `docs/qbittorrent-crash-fix.md` §E "autoheal — worse than
assumed"

## Context

autoheal restarts containers labelled `autoheal=true` — here qbittorrent and
slskd. It reaches Docker only through `dockerproxy` (ADR-0013).

It had been **stopped since 2026-07-29** — collateral from a bare
`docker compose stop`, not a decision about autoheal. Its entire log was
startup banners; it had never restarted anything. A stopped autoheal is
invisible, which is how it went unnoticed for a month.

## The two wrong timeouts

**`AUTOHEAL_DEFAULT_STOP_TIMEOUT`** defaulted to 10s and **ignores Compose's
`stop_grace_period` entirely** — the same trap `WATCHTOWER_TIMEOUT` exists to
close (ADR-0006). qbittorrent needs up to 120s to flush `torrents.db`; a 10s
SIGTERM→SIGKILL is precisely the ungraceful kill that leaves a stale lockfile
(ADR-0004, ADR-0005). Now **150**.

**`CURL_TIMEOUT`** must exceed the stop timeout. autoheal's restart call blocks
for the whole stop timeout, so a shorter curl timeout makes it log
`Restarting container ... failed` and **re-issue the restart every
`AUTOHEAL_INTERVAL` while the first one is still in flight**.

Verified 2026-09-01 with a deliberately-unhealthy probe: the restart succeeded
at exactly **t+150 s** while autoheal had already logged three failures and
fired **three overlapping requests**. For qbittorrent that pile-up is exactly
what leaves a stale lockfile. Now **180**.

## Invariant

```
AUTOHEAL_DEFAULT_STOP_TIMEOUT  >=  max(stop_grace_period in the stack)   # 120s
CURL_TIMEOUT                    >  AUTOHEAL_DEFAULT_STOP_TIMEOUT
```

Current values: 150 and 180.

## Operational note

autoheal has been running again since 2026-09-01. **If you are cycling
qbittorrent or slskd by hand, a container going unhealthy will be restarted out
from under you within ~30 s** (`AUTOHEAL_INTERVAL=30`). An unexplained restart
during testing is probably autoheal, not a crash — `docker logs autoheal` says
so explicitly.

To silence it for a service temporarily, **remove the `autoheal=true` label**
rather than stopping autoheal. A stopped autoheal is invisible.
