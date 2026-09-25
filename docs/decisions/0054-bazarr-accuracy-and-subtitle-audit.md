# ADR-0054 — Bazarr for accuracy, and subtitles checked against the audio

**Date:** 2026-09-25
**Status:** accepted
**Extends:** the Bazarr notes in `AGENTS.md` (subcleaner, HI-satisfies-non-HI) and
`docs/jellyfin-playback-audit.md` §3.5 (providers disabled for failed logins)

## Context

"Poirot's subtitles are all desynced, while Jellyfin's own search finds the right ones."
Measured on Agatha Christie's Poirot (70 episodes, 24 fps Blu-ray, no embedded subs):

| What Bazarr had                                                                                        | Effect                                                                                                                             |
| ------------------------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| **OpenSubtitles.com and Addic7ed off** since 2026-09-02 (`Login failed`, stale cookie)                 | Left with subf2m (old Subscene uploads, no release info), YIFY, tvsubtitles. 49 of 70 current Poirot subs came from subf2m         |
| Jellyfin's OpenSubtitles plugin **logged in the whole time** (`dipodidae`, `CredentialsInvalid=false`) | Jellyfin's search did find better subtitles, because it could search the biggest source and Bazarr could not                       |
| `whisperai` as an **auto-download** provider, `minimum_score: 60`                                      | Whisper transcripts score 220/360 = 61%, so 69 of them were auto-downloaded for Poirot                                             |
| sync with **`no_fix_framerate: true`**                                                                 | ffsubsync could shift but never stretch. S01E03 needed 0.960 (= 24/25), S03E07 1.036: PAL-DVD timings, slide ~2 min by the credits |
| a 9-language "Foreign + Lingarr" profile on 25 series / 26 movies                                      | 1 Dutch subtitle in the whole library                                                                                              |

And the case no setting can reach: **Season 3 was mis-numbered in a subf2m season pack**
(`…/english/2329921`, re-uploaded to subdl). S03E07 carried _The Mystery of the Spanish
Chest_ (S03E08). Bazarr then "synchronised" it. The history said 94.72%, the sync log
reported an offset, and every layer looked healthy.

## Decision

### 1. Bazarr config (all applied live over `/api/system/settings`)

- **Providers:** `opensubtitlescom` (Jellyfin's working login, hash matching on),
  `subsource` (subbuzz's API key), `gestdown` (Addic7ed without the cookie), `subdl`,
  `embeddedsubtitles`. **Off:** `subf2m`, `yifysubtitles`, `tvsubtitles`,
  `supersubtitles`, `betaseries` (French) and `whisperai`, because generated subtitles are not wanted.
  **Podnapisi cannot be added:** Bazarr 1.6 deleted it (`config.py`: "this provider
  doesn't exist anymore"). All five report `Good`. OpenSubtitles returned 10 English
  candidates for S03E07 on the first search, against zero before.
- **Scores (TRaSH):** minimum 90 series / 80 movies. **Sync:** on, thresholds 96 / 86
  (TRaSH; higher re-syncs already-good subtitles into being slightly off), and
  **`no_fix_framerate: false`**.
- **Correction mods:** `common`, `OCR_fixes`, `remove_HI`, `fix_uppercase`. `remove_tags`
  is left off because it would strip italics too. subcleaner post-processing stays on.
- **Deep audio analysis** (`parse_embedded_audio_track`) on; upgrade window 7 → 30 days.
- **Profile "NL + EN"**, both always (no cutoff), on all 82 series and 70 movies, and
  the default for new ones. "Foreign + Lingarr" was removed from Bazarr; Lingarr itself
  is untouched. "Standard EN" is kept, unused, as the one-click rollback.
- Pinned by `scripts/check-bazarr-config.py` in `make verify-runtime`. Every value
  lives in gitignored `config.yaml` / `bazarr.db`.

### 2. `scripts/subtitle_audit.py`, hourly (`:33`)

Scores and sync cannot see a wrong episode or a different cut. This checks the one
thing that matters: are the subtitle's words spoken at the subtitle's times? It uses
three 60 s clips (20/50/80%, seeked, not read through), transcribed by the existing
`whisper` container **as a measuring instrument only**, and matches each spoken line
to a cue. Verdicts:

- **GOOD:** every window within 0.75 s. Whisper's segment starts jitter by about 0.3 s.
- **FIXABLE:** one line `audio = a·sub + b` explains every window within 0.5 s (with 3
  windows; with 2, only a constant shift or a real framerate ratio). The cue times are
  rewritten, then **verified at four other points**. If they don't all agree, the
  original is restored and the result is treated as UNFIXABLE.
- **MOSTLY:** right in most windows and off in one. Reported and left alone. _Five Little
  Pigs_ is −1.3 s at 19 min and ±0.3 s after; a line through that would make the good
  parts worse.
- **WRONG / UNFIXABLE:** the file goes to `backups/subtitle-audit/`, and Bazarr's
  `/api/…/blacklist` deletes it, blacklists that exact `provider + subs_id`, and
  re-searches.
- **UNSURE:** too little speech. Re-checked after 30 days.

A subtitle in a different language from the audio (NL on English, EN on Korean) can't
be text-matched. It gets a speech-onset check, which is reported and never acted on.
Verdicts are remembered per file size + mtime.

### 3. Replacing, not just removing

Blacklisting one file does not stop Bazarr from choosing its **sibling**. After the
first pass, SubDL's replacements for Poirot S03 were the same mis-numbered pack
re-uploaded (`…english-2329921.zip`, one file per episode, each with its own
`subs_id`), and 7 of 8 came back WRONG again. Then SubSource offered a third copy
(`651487`). And with OpenSubtitles over its daily quota, S03E07's only candidates were
four 86% Gestdown DVD rips: below the 90 minimum, so Bazarr's own search would leave
the episode with no English subtitle at all.

So the audit drives replacement itself. Every removed subtitle is tracked (state key
`_replacing`) together with the **pack id** it came from (the ≥6-digit upload id, which
survives re-uploads across providers). While nothing has replaced it, the audit takes
Bazarr's manual-search candidates, skips anything from a known-bad pack and anything
already tried, and queues the best one, **ignoring the minimum score**: the audit
decides, not the score. The next run measures what landed. It makes at most 5 attempts,
then rests a day (OpenSubtitles' quota resets, and new uploads appear).

Provider and `subs_id` for the blacklist are read from `bazarr.db` read-only, not from
`/api/episodes/history`. That endpoint inner-joins Bazarr's subtitle index, so right
after a subtitle is deleted or replaced the episode's whole history disappears from it:
0 rows for S03E07, an hour after it returned 5.

### 4. Wrong here is often right next door

Every provider's Poirot S03 subtitles were WRONG, even Gestdown's (Addic7ed) DVD rip,
and for one reason: **TVDB counts the feature-length _Mysterious Affair at Styles_ as
S03E01, and every subtitle site leaves it out of Season 3**, so their "S03E*n*" is
Sonarr's S03E*n+1*. Measured: Gestdown's "S03E10" matched S03E11's audio 15/24 and at
most 2 lines against every other Season 3 video; the pack's "S03E07" is S03E08's.

So a WRONG subtitle is tried against its neighbours (±1, then ±2, in the same
season) with one mid-episode window before it is discarded. If the spoken lines are
there, it moves to that episode, unless that episode already has a subtitle the
audit has not judged bad. The next run measures it in full, so it is retimed if
need be. Bazarr cannot do this: it only ever searches by the episode's own number.

## What was measured

- Poirot S03 (11 subs): **8 WRONG**, 7 of them from the one mis-numbered pack, 1 MOSTLY,
  1 UNSURE. The detector was cross-checked: S03E07's file matches the S03E08 _video_
  (lines found, slope 0.9626 ≈ 24/25).
- The retimer on that file went from +15.4 s / −22.7 s to +0.11 s / +0.14 s at the
  points it was fitted on, **and 5 s off at 37 min**: the DVD cut differs. The first
  version of the audit "verified" on the same points it fitted and called it GOOD; the
  held-out verification exists because of that. Re-run: UNFIXABLE, original restored
  (md5 unchanged).
- Independent methods disagree by design. ffsubsync's whole-file VAD said −49 s / 1.017
  after Bazarr's own sync on a wrong-episode file, which is meaningless there. The
  text match is the ground truth; ffsubsync is not used to judge.

## Consequences

- The profile change queued ~200 Bazarr jobs (NL for everything). Each download syncs,
  and each sync reads the whole audio track, so the USB disk is busy for a while.
  `jellyfin_subtitle_prewarm.py` and this audit both tolerate it.
- OpenSubtitles' free tier has a daily download quota. Bazarr throttles the provider
  when it is hit and resumes the next day; the NL backfill is quota-bound, not broken.
- Whisper is CPU-only (`small`). About 20 s per subtitle; ~815 subtitles is ~5 h,
  spread over the hourly 25-minute budget.

## Rollback

Bazarr config and DB from before this change:
`/mnt/drive/backups/nas-config-backups/bazarr-pre-provider-overhaul-20260925-*/`.
Every file the audit touched: `/mnt/drive/backups/subtitle-audit/<same relative path>`.
Remove the `subtitle-audit` cron line to stop it.
