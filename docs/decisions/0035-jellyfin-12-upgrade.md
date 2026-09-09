# ADR-0035 — Jellyfin 12.0: four things the version bump broke silently

**Date:** 2026-09-09
**Status:** accepted
**Supersedes the pin in:** ADR-0006 (`10.11.11ubu2604-ls47` → `12.0ubu2604-ls48`)
**Same `CAP_KILL` mechanism as:** ADR-0004 (qbittorrent), ADR-0021 (nginx)

## The upgrade

`lscr.io/linuxserver/jellyfin:10.11.11ubu2604-ls47` → `12.0ubu2604-ls48`.
Jellyfin dropped the leading `10.` from its version scheme, so what would have
been 10.12.0 ships as **12.0**. Direct 10.11.x → 12.0 is supported; the schema
changes and data is rewritten on first boot, so **a backup is the only way
back**. 30 migrations applied in ~35s (18 `CoreInitialisation`, 13
`AppInitialisation`, one `RefreshCleanNamesAndValues` over 195,954 items) with
no failures, and the library came through intact: 5 users, 3 libraries on the
unchanged ADR-0016 paths, 59 movies / 38 series / 1180 episodes / 15,676 albums
/ 153,240 songs.

Nothing restarted Jellyfin mid-migration because it carries no `autoheal=true`
label — worth keeping that way, since the health check is red for the whole
migration and an autoheal restart lands in the middle of a schema rewrite.

The pre-upgrade backup is
`/mnt/drive/backups/jellyfin-pre-12/configs-20260909-194137.tar.gz` (476 MB
gzipped, 566 files) — DB, XML config, `users/`, and the 20 pre-12 plugin
directories, with `data/metadata/**` excluded deliberately (17 GB, regenerable,
and the required post-upgrade scan rebuilds it anyway).

## 1. Jellyfin never shut down gracefully, and nobody could have noticed

Same mechanism as ADR-0004 and ADR-0021, third service. s6-overlay runs as root
and must signal `jellyfin`, which runs as `abc` (uid 1000). `cap_drop: ALL` plus
the `svc-lsio` fragment's four capabilities did not include `KILL`, so the
kernel refused every signal — being root is not sufficient, the check is on the
capability.

Probed rather than inferred, from root inside the running container:

```
target pid=182 uid=1000
$ kill -0 182   ->  sh: 1: kill: Operation not permitted
```

Measured both ways, which is the whole argument for fixing it before migrating a
1.1 GB SQLite database:

|                 | without `KILL`                      | with `KILL`                         |
| --------------- | ----------------------------------- | ----------------------------------- |
| stop duration   | 10.5 s (the full grace period)      | 3.66 s                              |
| exit code       | 137 (SIGKILL)                       | 0                                   |
| shutdown logged | **nothing**                         | `Disposing "CoreAppHost"` … et al.  |
| SQLite WAL      | 4.2 MB left dirty on **every stop** | checkpointed; `-wal`/`-shm` removed |

The WAL row is the reason this matters beyond tidiness. Every stop this stack
has ever performed left an un-checkpointed WAL, so any backup taken by copying
`jellyfin.db` alone was silently stale — the WAL-mode trap this repo already
documents for `*arr` databases, reached by a different route. The pre-12 backup
above was taken **after** granting `KILL`, from a cleanly closed database.

Do not remove `KILL` from jellyfin's `cap_add`.

## 2. Diun would have stopped reporting Jellyfin updates, quietly

`scripts/emit_diun_manifest.py` filtered jellyfin's tags with
`^\d+\.\d+\.\d+ubu\d+-ls\d+$` — three mandatory numeric components, written when
`10.11.11` was the shape of a release. `12.0ubu2604-ls48` has two, so it matched
neither the filter nor the manifest's own `name:`. That does not fail: diun
would simply have reported no jellyfin updates, forever, which is precisely the
failure ADR-0024 exists to prevent.

The patch component is now optional, and the fix was checked against tags that
must and must not match:

| tag                               | old filter | new filter |
| --------------------------------- | ---------- | ---------- |
| `12.0ubu2604-ls48`                | no         | **yes**    |
| `12.0.1ubu2604-ls50`              | yes        | yes        |
| `10.11.11ubu2604-ls47`            | yes        | yes        |
| `nightly-2026090709ubu2604-ls100` | no         | no         |
| `12.0-rc1ubu2604-ls40`            | no         | no         |

## 3. Legacy authorization is off after migration, and it breaks four consumers

12.0 ships `EnableLegacyAuthorization` off and **its migration turns it off on
existing installs**. Measured on the live server with the flag off:

| mechanism                            | `/Users` | `/Library/Media/Updated` |
| ------------------------------------ | -------- | ------------------------ |
| `Authorization: MediaBrowser Token=` | 200      | 204                      |
| `X-Emby-Token`                       | 401      | 401                      |
| `X-MediaBrowser-Token`               | 401      | 401                      |
| `?api_key=`                          | 401      | 401                      |

Four consumers here used the deprecated forms. Two are third-party and cannot be
changed from this repo:

- **Jellyseerr** — proven broken, not inferred: `401` and `Sync interrupted` at
  19:50 UTC, then `Recently Added Scan Complete` at 19:55 once the flag was back
  on.
- **Sonarr/Radarr** `MediaBrowser` notifications — send `X-MediaBrowser-Token`,
  so the entire import/delete → Jellyfin chain of ADR-0016 and the
  `lidarr_jellyfin_bridge.py` cron would have gone to `401`. This is the
  fail-silent shape that repo already has a bridge, a cron job and a runtime
  assertion for.

So **`EnableLegacyAuthorization` is on, deliberately**, and it is a
third-party-compatibility flag, not a preference. It lives in
`.docker-config/jellyfin/system.xml`, outside the repo — a config restore or a
fresh container can drop it, which makes it exactly the class of setting
`make verify-runtime` exists for (see the `nas-runtime-vs-repo` skill).

Everything in this repo was moved to the non-deprecated scheme anyway, so the
flag only ever has to cover other people's code:

- `webapps/lidarr-bulk/server/utils/jellyfin.ts`
- `webapps/jellyfin-playlist-generator` (submodule, 5 call sites) — verified
  end-to-end **with the flag off**: `POST /settings/test/jellyfin` →
  `{"ok":true,"message":"HTTP 200"}` while `X-Emby-Token` returned 401 in the
  same window, so the fix does not merely ride on the flag
- `.env.example`'s documented `curl` recipe
- `scripts/jellyfin_notify.py` — **deleted**. Unreferenced by any script, cron
  entry or Makefile target, and superseded by `jellyfin_library_scan.py`, which
  drives the per-library endpoint with the modern header.

## 4. One `xsi:nil` in `encoding.xml` reset the whole transcoding config

12.0 rejects `<EncoderPreset xsi:nil="true" />` with `Instance validation error:
'' is not a valid value for EncoderPreset`. The consequence is not a skipped
field — the **entire file** fails to load, Jellyfin boots with encoding
defaults, and then persists those defaults over the real settings:

```
[ERR] BaseConfigurationManager: Error loading configuration file: /config/encoding.xml
```

`HardwareAccelerationType` went `vaapi` → `none` and `HardwareDecodingCodecs`
lost `hevc`, `mpeg2video`, `vp8`, `vp9` and `av1`. Hardware transcoding was off,
on a box with `/dev/dri` passed through and a `group_add` for the render GID
specifically to have it. Nothing alerted; the only trace was one `[ERR]` line at
startup.

### What the settings are now, and why

The recovered file was not restored verbatim — it was where the invalid value
came from, and its values were not traceable to anything. The configuration is
**12.0 stock** except where a probe on this hardware justifies otherwise. The
hardware is an i5-12600H / Alder Lake-P GT2 (Iris Xe), enumerated with `vainfo`:
decode for H264, HEVC Main/Main10/Main12, VP9 0–3, VP8, MPEG2, VC1 and **AV1
Profile0**; encode for H264, HEVC, VP9, MPEG2 — **AV1 decode-only** (`VLD`, no
`EncSlice`).

| setting                            | stock     | here                  | why                                                                                                                                                                                                                                                    |
| ---------------------------------- | --------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `HardwareAccelerationType`         | `none`    | `qsv`                 | QSV device init verified OK; Intel-native, and the only path that gives working tone mapping                                                                                                                                                           |
| `QsvDevice`                        | `''`      | `/dev/dri/renderD128` | `/dev/dri` also holds `card1`; naming the render node removes the ambiguity                                                                                                                                                                            |
| `HardwareDecodingCodecs`           | h264, vc1 | all 7                 | each one confirmed `VAEntrypointVLD` in `vainfo`                                                                                                                                                                                                       |
| `AllowHevcEncoding`                | false     | **true**              | `HEVCMain`/`Main10` `EncSlice` present                                                                                                                                                                                                                 |
| `AllowAv1Encoding`                 | false     | false (stock)         | AV1 is `VLD`-only — enabling it would silently drop to a _software_ AV1 encode                                                                                                                                                                         |
| `EnableIntelLowPowerH264HwEncoder` | false     | **true**              | H264 `EncSliceLP` present (Gen12 VDEnc)                                                                                                                                                                                                                |
| `EnableIntelLowPowerHevcHwEncoder` | false     | **true**              | `HEVCMain` `EncSliceLP` present                                                                                                                                                                                                                        |
| `EnableVppTonemapping`             | false     | **true**              | the only tone-map path that works here                                                                                                                                                                                                                 |
| `EnableTonemapping` (OpenCL)       | false     | false (stock)         | **measured**: OpenCL init fails, `Failed to get number of OpenCL platforms: -1001`. This is the trap — it is the better-quality option and it would have broken HDR. See the note below for _why_, so nobody re-tests it hoping for a different answer |
| `EnableThrottling`                 | false     | **true**              | bounds a runaway transcode on the box whose Jellyfin memory growth is ADR-0008                                                                                                                                                                         |
| `EnableSegmentDeletion`            | false     | **true**              | the HLS temp dir lives on the root LV, at 78% used                                                                                                                                                                                                     |

Everything else is stock on purpose. `EnableDecodingColorDepth10HevcRext`,
`DeinterlaceMethod: bwdif` and `EnableAudioVbr` were tried and **reverted** —
the hardware supports the first, but none of the three is justified by a
capability or by this host's state, and "stock unless measured" is the rule that
keeps this file reviewable.

Verified by effect rather than by a `204`, with a forced 640×360 transcode of a
1080p remux:

```
-init_hw_device vaapi=va:/dev/dri/renderD128,driver=iHD -init_hw_device qsv=qs@va
-hwaccel vaapi -hwaccel_output_format vaapi          # hardware decode
-codec:v:0 h264_qsv -low_power 1                     # hardware LP encode
-vf scale_vaapi=...,hwmap=derive_device=qsv,format=qsv   # never leaves the GPU
```

699 fps, 27.8× realtime, no software fallback and no errors in the ffmpeg log.

### Why OpenCL tone mapping cannot work here, and why that is not worth fixing

The `-1001` above is **not** a hardware limitation — it is a packaging fact about
the LSIO image, and stating it precisely matters because otherwise it invites a
re-test. Verified inside the container:

```
/etc/OpenCL/vendors/   ->  nvidia.icd ONLY
libigdrcl.so           ->  absent from the whole filesystem
dpkg                   ->  ocl-icd-libopencl1 (the generic ICD *loader*) but
                           NOT intel-opencl-icd (the Intel runtime)
```

So `-1001` means "no OpenCL platform is registered", not "this GPU cannot". It is
fixable with an LSIO `universal-package-install` mod, and it is **deliberately not
fixed**: that adds a network-dependent package install to every Jellyfin start, and
the entire benefit would apply to **2 files out of 1214** in this library — one of
which is Dolby Vision Profile 7, where Jellyfin discards the enhancement layer and
tone maps the HDR10 base layer anyway.

For the same reason `HardwareAccelerationType` stays `qsv` rather than moving to
`vaapi`, even though this box _can_ do Vulkan/libplacebo tone mapping (Jellyfin logs
`supports Vulkan DRM interop`, `libplacebo` is in its filter list, and
`-init_hw_device vulkan=vk` initialises): that path is reachable only under the VAAPI
backend, so taking it would re-litigate a proven QSV chain — which also serves 352
PGS subtitle burn-ins through `overlay_qsv` — for 0.16% of the library.

One lever that IS left on the table, recorded rather than taken: `EncoderPreset:
auto` resolves to `-preset veryfast` for QSV (TargetUsage 7, the fastest and lowest
quality point), which is what the measured transcode above actually ran. `slow`
(≈TU3) would trade some of the 27.8× headroom for quality — but in `-low_power 1`
VDEnc mode TargetUsage has reduced effect and the extended-lookahead and B-pyramid
tools are unavailable, so the gain may be nil. Measure fps and picture on a 1080p
HEVC transcode before keeping it; do not change it on faith.

## Plugins

Upstream says remove third-party plugins before migrating; 10.11-built plugins
do not load on 12.0. All 20 were moved to `data/plugins.pre-12.0` (kept, not
deleted) and 15 reinstalled at their 12.0-targeting versions. **`Last.fm` and
`subbuzz` have no 12.0 build yet** (newest target 10.11.9.0 / 10.11.11.0) and
`Collection Sections` and `Custom Tabs` are in no configured repository — those
four stay uninstalled until upstream ships.

Plugin _configurations_ were merged rather than restored wholesale: 27 pre-12
configs came back for their credentials (Last.fm, OpenSubtitles, subbuzz,
Fanart), while the three that 12.0 had already regenerated were kept, because
12.0's `Jellyfin.Plugin.Tmdb.xml` is a superset — it has `ImportUnairedEpisodes`
and five other options the pre-12 file predates.

## A full library scan is required

Alternate-version storage changed, so a full scan is mandatory after upgrading
and items may legitimately appear newly added. Triggered post-upgrade; expect it
to run long against 153,240 songs.
