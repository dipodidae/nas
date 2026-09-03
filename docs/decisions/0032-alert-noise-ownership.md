# ADR-0032 — The watchdog owns indexer and *arr health alerting

**Date:** 2026-09-03
**Status:** accepted

`onHealthIssue` and `onHealthRestored` are now **off** on the Prowlarr, Sonarr
and Radarr Ntfy connections. `scripts/stack_watchdog.py` reports that ground
truth instead, from the Prowlarr and *arr APIs, with damping.

This is a live-config change in three SQLite databases, not in the compose
files, so `make check` cannot assert it. Hence a record.

## Context

Over 48 hours to 2026-09-03, counted from ntfy's own `cache.db`:

```
  22  Sonarr - Health Check Restored
  22  Sonarr - Health Check Failure
  19  Prowlarr - Health Check Failure
  17  Prowlarr - Health Check Restored
  12  Radarr - Health Check Failure
  11  Radarr - Health Check Restored
```

103 messages at `priority=4`. What they described was **two** indexers:
1337x (failing since 2026-08-27) and Torrent[CORE] (since 2026-07-27).

Three mechanisms multiplied together:

1. Prowlarr's **short-term** `IndexerStatusCheck` appears and clears within
   minutes as public trackers bounce. Flapping is the normal condition of a
   public tracker, not an incident.
2. All three apps hold an Ntfy connection on `onHealthIssue` **and**
   `onHealthRestored` with no tag filter, so every transition is two messages.
3. Sonarr and Radarr raise their own copy of the same Prowlarr-sourced warning,
   so one indexer bouncing is up to six messages.

The 2026-09-03 triage feed reported Knaben, Uindex and TorrentDownload as
"currently failing, no restore". None of the three had an `/indexerstatus` row
at all — Prowlarr considered them healthy. The only accurate line in the whole
feed was `IndexerLongTermStatusCheck`, the >6h one, which named exactly the two
that really were down.

## Decision

Damp on **our** side of the webhook, and let one component own indexer state.

- `check_indexer_failures` polls Prowlarr's `/indexerstatus` and alerts only on
  indexers whose `initialFailure` is more than 6h old — deliberately the same
  threshold as `IndexerLongTermStatusCheck`, because that was the signal that
  was right. One key per indexer, carrying the real outage duration, backing
  off to 6-hourly.
- `check_arr_health` polls all four apps' `/health` for everything else, so
  root-folder, download-client and update warnings are not lost. Deduped per
  `(app, source)`, repeated daily.
- Both indexer health checks are dropped from `check_arr_health`:
  `IndexerStatusCheck` because it is the churn itself, and
  `IndexerLongTermStatusCheck` because it is redundant — all three apps raise it
  for the same indexers, which turned 2 dead indexers into 6 alerts.
- `onHealthIssue` / `onHealthRestored` off on all three Ntfy connections.
  `onManualInteractionRequired` is **left on** in Sonarr and Radarr: that one is
  a request for a human, not a status report.

Result on this stack: 6 indexer-related alerts became 2, and a flap now produces
nothing at all.

## Why not filter in Prowlarr

Tag-based notification filtering is unreliable (Prowlarr#1977), and there is no
per-health-check mute in any of the three apps. The choice was between all
health notifications or none, which is what made moving the logic out the only
option that keeps the signal.

## Why not leave the app-side notifications on as well

Because then nothing improves. The watchdog's damping is additive; the 103
messages come from the apps. Both had to change together, which is why the
watchdog gained `check_arr_health` first — turning the toggles off before that
existed would have dropped real warnings.

## The trap, if you ever edit these connections

`GET /notification` **masks the `password` field as 8 asterisks.** CLAUDE.md
records the MediaBrowser `apiKey` case, where the real value must be written
back before the `PUT` or it is clobbered.

**For the Ntfy `password` field on these versions the mask is preserved** — a
`GET` → modify → `PUT` round-trip leaves the stored credential intact. This was
not assumed. It was measured, per app, on a throwaway notification created with
a sentinel password, by reading the value back out of the live SQLite DB:

```
prowlarr   masked-on-GET=True  password-preserved-through-PUT=True
sonarr     masked-on-GET=True  password-preserved-through-PUT=True
radarr     masked-on-GET=True  password-preserved-through-PUT=True
```

So the trap is **field- and implementation-specific**, not a property of the
*arr API. Verify it on a throwaway before trusting it for another field; do not
generalise either way from this record.

Two further points inherited from the MediaBrowser case and still true here:

- Confirm the result **in the database**, not by re-`GET`ting, which re-masks.
- `.docker-config/*/[app].db` is **WAL-mode**. Read the live path (the `-wal` is
  beside it) or open a copy that includes `-wal`/`-shm`; copying only the `.db`
  reads back stale values.

## Revert

```bash
# Per app: set both toggles back to true. The mask round-trips safely.
#   prowlarr id=1, sonarr id=2, radarr id=3   (implementation: Ntfy, "ntfy — alerts")
# GET /api/{v1|v3}/notification/<id> -> set onHealthIssue/onHealthRestored true -> PUT
```

Verify by reading `OnHealthIssue, OnHealthRestored` out of `Notifications` in
the app's DB, then fire the connection's own test action — the test proves the
credential still works, which the structural check does not.

## Also removed, same day

**Torrent[CORE]** (Prowlarr indexer id 16), down 912h since 2026-07-27, priority
25, not `cloudflare`-tagged so byparr was never in its path. Five weeks of
uninterrupted failure with no recovery reads as a dead site rather than a
misconfiguration. Its definition is exported to
`docs/removed-indexers/torrent-core-2026-09-03.json` (secret-shaped fields
redacted on export) so it can be re-added if the site returns.

`1337x` was left in place pending a separate decision; see
`TRIAGE-2026-09-03.md` §P4 for why byparr cannot fix it (Prowlarr#2572, #2672 —
Prowlarr discards the solver's body and re-fetches with its own TLS
fingerprint).
