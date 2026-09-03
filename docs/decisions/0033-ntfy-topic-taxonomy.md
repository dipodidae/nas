# ADR-0033 — Six ntfy lanes: priority carries severity, topic carries audience

**Date:** 2026-09-03
**Status:** accepted
**Supersedes:** the single-topic layout in ADR-0012 (which stands otherwise)
**Related:** ADR-0011 (credentials off the container), ADR-0012 (self-hosted
ntfy, deny-all), ADR-0024 (diun), ADR-0032 (the watchdog owns \*arr health)

`nas-alerts` is retired. Six topics replace it, all prefixed `nas-`:

| Topic           | prio | What goes here                                                                                                                                          | Publishers                                                                                                   |
| --------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| `nas-critical`  | 5    | A compose service with **no container at all**; host OOM kill; ext4/SMART/disk error; config backup failed; a user-visible service down >5 min          | `stack_watchdog`, `cron_job --fail-lane critical`, `post_update_verifier`, `scrutiny`, `verify-runtime`      |
| `nas-attention` | 4    | Needs a human today, not now: \*arr health, manual interaction, import/download failure, slskd logged out past grace, disk >90 %, a cleanuparr deletion | `stack_watchdog`, `cron_job` (2nd failure), `slskd_login_watch`, `cleanuparr`, the \*arr Ntfy connectors     |
| `nas-media`     | 3    | New stuff you can actually watch, with title, quality, size and a click-through                                                                         | `arr_notify.sh` via the \*arr Custom Script connector, `process_soulseek_imports`, the jellyseerr ntfy agent |
| `nas-requests`  | 4    | Jellyseerr: pending approval, declined, failed, issue reported, issue comment                                                                           | the jellyseerr webhook agent                                                                                 |
| `nas-infra`     | 2    | Routine ops: recoveries, first cron failures, drift, the 09:00 digest                                                                                   | `stack_watchdog`, `cron_job` (1st failure), `notify_digest`, `post_update_verifier`, `verify-runtime`        |
| `nas-updates`   | 1    | `diun` image-update notifications, and nothing else                                                                                                     | `diun`                                                                                                       |

## Context

One topic, every severity. Over the 48 hours to 2026-09-03, `nas-alerts`
carried 103 messages describing two dead indexers (ADR-0032) alongside every
import, every upgrade, every subtitle, every cron blip and every available image
update. ADR-0032 damped the loudest _detector_. This is the other half: the
messages needed somewhere to go.

A mixed-severity topic has exactly one stable outcome — it gets muted — and then
the failures the alerting was built to surface are the ones you stop seeing.
That is the same failure this stack has already been through twice: Jellyfin
OOM-killed five times in 48 h and qBittorrent dead for 14 h, both found by
accident, which is why ADR-0012 exists at all. An alerter that trains you to
swipe it away has regressed to the state it was built to fix.

## Decision

**Severity is carried by the ntfy priority. Audience is carried by the topic.**

The inverse — topics named for severity — was the obvious layout and is wrong: a
phone can mute a topic but cannot un-mute half of one, so `nas-errors` and
`nas-warnings` become two muted topics instead of one. Naming topics for
_audience_ means each one can be given a different phone setting once and left
alone: `nas-critical` and `nas-requests` bypass Do Not Disturb, `nas-media` is
normal, `nas-infra` and `nas-updates` are minimum importance with no sound.

The `nas-` prefix is load-bearing too. It makes one wildcard ACL (`nas-*`) and
one glance at the phone's subscription list sufficient.

**Nothing publishes directly.** `scripts/notify.py` is the only thing on this
host that knows a topic name. Callers name a **lane**; the router resolves the
topic from `NTFY_TOPIC_<LANE>`, applies the lane's default priority, and owns
the tag vocabulary. Containers that must hold a literal topic (diun, scrutiny,
cleanuparr, the \*arr connectors) interpolate the same `${NTFY_TOPIC_*}`
variables, so the mapping lives in exactly one place. `make check` asserts that
no publisher and no compose file holds a topic string.

### Three principals, least privilege, verified empirically

- `nas-scripts` — **write-only** across `nas-*`. The host router.
- `nas-arr` — **write-only** on exactly `nas-media`, `nas-attention`,
  `nas-requests`. Three grants, no wildcard: this token is stored inside three
  \*arr SQLite databases and bind-mounted into three containers, so a
  compromised \*arr must not be able to reach `nas-critical`.
- `nas-phone` — **read-only** across `nas-*`. The credential that gets typed
  into a phone and backed up to Google.

**Write-only, not read-write**, against the plan for this work. ADR-0012 makes
the publisher accounts unable to read the topics on purpose, so a leak of this
box's `.env` exposes no alert history — and nothing in this design reads from a
publisher account (the read-back in `make notify-test` uses `nas-phone`). `rw`
would have widened a capability for nothing.

The ACLs were asserted rather than assumed, with nine probes: the `nas-arr`
token returns **403** on `nas-critical`, `nas-infra` and `nas-updates`, and
**200** on its three lanes; `nas-scripts` returns 200 on `nas-critical` and
`nas-updates`; `nas-phone` returns 403 on publish and reads the message back out
of the cache. `scripts/check-ntfy-acls.py` re-asserts the grant _shape_ on every
`make verify-runtime`, by parsing `ntfy access` rather than publishing — a
monitor that probes every lane on every run becomes the noise.

### The `arr-token` file

The `nas-arr` token reaches sonarr/radarr/lidarr as
`${CONFIG_DIRECTORY}/ntfy/arr-token`, mode `0600`, owned `${PUID}:${PGID}`,
bind-mounted **read-only** at `/run/ntfy-arr-token`. Never an `environment:`
entry: ADR-0011 exists because a credential there leaks into `docker inspect`.
`make check` asserts both the mount and the absence. Read-only specifically
because a container that can rewrite its own credential can escalate its own
ACL.

### The noise controls

These are the point of the exercise, not a refinement of it.

- **Transition-only.** `notify.transition()` publishes on a state _change_. A
  `*/5` job cannot send the same message 288 times a day; a test asserts that
  288 polls of one active condition produce exactly one message.
- **Escalation by age**, in `stack_watchdog`: an unhealthy container starts in
  `nas-infra` because most are blips, moves to `nas-attention` after 15 min, and
  moves to `nas-critical` after 5 min if it is one of the four services whose
  failure a human notices unaided (jellyfin, nextcloud, swag, qbittorrent). "No
  container at all" skips the ladder — that is ADR-0006's failure mode and it
  cost qBittorrent 13 hours. An escalation is pushed **immediately** rather than
  waiting out `--repeat-min`, or the alert spends the whole outage in the
  quietest lane while the state file claims otherwise.
- **Cooldowns** keyed on a caller-supplied `dedup_key`, state in
  `logs/.notify_state.json`: 6 h on `nas-attention`, 1 h on `nas-infra`, none on
  `nas-critical`. Suppressed messages are **counted**, and the daily digest
  reports the count — that number is what keeps the windows honest.
- **`nas-critical` is structurally exempt** from both. `cooldown_seconds()` pins
  it to zero and `build_message()` strips `X-Delay` for it, whatever the caller
  or the clock asks. `make check` asserts both, because there is no cooldown
  value that is correct for the one lane where a swallowed message is the failure
  mode itself.
- **Quiet hours 23:00–08:00 Europe/Amsterdam** delay `nas-media`, `nas-infra`
  and `nas-updates`. `nas-critical` and `nas-requests` are never delayed.
- **The 09:00 digest** (`scripts/notify_digest.py`) replaces the routine
  chatter: containers, disk, OOM kills, cron failures, last good backup, imports
  per \*arr, slskd login state, diun freshness, and the suppressed count. It
  reports **state, not events**, so a digest that arrives late is still correct.

**Deliberately not notified at all:** On Grab, On Rename, On Retag, On
Application Update, On Test, subtitle downloads, recyclarr syncs, per-run cron
_successes_, `qbittorrent_settings_enforce` runs that changed nothing,
`media_ops_status` runs, and any `*/5` job reporting "all good".

## Rejected alternatives

**One topic per service.** Twenty-eight subscriptions to manage, no way to say
"wake me for this one", and the per-topic settings would have to be reapplied
every time a service is added. Muted within a week.

**Severity in the topic name** (`nas-errors`, `nas-warnings`, `nas-info`). Not
mutable at the granularity that matters, as above. It also makes routing a
_judgement_ at every call site rather than a lookup, so two publishers inevitably
disagree about whether the same event is an error or a warning.

**Apprise as a middleman.** An extra hop and an extra thing to keep running on a
single-host stack where every publisher already speaks ntfy natively. It buys
fan-out to services this box does not use. Rejected for the same reason ADR-0031
rejects a second notifier.

**A per-lane ntfy account.** Six accounts, six credentials, and no distinct
trust boundary between most of them. Three accounts map exactly onto the three
trust domains that actually exist here: the host, the containers that talk to
indexers and trackers, and the phone.

## Consequences, including the ones that cost something

- **The phone must subscribe to six topics** and be configured per topic. That
  cannot be automated; the checklist is in `README.md`.
- **\*arr UI toggles can drift.** The connectors live in each app's SQLite, so
  `make check` cannot see them. `make verify-runtime` therefore queries all four
  apps' `/notification` and asserts the connector set and trigger flags exactly
  — that is the check that catches someone ticking "On Grab" back on.
  `scripts/configure_arr_notifications.py --apply` converges them again.
- **Rotating `NTFY_TOKEN_*` needs no redeploy.** The router reads the token at
  call time, pinned by a test. (Regenerating the **VAPID** keypair is a
  different matter: it invalidates every existing browser Web Push
  subscription, so every browser has to re-subscribe. ADR-0012 already says so;
  it is repeated here because a token rotation and a keypair regeneration look
  like the same chore and are not.)
- **`nas-cleanuparr` is retired too.** Cleanuparr's deletions are exactly the
  "needs a human today" shape and belong in `nas-attention` rather than on a
  topic of their own, which was a private single-topic split made for the same
  reason this ADR replaces globally.

## Departures from the plan for this work, and why

Recorded because each was a deliberate choice against an explicit instruction.

1. **ADR numbered 0033, not 0030.** 0030–0032 were already taken.
2. **`http://ntfy:8410`, not `http://ntfy`.** ntfy runs as `${PUID}:${PGID}`
   (ADR-0014) and a non-root process cannot bind `:80`; `NTFY_LISTEN_HTTP=:8410`
   is why SWAG proxies to it. Verified 200 on `/v1/health` from inside sonarr,
   radarr and lidarr.
3. **Publisher accounts are `wo`, not `rw`** — see above.
4. **`onHealthIssue` / `onHealthRestored` stay OFF on all four \*arr.** The plan
   asked for them on. ADR-0032 switched them off the day before after they
   produced 103 messages in 48 h: no filtering, fires on every transition, and
   all three apps raise the same indexer warning. `stack_watchdog` owns \*arr
   health and routes it to `nas-attention`, so the lane loses nothing. Turning
   them back on would have restored the exact noise this ADR exists to remove.
5. **"On Import Failure" / "On Download Failure" exist only on Lidarr.** Read
   off the live `/notification/schema`: Sonarr and Radarr have neither, and have
   `onManualInteractionRequired`; Lidarr is the inverse. An unsupported flag in a
   `PUT` body is accepted and silently dropped, so the plan's uniform trigger set
   would have produced connectors that look configured and fire for nothing.
6. **Prowlarr gets no notification connector at all.** It supports only
   `onGrab`, `onHealthIssue`, `onHealthRestored` and `onApplicationUpdate` —
   every one on the do-not-notify list. Its existing connector had zero triggers
   enabled for exactly this reason; it is deleted rather than reconfigured.
7. **Jellyseerr's two agents are swapped.** Its native ntfy agent **hardcodes
   `priority = 3`** and sets no tags (`buildPayload` in
   `/app/dist/lib/notifications/agents/ntfy.js`); its webhook agent takes an
   arbitrary `authHeader` and body template. So the webhook serves
   `nas-requests` at priority 4 and the native agent serves `nas-media`, whose
   priority is 3 — exactly what it hardcodes. What is lost is a `popcorn` tag and
   a custom title on "now available": decoration, where the priority is the
   contract.
8. **`X-Delay` needed no fallback queue.** Measured on this instance: a 20 s
   delayed publish returned 200 and was delivered 20 s later (15:55:52 →
   15:56:12), and the `8am` form is accepted. No state-file queue, no 08:00
   flush cron.
9. **The `nas-alerts` invariant is scoped to code and configuration.** The
   literal still appears in `docs/decisions/`, `docs/jellyfin-playback-audit.md`,
   the archived crontab snapshots and in prose explaining this migration. Those
   are **records of what was true**; rewriting history to satisfy a grep would be
   the wrong fix.
10. **`playlist-generator` is exempt from the escalation ladder.** Its CPU-bound
    enrichment stages block the single-worker backend's event loop — measured CPU
    101.63 % with an unhealthy streak of 23 — so it goes unhealthy for hours doing
    what it is supposed to do. It still alerts, in `nas-infra`, and the digest
    counts it; it just cannot escalate on age. Same shape as ADR-0026's slskd
    `start_period`: the container is busy, not broken. The exemption does **not**
    cover `:missing` or `:down` — absent is not busy.
11. **Jellyfin's Webhook plugin stays uninstalled.** It was not installed (24
    plugins, none of them it), and \*arr imports already cover new media; a second
    source would double-notify every episode.

## Four bugs this work surfaced, all of the same shape

Each had been failing silently for as long as it had existed, and each was found
only by checking the property rather than the component — AGENTS.md's rule,
earning its keep four more times.

1. **Every non-ASCII title from a Python publisher was never sent.**
   `http.client` encodes header values as **latin-1**, so an `X-Title` with an
   em dash or an emoji raised `UnicodeEncodeError`, which the router caught as a
   `ValueError` and reported as a failed publish. Invisible because the failure
   is a logged warning on a deliberately best-effort notifier, and because
   `arr_notify.sh` uses `curl` — raw bytes — so the shell path worked and the
   Python path did not. Fixed at the wire boundary by encoding to UTF-8 and
   decoding as latin-1, which is byte-identical to what curl sends.
2. **`verify-runtime`'s cron line swallowed every violation it ever found.**
   `make verify-runtime` exits 1 on a violation and the line used `cron_job.py`'s
   default `--ok-codes 0,1`. Now `--ok-codes 0`, and the target pushes its own
   findings: `nas-critical` for an invariant lost, `nas-infra` for drift.
3. **`post_update_verifier` reported a failure on every single run.** It probed
   `https://localhost:443` against SWAG's real Let's Encrypt certificate, so
   hostname verification could never pass — and `--ok-codes 0,1` hid it. The
   probe asks "does nginx answer", not "is the cert valid for localhost", which
   is a question with no useful answer.
4. **The digest's Sonarr import count would have been zero forever.**
   `episodeFileImported` does not exist in Sonarr; the event is
   `downloadFolderImported`. And the paged `/history` endpoint cannot answer "how
   many in 24 h" — 200 rows of Sonarr history here reached back only a few hours,
   because 112 were grabs and 70 were renames. `/history/since?date=` returns the
   whole window.

## The verification method the plan prescribed does not work

For discovering the \*arr custom-script variable names, the plan said to fire
the connector's Test and dump `env`. Measured: the \*arr Custom Script test
passes exactly **one** variable, `<app>_eventtype=Test`, and nothing else. It
proves the script is invoked and exits 0, and confirms no payload name whatever.
The names are not literals in the shipped DLLs either — Sonarr composes them at
runtime, so `strings` finds nothing.

`arr_notify.sh` therefore dumps its whole environment **once** on the first real
event, marker-gated, and every field degrades to `imported` rather than
rendering a blank. The next genuine import proves the names in
`/config/logs/arr_notify.log` without anyone guessing, and a wrong name makes a
message thin rather than absent.

## Revert

```bash
# 1. Re-grant the old topic and repoint the publishers.
docker exec ntfy ntfy access nas-scripts nas-alerts wo
# 2. Set NTFY_TOPIC_<LANE>=nas-alerts for every lane in .env. Every publisher
#    reads the mapping from there, so this is the whole rollback for the router,
#    diun, scrutiny and (after a re-run of the two convergers) the *arr,
#    jellyseerr and cleanuparr notifiers:
#      .venv/bin/python scripts/configure_arr_notifications.py --apply
#      .venv/bin/python scripts/configure_service_notifications.py --apply
# 3. `make check` will fail `ntfy-no-topic-literals` / `ntfy-alerts-retired`.
#    That is the invariant doing its job; remove section 25 from
#    scripts/check-invariants.sh deliberately, in the same commit.
```

Note what this does **not** restore: the per-lane priorities collapse to one
topic, so the phone loses the ability to treat a dead container differently from
a new episode. That capability is the whole ADR.
