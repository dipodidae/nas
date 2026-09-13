# ADR-0039 — Trickplay: the media mount is read-write on purpose

**Date:** 2026-09-13
**Status:** accepted
**Relaxes one bit of:** ADR-0016 (Jellyfin's volume mappings) — the path is still frozen,
the `:ro` flag is not
**Same shape as:** ADR-0035 (Jellyfin config that lives outside this repo)

## What was broken

Trickplay — the thumbnail strip you get when you drag the scrub bar — had been enabled on
the Movies and TV Shows libraries and had produced **nothing at all**. Not one tile, for
either library, ever.

The user-visible symptom arrived in two stages and neither pointed at the cause. First the
scrub bar fell back to **chapter images**, which exist for 687 of 1816 videos — so some
things previewed and most did not. Then chapter-image extraction was turned off to force
trickplay to be used, which removed the fallback and left **nothing**.

## The cause

`SaveTrickplayWithMedia` was on (tiles stored next to the media file, as
`<name>.trickplay/320 - 10x10/N.jpg`), and `${SHARE_DIRECTORY}` was mounted into Jellyfin
as `:ro`.

So every generation:

1. ran the full hardware-accelerated ffmpeg extraction — **~2 minutes per episode**,
2. wrote a few hundred JPEGs into `/tmp/jellyfin/<guid>/`,
3. assembled the tile sheets,
4. and died on the **last** step, creating the output directory:

```
[ERR] Jellyfin.Server.Implementations.Trickplay.TrickplayManager: Error creating trickplay images.
System.IO.IOException: Read-only file system : '/data/movies/series/Beef/Season 2/BEEF.S02E06….trickplay'
   at …TrickplayManager.CreateTiles(…)
   at …TrickplayManager.RefreshTrickplayDataInternal(…, Boolean saveWithMedia, …)
```

**4582 of these across three days**, roughly 30 GPU-hours spent producing zero bytes. The
task was still running, on its nightly 03:00 trigger, when this was found.

## Why nothing caught it

This is the sixth instance of the house failure mode (`.claude/skills/verifying-by-effect`)
and it is worth naming precisely, because every single indicator was green:

- the **scheduled task** reported no failure — it completes normally; the per-item
  exception is caught and logged, not propagated,
- the **healthcheck** stayed green; Jellyfin was perfectly healthy,
- `make check` was satisfied — the compose model was exactly what it asserted,
- and the only trace was one `[ERR]` line per item in a log nobody tails.

An HTTP 204 from the config API "confirmed" the setting every time. The setting *was*
saved. It was simply impossible to honour.

## Decision

**`${SHARE_DIRECTORY}:/data/movies` is mounted read-write, and tiles are stored with the
media.** The alternative — leaving `:ro` and storing tiles in Jellyfin's data dir — was
implemented first and then reversed on the owner's instruction. The owner's call is also
the better one here:

| | with media (chosen) | in `/config` |
| --- | --- | --- |
| lands on | `/mnt/drive`, **3.8 TB free** | `/`, **55 GB free at 76 % used** |
| ~6 GB for this library | 0.16 % of the drive | 11 % of remaining root |
| survives a Jellyfin config wipe | yes | no |
| in the nightly config archive | no (not covered at all) | only because it is excluded |

### What this costs, stated plainly

Jellyfin can now **delete and modify media**. It could not before. That is a real
reduction in blast-radius containment and it is accepted deliberately, not overlooked.
Two things bound it: Jellyfin's delete-media permission is off for every non-admin user,
and Cleanuparr's `Orphaned Files` / `Unlinked Downloads` modules stay off (ADR-0028), so
nothing sweeps the `.trickplay` directories that now sit in the media tree.

### What did NOT change

The **path** and **source** of the mount. ADR-0016's actual concern is that
`/data/movies` is load-bearing for three systems that must move in lockstep — every
Jellyfin library path, the \*arr `mapFrom`/`mapTo` mappings, and playlist-generator's
`LOCAL_PATH_PREFIX`/`JELLYFIN_PATH_PREFIX` pair. None of those can tell `:ro` from `:rw`.
`check-invariants.sh` still asserts source and target exactly; only the expected
`read_only` bit flipped, with the reason inline so nobody "hardens" it back.

## Keyframe-only extraction: measured, not assumed

`TrickplayOptions.EnableKeyFrameOnlyExtraction` was off. Turned on, measured on this host
against South Park Specials (1080p WEB-DL, same codec, adjacent files):

| | per 45–50 min episode |
| --- | --- |
| full decode (`fps` filter over every frame) | **112 s** |
| keyframe-only (`-skip_frame nokey`) | **14–18 s** |

**~7×**, and the output is not degraded: still `320x180` thumbnails in `3200x1800`
10×10 sheets, still ~14 KB per thumbnail, and `TrickplayInfos.ThumbnailCount` is still
*exact* (279 for a 46.5-min episode at a 10 s interval = 2790 s / 10). Timing snaps to the
nearest keyframe, which for these GOPs is within a couple of seconds — invisible on a
scrub bar.

This matters beyond tidiness. Extrapolating the measured per-item numbers over 1816
videos: full decode is well over a day of continuous GPU work, which does not fit between
nightly 03:00 runs and so never converges; keyframe-only brings the episode bulk to
roughly 8 h. Both are estimates from a small sample — the measured facts are the
per-episode numbers above.

### It does not work on every file, and that is handled

Observed during the backfill, on the first Bluray Remux it reached:

```
[INF] MediaEncoder: Trickplay process unresponsive.
[INF] MediaEncoder: Stopping trickplay extraction.
[WRN] MediaEncoder: I-frame trickplay extraction failed, will attempt standard way.
      Input: "/data/movies/movies/12 Angry Men (1957)/12 Angry Men (1957) Remux-1080p.mkv"
```

This is the documented "not compatible with all decoders and/or video files" case, and
**Jellyfin falls back to full decode by itself** — no missing tiles, no failed item, no
intervention. The cost is a 20 s unresponsiveness timeout before the retry.

So keyframe-only is not a gamble: it is ~7× on everything that supports it (the WEB-DL
episodes that are the bulk of this library) and costs 20 s on the minority that does not.
Do not turn it off because of a `[WRN]` line — that line is the safety net working. The
setting to worry about would be one that made the fallback *not* happen.

## The rest of the config, and why it is left alone

`TrickplayOptions` **moved in 12.0** from `encoding.xml` (`EncodingOptions`) to
`system.xml` (`ServerConfiguration`). `GET /System/Configuration/encoding` no longer
contains it and `/System/Configuration/trickplay` is a 404 — it is
`GET /System/Configuration`. Anything scripted against the old location silently reads
nothing.

Kept at stock, deliberately:

- **Interval 10000 ms, one width (320), 10×10 tiles.** 10×320 = 3200 px per sheet, safely
  under the 4096 px texture limit that TVs and mobile browsers enforce. A 480 px width
  would make sheets 4800 px wide and break on exactly the clients hardest to debug.
- **`Qscale: 4`** is what actually controls quality. **`JpegQuality: 90` is a no-op** —
  upstream jellyfin#13391, closed as not planned. Do not tune it and expect anything.
- **`EnableHwAcceleration: true`** (VAAPI/QSV on `/dev/dri/renderD128`) — verified in use
  in the emitted ffmpeg command, and it survives keyframe-only.
- **`ProcessThreads: 2`** left at default. An attempt to measure 4 produced no usable
  sample and it is not changed on a guess.
- **`ScanBehavior: NonBlocking`, `ProcessPriority: BelowNormal`** — keeps generation off
  the critical path of a library scan and behind live transcodes.

## The tripwire

`scripts/check_jellyfin_trickplay.py`, wired into `make verify-runtime`. It asserts the
two halves **that cannot see each other**:

- the mount is genuinely writable — probed by `mkdir` inside the container, not read off
  the compose model, because the mount can be rw while the uid still cannot write;
- every trickplay-enabled library still has `SaveTrickplayWithMedia` on;
- `EnableKeyFrameOnlyExtraction` is still on;
- and `TrickplayInfos` is **non-empty** — settings can be perfect and generation still
  broken, so the check looks for output, not for configuration.

The per-library half lives in
`.docker-config/jellyfin/data/root/default/<lib>/options.xml`, which is gitignored, and
the rest in `system.xml`. A config restore or one untick in the web UI silently re-creates
the outage from the side git cannot see — which is the whole argument for a runtime
assertion here rather than a comment (`.claude/skills/nas-runtime-vs-repo`).

Proven to fail as well as pass: unticking `SaveTrickplayWithMedia` on Movies turns it red
(exit 1, naming the library), re-ticking turns it green again.

## Verification

Not a 204, not a green task:

- `find /mnt/drive/{series,movies} -name '*.trickplay'` — directories exist next to the
  media, containing `320 - 10x10/N.jpg` at `3200x1800`;
- `GET /Videos/{id}/Trickplay/320/tiles.m3u8` → `200`, with
  `#EXT-X-TILES:RESOLUTION=320x180,LAYOUT=10x10,DURATION=10`;
- `GET /Videos/{id}/Trickplay/320/0.jpg` → `200`, **1 308 668 bytes, byte-identical to the
  file on disk**;
- `TrickplayInfos` rows with correct `ThumbnailCount`;
- and zero `Read-only file system` lines after the change.

Jellyfin's own **`Migrate Trickplay Image Location`** task (`MoveTrickplayImages`) moved
the tiles generated during the interim `/config` configuration into the media tree — a
move, not a copy: the data dir went to 0 tiles.
