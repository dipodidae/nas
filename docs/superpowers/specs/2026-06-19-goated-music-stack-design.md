# Goated 2026 Music-Intelligence Stack — Design / North-Star Roadmap

**Date:** 2026-06-19
**Status:** Design — approved direction, phased workstreams each get their own spec→plan
**Author:** brainstormed with Claude, grounded in live-ecosystem research (2026-06-19)

## Context

The trigger was a research dump recommending a batch of *arr-adjacent tools
(Profilarr, Cleanuparr, Decluttarr, Maintainerr, Modmanager, cross-seed). The
user's goal: take this single-host homelab NAS stack to "goated levels" across
three axes — **self-healing quality**, **less maintenance**, and **music
intelligence** — using only real, current ecosystem tools, tied together
coherently.

Three parallel research streams verified each candidate against live 2026 repos
(GitHub/PyPI/API as of 2026-06-19). The finding reframed the whole exercise:
**most of the dump's headline tools either already run here or do not fit a
Jellyfin + Lidarr + slskd stack.** The leverage is not breadth (more wrappers)
but depth — closing a *music-intelligence loop* around the differentiated
components already built in this repo.

## Guiding thesis

> "Goated" for this stack = **concentration, not accumulation.**

Every external tool added must fill a *gap the existing stack actually has*.
The unifying architecture is a **closed music-intelligence loop**, with the
repo's own `playlist-generator` and `ops.html` dashboard as the hub:

```
  acquire            enrich on import         serve          capture            recommend
 ┌─────────┐        ┌──────────────────┐    ┌─────────┐    ┌────────────┐    ┌───────────────────┐
 │ Lidarr  │        │ beets / rsgain / │    │         │    │ ListenBrainz│   │ playlist-generator │
 │ + slskd │ ─────▶ │ Essentia (mood/  │──▶ │ Jellyfin│──▶ │  (behavioral│──▶│  + AudioMuse ideas │
 │ + qBit  │        │ genre features)  │    │         │    │   scrobbles)│   │  (sonic + behavioral)│
 └─────────┘        └──────────────────┘    └─────────┘    └────────────┘    └───────────────────┘
      ▲                                                                                  │
      └──────────────────── new playlists pushed back into Jellyfin ◀────────────────────┘
                                         (loop closes)

  ops.html observes every hop (acquisition health, enrichment coverage, loop freshness)
```

Each external tool slots into one gap the home-grown stack does **not** already
cover:

| Gap in current stack | Filled by |
| --- | --- |
| Behavioral listening signal (what gets played/loved) | **ListenBrainz** + `jellyfin-plugin-listenbrainz` |
| Acoustic features by track (AcousticBrainz is dead) | **local Essentia models** feeding `playlist-generator` |
| Plugin-side acquisition flood control | **Tubifarry v2.1.1** semaphore / `MaxGrabsPerUser` |
| Sonic-similarity recommendation benchmark | **AudioMuse-AI** (as prior-art bake-off, not blind swap) |

## Verified keep / add / skip decisions

These are the load-bearing conclusions from the research. They are decisions,
not options.

### KEEP (already correct — do not churn)
- **Lidarr `:nightly`** — alive, maintenance-pace not abandoned; no credible
  successor exists. The album-centric / track-first gap remains unfilled in 2026.
- **slskd** — actively maintained; do **not** swap for experimental `slskdn`/`slskr` forks.
- **Tubifarry** — the leading, most active Lidarr↔slskd integration (v2.1.1, 2026-06-12).
- **rsgain** (`scripts/replaygain.py`) — confirmed the modern ReplayGain 2.0 / EBU R128 standard.
- **recyclarr** — keep for Sonarr/Radarr custom-format sync.
- **byparr** — keep as the FlareSolverr-compatible solver for CF-protected Prowlarr indexers.
- **Cleanuparr** — keep, but audit scope (see Phase C).
- **The custom Python scripts** (`slskd_incomplete_sweep.py`,
  `lidarr_stuck_download_reaper.py`, `lidarr_backlog_drip.py`, the Tubifarry
  gates) — **confirmed load-bearing**: no off-the-shelf tool covers slskd or
  Lidarr "hunting" (see Skip rationale).
- **The VA-by-MBID / verify-by-foreign-id work** (recent commits) — this is
  coding around the documented upstream Lidarr metadata-server degradation.
  Keep it; it is the correct workaround.

### ADD (verified gap-fillers, phased below)
- **ListenBrainz account + `lyarenei/jellyfin-plugin-listenbrainz` v6.2.0.3**
  (targets Jellyfin 10.11+) — server-side scrobbling, loved/favorite sync, and
  syncs ListenBrainz "Created for You" playlists (Weekly Jams / Exploration /
  Daily Jams) back into Jellyfin. Highest impact, lowest effort.
- **Local Essentia models** (`essentia-tensorflow`, models at essentia.upf.edu)
  — the technical replacement for AcousticBrainz; produce mood/genre/feature
  vectors per track to backfill the `album_tags` enrichment table that
  `playlist-generator` already has wired but **empty**.
- **AudioMuse-AI** (v2.3.0) — evaluated as a **time-boxed bake-off** against the
  in-house `playlist-generator`, NOT a blind replacement. It overlaps heavily
  (local sonic analysis, embeddings, NL playlists, Jellyfin Instant Mix
  integration). Outcome is one of: adopt as replacement, harvest specific ideas,
  or confirm the home-grown engine wins. Decision deferred to its own spec.

### SKIP (verified non-fits — with reasons, so they are not re-litigated)
- **Profilarr** — hard-excludes Lidarr; for Sonarr/Radarr it is a heavier
  two-container always-on GUI that only *pushes* config. Sidegrade, not upgrade,
  for a config-as-code homelab. recyclarr stays.
- **Modmanager** — marginal on a single host AND wants the Docker socket, which
  collides with the standing `dockerproxy` socket-isolation rule. Only worth it
  with many Docker Mods (not the case here).
- **Decluttarr** — redundant with Cleanuparr.
- **Maintainerr** — Plex/Overseerr-centric; weakest Jellyfin fit of the batch.
- **Huntarr** — never installed here (confirmed absent from compose); the Feb-2026
  security advisory is moot. No keys to rotate.
- **Picard 3** (beta, config-incompatible with v2), **slskdn/slskr forks**, and
  "AI auto-taggers" (none have displaced AcoustID/MusicBrainz) — premature/hype.

## Phased workstreams

Each phase is independently shippable and gets its own spec→plan→implement
cycle. Ordered by impact-to-risk. **No live compose changes happen under this
roadmap doc itself** — only under each phase's own approved plan.

### Phase 1 — Close the listening loop (HIGH impact, LOW risk)
**Goal:** Jellyfin music gains a behavioral intelligence signal it has never had.
- Stand up a ListenBrainz account (self-hosted instance is out of scope — use the
  hosted MetaBrainz service; it is the recommendation engine).
- Deploy `jellyfin-plugin-listenbrainz` v6.2.0.3 (requires confirming Jellyfin is
  on 10.11+; if not, that upgrade is a prerequisite sub-task).
- Verify scrobble → loved-sync → ListenBrainz-playlist-import round-trips.
- Surface "loop freshness" (last scrobble, last imported playlist) on `ops.html`.
**Risk notes:** Jellyfin version gate; plugin compatibility. Reversible (plugin removal).

### Phase 2 — Acquisition hardening (MEDIUM impact, MEDIUM risk)
**Goal:** push flood control and config-correctness upstream into the tools so
fewer custom-script firefights are needed.
- Upgrade **Tubifarry to v2.1.1** (gains slskd semaphore + `MaxGrabsPerUser`,
  the plugin-side analogue of the local `--no-search` / `useFallbackSearch=False`
  ban fixes). **Watch issues #199 (FFmpeg re-download loop in Docker) and #200
  (Lidarr fails to parse JSON from Flaresolverr/Byparr)** — both directly touch
  this stack's byparr-fronted indexer path. Stage on a throwaway first.
- Plan the **slskd v0.25 config migration** — v0.25.0 is a BREAKING restructure
  (`global`→`transfers`, `integration`→`integrations`, upload-key reorg) + .NET 10.
  Must be authored before the next slskd bump or the container will fail config-parse.
- Evaluate slskd's **native gluetun port integration** (v0.24.4+) to replace the
  manual `FIREWALL_INPUT_PORTS` ↔ slskd coupling in the `network_mode:
  service:gluetun` setup.
**Risk notes:** Tubifarry upgrade can regress acquisition; slskd config migration
is breaking. Both need staging. Respects the documented slskd login-timeout /
ghost-session gotcha (no login-aware healthcheck).

### Phase 3 — Acoustic enrichment backfill — ✅ VERIFIED ALREADY DONE (2026-06-19)
**Original goal:** fill the "empty" `album_tags` table and wire its consumers.
**Verification finding:** the premise was stale. `album_tags` holds **44,303 rows
across 7,148 albums** (Last.fm tags + Discogs styles/genres). Its consumers
(`genre/manifold.py` C2/C3 ensemble, `trajectory/candidates.py` album_genres, the
BM25/genre-match queries in `database_pg.py`) read it via plain SQL JOINs and are
**NOT flag-gated** — they auto-activated when the table filled. There is nothing
to implement here.
- **Residual (optional, minor):** MusicBrainz + Metal Archives contributed 0
  `album_tags` rows despite being wired — a small enrichment-coverage gap, not a phase.
- **Recommended follow-up:** re-run `eval_loop.py --multi` to quantify the lift the
  now-live C2/C3 paths give (a measurement, not a build).
- **Essentia question moved to Phase 4** — see below.

### Phase 4 — Sonic-ML bake-off: heuristics vs Essentia vs AudioMuse-AI (EVALUATION, then decide)
**Goal:** decide whether *local ML sonic features* beat the **librosa heuristic
proxies** the engine already uses (`service/app/audio/analyzer.py`: BPM, loudness,
valence, danceability, brightness, etc.), and what role AudioMuse-AI plays vs the
in-house `playlist-generator`. Essentia and AudioMuse are the **same question** —
"better sonic features via local ML" — so they are evaluated together, against the
current heuristic baseline, NOT pre-built.
- **Heuristic baseline established (2026-06-19, `eval_loop.py --multi --max-iter 1`,
  9-prompt suite, preserved in `eval_out/baseline_20260619/`):**
  **overall 5.70/10** — genre 6.7 (strongest, C2/C3 album_tags working) · arc 5.89 ·
  fidelity 5.4 · curation 5.33 · **transition 5.11 (weakest)**. Diagnosis: HIGH
  genre-drift, MEDIUM transition-weakness, MEDIUM arc-failure. **Key implication:**
  the bottleneck is *sequencing/transition*, not feature richness — and the
  diagnosis's own quick-wins are all *scoring-weight tweaks* (genre-constraint
  weight, transition component, energy/darkness trajectory emphasis), NOT "get
  better features." So sonic-ML is only justified if it beats cheap sequencer
  tuning **specifically on transition_quality**. Caveats: max-iter 1 is a floor
  (production iterates); the suite is dark/metal-heavy (shoegaze 3.80 / jazz 4.50
  worst — likely partly library composition, not algorithm).
- **Therefore — do the FREE sequencer-weight tuning first** (the diagnosis quick-wins
  in `trajectory/candidates.py` + `sequencer.py`), re-eval against 5.70. Only if
  transitions stay weak does the Essentia/AudioMuse spend earn its place.
- **Essentia arm:** spike model-based mood/genre/feature extraction on a *sample*
  of tracks; feed into the existing audio-feature consumers; re-score. Adopt only
  if it beats baseline by a meaningful margin.
- **AudioMuse-AI arm:** stand up its server + Jellyfin companion plugin in
  parallel; compare playlist quality / similarity / Instant-Mix integration.
- Outcome decides per-arm: replace, harvest specific ideas, or keep home-grown.
  **No commitment until the bake-off ships its own findings doc.**
**Risk notes:** pure evaluation; the only cost is time. Explicitly avoids the
trap of replacing working heuristics (or a working in-house engine) on hype —
informed by the C4/harmonic experience, where a fancier feature tested
*below-random* and was correctly shipped OFF.

### Phase C (continuous, parallel) — \*arr quality + Cleanuparr audit (LOW risk)
**Goal:** make sure the self-healing already installed is scoped safely.
- Audit Cleanuparr config: confirm it is scoped to the **qBittorrent/torrent path
  only** (it has no slskd support and its missing/upgrade "hunting" excludes
  Lidarr — so it does NOT replace the custom scripts).
- Verify Cleanuparr's **orphan auto-purge is not armed dangerously** given the
  prior 1.4 TB download-topology incident — start dry-run/observe.
- recyclarr: routine, no change; keep syncing Sonarr/Radarr custom formats.

## Success criteria (per loop, not per tool)

The roadmap succeeds when the **loop closes and is observable**:
1. A track played in Jellyfin produces a ListenBrainz scrobble (Phase 1).
2. ListenBrainz "Created for You" playlists appear inside Jellyfin (Phase 1).
3. `album_tags` is populated and `playlist-generator`'s dormant enrichment paths
   are live (Phase 3).
4. Acquisition flood-control incidents drop (fewer slskd 30-min bans) after the
   Tubifarry upgrade (Phase 2).
5. `ops.html` shows the loop's health end-to-end (each phase contributes a tile).

## Out of scope
- Self-hosting a ListenBrainz instance (use hosted MetaBrainz).
- Migrating off Lidarr (no viable successor in 2026).
- Any change to Jellyfin's deliberately-odd `:ro` volume mappings (standing owner instruction).
- The dump's skipped tools (Profilarr/Modmanager/Decluttarr/Maintainerr).

## Open flags carried from research (verify at phase time, not now)
- Lidarr metadata-server health is a moving target (degraded-but-functional-via-ID).
- Reddit community sentiment could not be fetched cleanly — "consensus" claims
  rest on GitHub/blogs/forums.
- A "ListenBrainz AIBrainz beta" appeared in one search result — unverified; ignore until primary-sourced.
- Confirm Jellyfin version (10.11+ gate for the ListenBrainz + AudioMuse plugins) before Phase 1/4.
