# ADR-0025 — Watchtower is retired, and dockerproxy narrows with it

**Date:** 2026-09-02
**Status:** accepted
**Supersedes:** ADR-0020 (which demoted it), ADR-0006 (whose per-service opt-outs it needed)
**Amends:** ADR-0013 (the exposed endpoint set)
**Depends on:** ADR-0024 — do not revert this without reverting that

## Decision

1. **Remove the `watchtower` service.** Nothing in this stack can stop, remove
   or recreate a container any more.
2. **Remove all 16 `com.centurylinklabs.watchtower.enable` labels.**
3. **Narrow `dockerproxy`** from eight endpoint groups to four:
   `CONTAINERS`, `POST`, `PING`, `VERSION`. Dropped: `IMAGES`, `NETWORKS`,
   `DELETE`, `INFO`.

## Why it no longer earns its place

ADR-0020 left watchtower running in monitor-only mode, where its only remaining
function was notification. ADR-0024 gives that function to `diun`, whose
coverage is a **strict superset**:

|                                         | watchtower (monitor-only) | diun (file provider)                 |
| --------------------------------------- | ------------------------- | ------------------------------------ |
| the 16 labelled `:latest` services      | yes                       | yes                                  |
| the 4 locally-built images              | label meaningless         | excluded by derivation               |
| pinned tags (`jellyfin`, `qbittorrent`) | **never reports**         | yes, semver-ranked                   |
| unlabelled containers                   | **never checked**         | irrelevant — reads the compose model |
| coverage asserted by `make check`       | no                        | yes, both directions                 |
| needs Docker API access                 | yes                       | **no**                               |

There is nothing watchtower reported that Diun does not. There is one thing it
could do that Diun cannot: stop, remove, and then fail to create a replacement.

## Correcting a common mis-statement about this stack

Watchtower **never held the Docker socket.** It reached the API through
`dockerproxy` over TCP, as ADR-0013 requires. Retiring it therefore removes no
socket mount — `make verify-runtime`'s socket assertion was already green and
stays green.

What it removes is a _privileged client_ of the proxy, and that is where the
real win is.

## The dockerproxy narrowing — the actual security gain

`IMAGES`, `NETWORKS` and `DELETE` were enabled **only** for watchtower's
recreate flow: `NETWORKS` so it could disconnect and reconnect containers on the
custom `nas-network`, `DELETE` so it could remove them. ADR-0013 says so
explicitly.

That recreate is not a hypothetical risk. It is the incident: a failed remove
logs `Failed=1` and moves on **without creating a replacement**, which
`restart: unless-stopped` cannot fix and `autoheal` cannot heal, because there
is nothing left to heal. It cost 13 h of qBittorrent on 2026-09-01 and 7 days on
2026-08-19, and it recreated `qui` as recently as 04:01 on 2026-09-02.

So narrowing the proxy is not tidying an unused grant. **It removes the
capability that caused the incident.** Nothing left in the stack can delete a
container through the Docker API.

`INFO` goes too — nothing used it.

### Verified by probe, not by reasoning

The brief was explicit: do not prove autoheal still works by reasoning about
which flags it needs. So, on 2026-09-02, against the narrowed set:

- a disposable `canary` container with a healthcheck of `exit 1`;
- a **dedicated** autoheal instance watching `canaryheal=true`, pointed at the
  narrowed `dockerproxy`.

Result — the canary was restarted three times, `StartedAt` advancing
`17:45:47 → 17:46:23 → 17:47:03 → 17:47:43`, with autoheal logging
`Container /canary found to be unhealthy - Restarting container now`. Both were
then deleted.

The endpoint matrix through the same proxy:

```
/_ping           -> 200      /images/json  -> 403
/version         -> 200      /networks     -> 403
/containers/json -> 200      /info         -> 403
                              /exec         -> 403
```

**Why a dedicated autoheal with its own label rather than the real one:** the
real `autoheal` watches `slskd`, and slskd was mid share-scan at the time. A
restart there costs 15–30 minutes of Soulseek downtime that you do not get back
(ADR-0009) and would have restarted a 2-hour scan. A second instance of the same
image against the same proxy exercises the identical code path, so it proves the
same thing at none of the cost. Do it this way next time too.

## What is lost

Nothing that was still being used. Unattended patching was already gone with
ADR-0020's monitor-only. The digest-change signal for `:latest` services moves
to `DIUN_WATCH_COMPAREDIGEST=true`.

`WATCHTOWER_SCHEDULE` in `.env` becomes dead and is removed. The 04:30
`post_update_verifier` cron kept its slot but no longer follows a 04:00 update
run — it is now a daily stack-health assertion, and its comment says so.

## What replaces the labels

The labels are gone rather than kept as documentation, because a label that
controls nothing while reading as policy is the exact class of lie this repo's
assertions exist to prevent. `make check` now asserts that **no** service
carries one.

The reasoning they encoded is not lost. It moves to `MANUAL_UPDATE_ONLY` in
`scripts/check-invariants.sh`, which records _which services a human must update
deliberately and why_, and asserts that each is still **pinned** — because
nothing auto-updates now, but `docker compose pull` still follows a moving tag,
and the pin is what makes the update a decision.

## Reverting

`git revert` this commit and watchtower comes back with its labels and the wider
proxy. Do **not** revert it without also reverting ADR-0024's commit, or the
stack has no update notification at all: watchtower cannot report the pinned
tags, which is the whole reason this sequence exists.
