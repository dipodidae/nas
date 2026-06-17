# Smarter import salvage for the Lidarr↔slskd pipeline

**Date:** 2026-06-17
**Status:** approved (Approach A)

## Problem

Lidarr searches → Tubifarry grabs → Lidarr then **rejects its own grab** and the row
wedges in the queue as `importFailed`. The hourly `lidarr_queue_unstick.py` blocklists +
removes it, and the paced drip re-searches it later — a wasteful grab → reject → re-grab
loop that also feeds the Soulseek search volume behind the 30-min flood bans.

Observed `importFailed` reason mix (snapshot): "Album release not requested" (the bulk),
"Album match is not close enough: X % vs 80 %", "Has missing tracks", "Has unmatched
tracks", "Not an upgrade", "Has fewer tracks than existing".

Most of these are salvageable: the grab is a *complete, valid* album, just a different
MusicBrainz **edition** than Lidarr arbitrarily monitored, or a slightly-imperfect match.
Lidarr's automatic pipeline deliberately won't release-switch and has no "accept ≥ N%"
knob, so they sit wedged.

## Goal

Make the queue drainer **try to import what was already fetched** before blocklisting +
re-grabbing. Accept the salvageable buckets; only re-grab the genuinely-bad ones.

## Acceptance policy (user-chosen)

Salvage (import) when the *only* blocking reasons are:
- **Album release not requested** — re-import with `disableReleaseSwitching: false`
- **Album match not close enough ≥ 70%** (`--accept-min-match`, default 70)
- **Has unmatched / extra tracks**

Re-grab (blocklist) when any reason is:
- not-close-enough **< 70%**
- **Has missing tracks** (partial album — strict for queue salvage)
- Not an upgrade / Couldn't find similar / Destination exists / Has fewer tracks than existing

## Approach A — broaden the unstick reclaim pass; extract a shared policy module

### Components

1. **`scripts/lidarr_import_lib.py` (new, pure — no I/O).** Single source of truth for
   "good enough":
   - `build_import_item(file_info)` — `/manualimport` entry → `ManualImport` payload item
     (`disableReleaseSwitching: false`), or `None` when ids/tracks are missing.
   - `classify_reasons(reasons, *, accept_min_match, accept_missing_tracks,
     block_fewer_tracks)` → `(acceptable, blockers)`. Parameterized so the two callers
     differ only where they must.
   - `release_track_count(file_info)`, `stub_coverage(...)` — incomplete-download guard.
   - `select_importable_items(entries, *, accept_min_match, accept_missing_tracks,
     block_fewer_tracks, min_track_fraction)` → `(items, stub_skip_reason | None)`.
   - Constants: `NOT_CLOSE_PCT_RE`, `ALWAYS_BLOCKERS`, `DEFAULT_ACCEPT_MIN_MATCH = 70.0`.

2. **`process_soulseek_imports.py` (orphan importer) — behavior preserved.** Its local
   `_build_import_item`, `stub_coverage`, `_release_track_count` delegate to the lib;
   `_evaluate_rejections` becomes a thin wrapper calling `classify_reasons(...,
   accept_missing_tracks=True, block_fewer_tracks=False)` — byte-identical decisions, so
   its tests stay green. The daily orphan job keeps accepting partial albums (≥50% via
   its stub guard); only the queue path is strict.

3. **`lidarr_queue_unstick.py` (queue drainer) — broadened.**
   - `is_reclaimable` → `is_salvageable(item, *, accept_min_match)`: cheap pre-filter on
     the queue **messages** (parse the `64.6 %` straight out, skip sub-floor without a slow
     scan), using `classify_reasons(..., accept_missing_tracks=False,
     block_fewer_tracks=True)`.
   - `reclaim_item` → `salvage_item(...)`: scan `/manualimport`, run
     `select_importable_items` (strict policy), import the acceptable files with release
     switching, **verify via track-file count delta** (existing robust check), keep the
     orphan in-place re-register fallback. Returns True only when track files actually
     appear.
   - New `--accept-min-match` (default 70). `--no-reclaim` retained as a `--no-salvage`
     alias.
   - Salvaged rows cleared with `blocklist=false, skipRedownload=true` (no re-grab);
     unsalvageable + failed-salvage fall through to today's `blocklist + remove`.

### Data flow

```
importFailed rows (age-gated)
  → is_salvageable?  ── no ─────────────→ blocklist + remove  (re-grab via drip)
        │ yes
        ▼
   salvage_item: /manualimport scan → select_importable_items (≥70%, extra OK,
                 edition-switch, reject missing/fewer/<70%) → ManualImport(copy,
                 release-switch on) → track-file delta verify
        │ imported          │ nothing imported / stub / scan fail
        ▼                   ▼
  clear row (no blocklist)  blocklist + remove  (re-grab via drip)
```

### Error handling / safety

- `--dry-run` unchanged (now reports `salvage` vs `remove`).
- `--max-actions`-equivalent: the existing `--min-age-hours` grace still gates eligibility;
  salvage scans are slow (fingerprinting), so the hourly cap is the eligible set.
- Track-file-delta verification means a no-op import never clears a row.
- Stub guard (`min_track_fraction`) blocks incomplete-release imports that slip past the
  message-level check.

### Testing

- `scripts/tests/test_lidarr_import_lib.py` — policy truth table for `classify_reasons`
  (each bucket × both caller policies), `build_import_item` guards, `stub_coverage`,
  `select_importable_items` (accept/reject/stub).
- `process_soulseek_imports` existing tests must stay green (delegation, no behavior
  change).
- `lidarr_queue_unstick` tests extended: `is_salvageable` truth table; salvage flow with
  stubbed HTTP (imported → no blocklist; failed → blocklist).

## Out of scope

- Accepting partial albums into the library from the queue path (the "maximally lenient"
  option) — explicitly rejected.
- Changing Lidarr's global match threshold or the automatic pipeline.
