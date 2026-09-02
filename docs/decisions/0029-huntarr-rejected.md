# ADR-0029 — Huntarr is rejected on security grounds, and the cron pair stays

**Date:** 2026-09-02
**Status:** accepted (rejection). **Reverses an explicit instruction to deploy it**
**Related:** ADR-0003, ADR-0017, ADR-0020

## What was asked for, and why it is not being done

The work order specified adding `plexguide/Huntarr.io` — a library sweeper that
finds monitored-but-missing and below-cutoff items and searches for them in
rate-capped batches, as the fill-side complement to Cleanuparr's clean side. It
was to be deployed read-only with low hourly API caps and a week of observation.

It is not being deployed. The upstream is **archived, and archived because of
unfixed authentication bypasses.**

## The evidence

- The repository at `plexguide/Huntarr.io` returns **404** for both
  `/releases/latest` and `/tags`. No image reference resolves:
  `ghcr.io/plexguide/huntarr:latest`, `:8.1.13`,
  `ghcr.io/plexguide/huntarr.io:latest` and `huntarr/huntarr:latest` all fail
  to pull.
- The only surviving copy is a third-party archive,
  `MGHazz/huntarr.io-archive`, whose README reads: _"preserved for posterity
  after the original author went scorched earth when significant security
  vulnerabilities were pointed out to them"_, and carries the banner
  **"THIS REPO IS NOT UNDER ACTIVE DEVELOPMENT. USE THIS CODE AT YOUR OWN
  RISK!"**
- **"This repository was archived by the owner on Feb 23, 2026."**
- The disclosed vulnerabilities, in 9.4.2 and earlier: multiple
  **unauthenticated authentication bypasses** reaching the application's
  settings, **plaintext leakage of stored user passwords**, and **exposure of
  the API keys of every integrated \*arr application.**

The disclosure's own title is the argument: _"your passwords and your entire arr
stack's API keys are exposed to anyone on your network, or worse, the
internet."_

## Why that is disqualifying here specifically

Huntarr's whole function requires holding `API_KEY_SONARR`, `API_KEY_RADARR`
and friends. The vulnerability class is precisely _unauthenticated disclosure of
exactly those keys_. So the failure mode is not incidental to the feature — the
credentials it must hold are the credentials it leaked.

On this box that lands badly:

- 16 subdomains are already public behind SWAG, so "only on your network" is not
  a boundary anyone should lean on.
- The \*arr API keys are write-capable. An attacker holding them can add,
  delete and re-path media across 4.6 TB that **is not backed up by choice**.
- ADR-0003 records that a single wrong \*arr API call
  (`PUT /api/v1/artist/editor`) wiped TrackFiles. The blast radius of a leaked
  key is already documented in this repo.

## The precedent this follows

ADR-0020 replaced `containrrr/watchtower` because it had been _archived_ —
"the next Docker API change has nobody to answer it" — and that was treated as a
maintenance risk worth acting on while the software still worked.

Huntarr is strictly worse than that case: archived **because** of security
defects, with no fix, no maintainer, and no image left to pull. If an archived
upstream justified replacing a working tool, an archived-under-disclosure
upstream cannot justify adding one.

## Consequence: the cron pair is not retired

The work order's next step was to delete `lidarr_monitor_sweep.py` and
`lidarr_backlog_drip.py` once Huntarr proved it covered them, and asked for a
retirement date rather than an open-ended deferral.

**There is no retirement date, because there is no replacement.** Those two jobs
stay, and this is now a justified position rather than a deferral:

- `lidarr_backlog_drip.py` self-throttles against slskd's _live_ in-flight
  count, excludes dead remote-queue grabs over 6 h, and holds a 12 h per-album
  cooldown. It exists because naive searching got this host's IP **soft-blocked
  by slsknet** — twice, by two different mechanisms (ADR-0009's neighbourhood,
  and the Tubifarry fallback fan-out).
- `lidarr_monitor_sweep.py --no-search` repairs artists left with zero monitored
  albums by bulk adds. That is a Lidarr-specific data-repair job, not a search
  sweeper, and nothing generic replaces it.
- Huntarr would not have been allowed near Lidarr anyway. The original brief
  said so: Huntarr searching + Cleanuparr armed + Lidarr on `:nightly` is a
  three-way footgun, and ADR-0017 already forbids Lidarr in any Cleanuparr
  module because its only client is slskd, which those tools cannot see.

So the "hand-rolled, music-only version of a solved problem" turns out to be a
hand-rolled version of a problem that this tool did not solve safely. The gap
for TV and film is smaller than it looked: Sonarr and Radarr both have built-in
periodic missing/cutoff searches, which are the supported path and hold the
credentials already.

## If a filler is wanted later

Requirements, in order: an actively maintained upstream; no plaintext credential
storage; authentication that cannot be bypassed; and hard per-hour search caps,
because the real constraint on this box is not CPU but **indexer and slsknet
rate limits**. Nothing currently evaluated meets the first requirement.
`Decluttarr` is not a candidate for this job — it is a queue cleaner, the same
side as Cleanuparr, and two queue cleaners is worse than one.
