# ADR-0056 — Umami for ongehoord.nl, and no visitor ever talks to it

**Date:** 2026-09-27
**Status:** accepted

## Decision

Self-hosted **Umami 3.4.0** (`compose/analytics.yaml`: `umami` + its own
`postgres:18.6-alpine`) is the only analytics on www.ongehoord.nl and
acceptance.ongehoord.nl. It **replaces Google Analytics**, which was setting
cookies with no consent banner.

The site uses **nuxt-umami with `proxy: 'cloak'`**. The browser posts events to the
site's own `/api/savory`, and Vercel's server forwards them to
`https://umami.${PUBLIC_DOMAIN}/api/send` with the visitor IP and user agent **in
the payload**. So:

- no visitor ever resolves this domain or connects to this box
- no adblock list sees an Umami host, because there is none in the page
- the website ID stays in private runtime config and never reaches a browser

Websites in Umami: `Ongehoord` (www.ongehoord.nl, Vercel Production),
`Ongehoord (acceptance)` (Preview, branch `acceptance`), and `nas-canary`
(`make verify-runtime` only). `NUXT_UMAMI_HOST` and `NUXT_UMAMI_ID` are build-time env on
Vercel. Without them the module is a no-op, which covers local dev, tests and other previews.

## Invariants

- **`SKIP_LOCATION_HEADERS=1`.** Every event arrives from Vercel's server, so a
  geo header (`x-vercel-ip-country`, and `cf-ipcountry` once ADR-0055 is live)
  describes Vercel. Location must come from the payload IP, looked up in the
  bundled GeoLite2. `check-umami-live.py` asserts this with a known NL address.
- **The route is `protect` with exactly `location = /api/send` open, POST only.**
  The dashboard and its `/api/*` data calls sit behind tinyauth, then Umami's own
  login (with TOTP available: `TWO_FACTOR_ENCRYPTION_KEY` is set). `/script.js`,
  `/api/batch` and `/share/*` are closed on purpose.
- **Pinned, both images** (`MANUAL_UPDATE_ONLY`). Umami runs Prisma migrations
  forward on start. `pg_dump umami-db` before bumping.
- **`APP_SECRET` stays stable.** Rotating it logs every dashboard user out.

## Privacy

Cookieless. Umami stores no IP address. A session is a salted hash whose salt
rotates monthly (`SALT_ROTATION` default). Event properties never carry a name,
e-mail address, message text, search query or location owner (see
`app/utils/analytics.ts`). A visitor can opt out with
`localStorage['umami.disabled'] = '1'`.

## Proof

`scripts/check-umami-live.py` (in `make verify-runtime`) sends an event through the
public route the way Vercel does, reads it back from the API, and checks it was
geolocated to NL from the payload IP. `check-door-live.sh` covers the door.
