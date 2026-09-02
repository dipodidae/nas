# ADR-0012 — Self-hosted ntfy for push alerting

**Date:** 2026-09-01
**Status:** accepted
**Background:** `docs/jellyfin-playback-audit.md` §3.3 "Alerting: built, and it
immediately had things to say", §4.4

## Why it exists

**Nothing on this box reported failure.** Jellyfin was OOM-killed five times in
48 h and qBittorrent sat dead for 14 h — both found by accident.

## Why ntfy over Gotify

A single unauthenticated-shaped `POST <topic-url>` with a text body is the
whole publish contract. It needs no server-side application/token setup before
the first alert can land, and this repo already speaks it
(`SLSKD_ALERT_WEBHOOK` in `scripts/slskd_login_watch.py`).

## Why self-hosted rather than ntfy.sh

So alert contents never leave the box. The watchdog publishes over loopback
(`127.0.0.1:8410`); only the phone's subscription goes out through SWAG.

## Configuration decisions

- **`NTFY_AUTH_DEFAULT_ACCESS=deny-all`** — a topic on a public subdomain is
  otherwise world-readable to anyone who guesses its name. Access is granted
  per-user.
- **`NTFY_ENABLE_SIGNUP=false`** — accounts are created by hand.
- **`NTFY_ENABLE_LOGIN=true`** — without it the web UI cannot authenticate at
  all, which on a deny-all server means `ntfy.${PUBLIC_DOMAIN}` is a login page
  you can never get past.
- **Cache 48 h** — an alert raised while the phone is offline is still
  delivered on reconnect instead of being silently dropped.
- **Web Push keypair** — lets a browser notify with no ntfy tab open. Generated
  with `docker exec ntfy ntfy webpush keys` and kept in `.env`.
  **Regenerating them invalidates every existing browser subscription.** Android
  does not use any of this; the app holds its own connection to this server.
- **Deliberately NOT set: `NTFY_UPSTREAM_BASE_URL`.** That exists only so iOS
  devices can be woken via ntfy.sh's APNs relay, and it would send a hash of
  every topic to ntfy.sh. Android needs no relay, so nothing about these alerts
  leaves the box.
- **`user: ${PUID}:${PGID}`** and a pre-chowned config dir — ADR-0014.

## Publishers

- `scripts/stack_watchdog.py` (cron `*/5`) — the detector for ADR-0006's
  "no container at all" failure mode
- `scripts/cron_job.py` — the wrapper that reports cron failures
- `scripts/slskd_login_watch.py` (cron `*/15`) — ADR-0009
- `watchtower` via shoutrrr, one digest per run
  (`WATCHTOWER_NOTIFICATION_REPORT=true`), so "what changed at 04:00 and did it
  work" does not require reading logs

## Coverage limit (deliberate)

The watchdog and ntfy run on the **same host**, so neither can tell you the
**host itself** is down. That needs an off-box heartbeat
(`NAS_HEARTBEAT_URL`, `scripts/heartbeat.py`); the off-box receiver is
deliberately not built here.
