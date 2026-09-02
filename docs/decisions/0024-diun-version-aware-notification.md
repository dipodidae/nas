# ADR-0024 — Diun closes the update-notification gap watchtower cannot

**Date:** 2026-09-02
**Status:** accepted
**Closes:** the "no update notification for `jellyfin` or `qbittorrent`" gap
**Related:** ADR-0006, ADR-0012, ADR-0014, ADR-0020, ADR-0022, ADR-0025

## The gap, restated precisely

ADR-0020 demoted watchtower to monitor-only and named what that could not fix:

> Watchtower reports updates for the tag a container was started from. A
> **pinned** tag never reports an update. An **unlabelled** container is never
> checked.

`jellyfin` and `qbittorrent` are both pinned _and_ unlabelled, so the two
services whose updates most need to be chosen deliberately got no notification
at all — and relabelling them would not have helped, because the pin silences
them anyway.

## Decision

Add `diun` (pinned `crazymax/diun:4.33.0`) using the **file provider**, watching
a manifest generated from the compose model, notifying the existing `nas-alerts`
ntfy topic.

## Why the file provider and not the docker provider

This is the whole point, not a detail. Diun's **docker** provider reads the tag
a running container was started from — i.e. it would reproduce watchtower's
blind spot exactly, and this record would close nothing. The **file** provider
watches a _repository_ and enumerates its tags, which is what makes a pinned tag
visible: it can answer "what is newer than 10.11.11" rather than "has
10.11.11 moved".

Consequence worth stating: **diun has no route to the Docker API at all**, not
even through `dockerproxy`. It never touches a container. `dockerproxy`'s
exposed endpoint set is unchanged by this record (ADR-0025 narrows it).

## Why the manifest is generated, and asserted

`scripts/emit_diun_manifest.py` walks `docker compose config` and writes
`diun/manifest.yml`, tracked in-repo and bind-mounted read-only — the same
reasoning as the SWAG proxy-confs (ADR-0022).

A hand-maintained list would drift the moment a service was added, and **drift
in a notification config is the worst kind of drift**: nothing tells you that the
thing that was supposed to tell you has stopped covering something. So
`make check` asserts two separate properties, because they fail for different
reasons:

- **coverage** — every service with an `image` and no `build:` appears in the
  manifest. Locally-built services are excluded by _derivation_, not by a list,
  so a new local project needs no edit (same trick as the watchtower opt-out).
- **currency** — the tracked file still equals what the emitter produces. This
  catches both a stale regeneration and a hand-edit of a generated file.

Regenerate with `make diun-manifest`. The assertion proved itself immediately:
adding `diun` to the compose model failed `diun-manifest-coverage` and
`diun-manifest-current` until the manifest was regenerated.

## Tag policy

A tag is either a **moving pointer** or a **pinned release**, and they need
opposite treatment:

- `:latest` (19 services) → `watch_repo: false`. Diun watches that one tag and
  `DIUN_WATCH_COMPAREDIGEST=true` reports when the digest behind it moves. This
  is the same signal watchtower gave for the 16 labelled services, minus the
  ability to act on it.
- a pinned release (`qbittorrent`, `jellyfin`, `scrutiny`, `watchtower`, `diun`)
  → `watch_repo: true`, `sort_tags: semver`, `max_tags: 5`, plus an
  `include_tags` regex for that image's release-tag shape. Moving pointers
  (`latest`, `nightly`, `develop`, `rc*`, …) are excluded globally, because
  semver-ranking them against real releases produces noise and, in some
  registries, ranks them above real releases.

`qbittorrent` additionally excludes everything below **5.2.2** (ADR-0005's
floor: upstream #24357, fixed by #24363). A notification offering a version the
invariants forbid is worse than no notification, so it is filtered at source
rather than remembered by the reader.

### One entry per image, not two — a deviation, stated

The brief asked for a 10.11.z point release to be "loud" and a hypothetical
major to be "separately loud". Diun's file provider keys entries by image name,
so two entries for the same repository collide; there is no clean way to express
two policies for one image. `sort_tags: semver` gives the same information
without the hack — a major arrives at the top of the same ranked window, and the
notification title carries the full tag, so `12.0.0` is unmistakable next to
`10.11.12`. Recorded as a deviation rather than silently dropped.

## The spam trap, and how it is avoided

Three settings that only work together:

| setting                                        | why                                                                                       |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------- |
| `/data` persisted (`${CONFIG_DIRECTORY}/diun`) | without it every restart is a first run again, and re-announces everything                |
| `DIUN_WATCH_FIRSTCHECKNOTIF=false`             | the first run populates the DB for 24 images; announcing them all would be the spam event |
| `DIUN_WATCH_RUNONSTARTUP=true`                 | makes the DB exist within a minute of a cold start, which the healthcheck depends on      |

**Verified, not assumed:** the first real run analyzed 24 images, logged
`New image found` for all of them, and sent **zero** ntfy messages.

And verified the other direction — that a genuinely new tag _does_ notify —
with a throwaway Diun against its own database: run 1 with an
`include_tags` matching only `v0.9.2-omnibus`, then run 2 with the filter
widened. Run 2 found the newly-matching tags and pushed to `nas-alerts` with
tag `package`.

Honest note on that test: it produced **three** notifications, because widening
a regex reveals three tags at once, which is not what a release does. With
`max_tags: 5` and `sort_tags: semver`, a single new release enters the ranked
window as one new tag and the oldest leaves it silently, so a release is one
push. The test proves the mechanism and the credential path; it does not prove
the count, and it is not claimed to.

## Notification: a third answer to the ntfy auth problem

AGENTS.md records two ways to get credentials into a service that cannot set an
`Authorization` header: the `?auth=` query parameter (Jellyseerr) and `user:pass@`
URL userinfo (watchtower, Bazarr). Diun's native ntfy notifier accepts neither —
it takes a **token** only.

So this uses an ntfy **access token** minted for the existing write-only `arr`
publisher:

```
docker exec ntfy ntfy token add --label "diun (update notifications)" arr
docker exec ntfy ntfy token remove arr <token>      # to revoke
```

That is strictly better than reusing the account's password: the token is
independently revocable without rotating a credential six other containers
share, and it inherits the account's write-only ACL on `nas-alerts`. Stored as
`NTFY_DIUN_TOKEN` in `.env`.

## Healthcheck: what "healthy" means for a scheduled checker

Diun has no HTTP server, so there is no liveness endpoint. The failure that
matters is not "the process died" (that is caught by `restart: unless-stopped`
and by `stack_watchdog.py`) but **"it is running and has silently stopped
checking"** — a monitor that has stopped monitoring while looking fine.

So the healthcheck asserts the database has been _written_ within 48 h (two
schedule periods). It is valid from a cold start only because
`RUNONSTARTUP=true` populates the DB immediately.

Implementation note, measured rather than assumed: this is a busybox image and
`find -newermt` is **not supported** there. It fails in a way that would have
made the healthcheck permanently green, which is worse than no healthcheck. The
test uses `stat -c %Y` and arithmetic instead.

## Schedule

`10 4 * * *` — twenty minutes past the slot watchtower occupied, so "what
changed overnight" stays one glance at one topic. `WATCHTOWER_SCHEDULE` becomes
irrelevant with ADR-0025.

## Does watchtower still earn its place?

**No.** Answered on paper here and acted on separately in ADR-0025, so the two
are independently revertable.

Under monitor-only, watchtower's only remaining function is notification. Diun's
coverage is a strict superset:

|                                         | watchtower (monitor-only) | diun (file provider)                    |
| --------------------------------------- | ------------------------- | --------------------------------------- |
| the 16 labelled `:latest` services      | yes                       | yes                                     |
| the 4 locally-built images              | label is meaningless      | excluded by derivation                  |
| pinned tags (`jellyfin`, `qbittorrent`) | **never reports**         | yes, with semver ranking                |
| unlabelled containers                   | **never checked**         | irrelevant — it reads the compose model |
| coverage asserted by `make check`       | no                        | yes, both directions                    |
| needs Docker API access                 | yes, via dockerproxy      | **no**                                  |

There is nothing watchtower reports that Diun does not, and one thing it does
that Diun cannot: stop, remove and fail to recreate a container.
