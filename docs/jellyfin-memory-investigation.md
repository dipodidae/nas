# Jellyfin memory: is the blowup ffprobe fan-out during library scans?

**Answer: no.** Measured 2026-09-02. This is a negative result, which is the
point of writing it down — the hypothesis is eliminated on evidence rather than
left open, and the mitigations in ADR-0008 are reclassified accordingly.

## The hypothesis under test

Multiple upstream reports — jellyfin/jellyfin#16048 and #16549 — attribute
10.11.x memory blowups to **ffprobe fan-out during library scans** rather than
to a slow leak in the server process. Memory climbs to saturation during a
refresh, and the reporters' own mitigation was capping the parallel scan and
image-extraction thread counts to 2, after 16 GB and then 24 GB of extra
headroom made no difference.

That maps suspiciously well onto this box: weekly per-library scans at 05:05 on
Fri/Sat/Sun, kills every 2–12 h, ~22–24 GiB anon-RSS peaks, `mem_limit: 10g` as
a blast-radius cap. If it held, the cure would be a concurrency setting, not a
containment strategy.

## What decided it: the kernel already knew

The six recorded OOM kills, from `journalctl -k -b all`:

| #   | when                | victim          | anon-rss      | total-vm |
| --- | ------------------- | --------------- | ------------- | -------- |
| 1   | Sun 30 Aug 10:15:40 | `task=jellyfin` | 24,042,904 kB | 276.9 GB |
| 2   | Sun 30 Aug 22:01:26 | `task=jellyfin` | 23,167,652 kB | 277.3 GB |
| 3   | Mon 31 Aug 10:10:40 | `task=jellyfin` | 23,843,220 kB | 277.0 GB |
| 4   | Mon 31 Aug 19:40:40 | `task=jellyfin` | 23,523,020 kB | 277.7 GB |
| 5   | Mon 31 Aug 21:57:16 | `task=jellyfin` | 22,763,820 kB | 277.2 GB |
| 6   | Tue 01 Sep 05:34:58 | `task=jellyfin` | 23,483,348 kB | 278.1 GB |

Two things fall out, and either alone refutes the hypothesis.

**1. The victim is always ONE process, and it is always the server.**
Every kill names `task=jellyfin` holding 22.8–24.0 GB of _anonymous_ memory in a
single process. An ffprobe fan-out is many children each holding a modest RSS;
the OOM killer picks the largest, so under that mechanism it would eventually
name `ffprobe`. It never did — `ffprobe` appears nowhere in the kernel log, and
`ffmpeg` appears exactly twice, both times as an unrelated segfault (see below).

**2. Not one kill coincides with a scan.** The scan crons are Fri 05:05
(Movies), Sat 05:05 (TV Shows), Sun 05:05 (Music). The kills are Sunday 10:15,
Sunday 22:01, Monday 10:10, Monday 19:40, Monday 21:57 and **Tuesday** 05:34 —
a day with no scan cron at all. Zero of six are at or near a scheduled scan.

So the growth is in-process, in anonymous memory, in the server itself, and it
is not triggered by scanning.

## Run A — a deliberate Movies scan, for completeness

Triggered by hand at `2026-09-02T17:56:27Z` via
`scripts/jellyfin_library_scan.py --library Movies`, with no playback sessions
active (`/Sessions` reported 2 connected, 0 playing) and the v4 sampler running
at 15 s intervals.

|                 | anon                              | mem_current | ffprobe      | ffmpeg       |
| --------------- | --------------------------------- | ----------- | ------------ | ------------ |
| n=27 over 6 min | 411 / 414 / 423 MB (min/mean/max) | 543 MB peak | 0 throughout | 0 throughout |

**Stated plainly: this run does not test fan-out under load.** The script posts
`metadataRefreshMode=Default&imageRefreshMode=Default`, which probes only new or
changed items — and nothing had changed, so nothing was probed. It measures a
scan with no work to do.

**Why it was not escalated to a forced refresh:** `replaceAllMetadata=true` /
`FullRefresh` would re-scrape the entire Movies library. That is a real risk to
this specific library, which had a wrong-match incident corrected only the day
before (`/data/movies/series/3%` identified as "3 Body Problem" — see
`docs/jellyfin-playback-audit.md`), and re-scraping is exactly how such a match
gets re-broken. Given that the kernel evidence above already refutes the
hypothesis on two independent grounds, spending that risk to strengthen an
already-decided answer was not worth it. Recorded as a deliberate limit of the
experiment rather than an oversight.

## Post-mitigation behaviour, 1,870 samples

Everything since the v3 sampler started, `2026-09-01T12:24` →
`2026-09-02T18:03` (30 h, one-minute cadence):

```
anon         min 15 MB    mean 716 MB    max 2,198 MB
mem_current                              max 7,441 MB
ffprobe/ffmpeg children present in 0 of 1,870 samples
arena_regions 0, doublemapper 0 throughout
```

Peak anon **2.20 GB** against a pre-mitigation 23–24 GB, and **zero OOM kills**
since 2026-09-01 05:34 — the same day the three mitigations landed.

## Two instrumentation defects, both caught before any conclusion

Worth recording because each would have produced a confident wrong answer:

1. **`scanning=` was reading the wrong mechanism.** It checked the
   `RefreshLibrary` scheduled task, but this host scans via
   `jellyfin_library_scan.py`'s per-library `POST /Items/{id}/Refresh`, which
   leaves that task `Idle` — the script's own docstring says so. The field would
   have recorded `scanning=no` through every real scan, forever, while looking
   like a working field. It now checks each virtual folder's `RefreshStatus`
   too.
2. **`scanning=` was always `NA` under cron.** The sampler read `.env` through
   python-dotenv, but its cron line runs `/usr/bin/python3`, not the venv. It
   worked by hand and never on schedule — the same shape as the missing `cd`
   that killed `media_ops_status.py` for three months. Replaced with a
   dependency-free reader.

The general rule, already in AGENTS.md, earned twice in one afternoon: when a
check passes, ask whether it proves the property you care about or just the
component that carries it.

## What the mechanism probably is

In-process native growth in one process, with a 277 GB virtual address space
against 23 GB resident — a very large number of mappings, not one runaway
allocation. That is the signature the two remaining mitigations target:

- **glibc malloc arena fragmentation** (dotnet/runtime#122027) — native RSS
  grows while the managed heap stays flat. glibc sizes arenas from the _host's_
  16 online CPUs, not the cgroup quota, which is why `mem_limit` alone could
  never restrain it. `MALLOC_ARENA_MAX=2`.
- **.NET W^X double-mapping** of JIT'd code (dotnet/runtime#89776, #121455) —
  `memfd:doublemapper` mappings accumulating. `DOTNET_EnableWriteXorExecute=0`.

The sampler reports `arena_regions=0` and `doublemapper=0` throughout the
post-mitigation window, consistent with both being suppressed.

## Honest limits of this conclusion

- **No A/B.** The sampler's history begins after the mitigations landed, so
  there is no pre-mitigation series from this instrument to compare against. The
  before-numbers come from the kernel, not from the sampler.
- Therefore "the kills stopped when the mitigations landed" is a strong temporal
  association, not a proven causation. Settling it would mean reverting one
  mitigation and waiting for a 24 GB excursion — deliberately provoking another
  host-wide OOM cascade, which is not worth the answer.
- 30 h and 1,870 samples is a decent window against a 2–12 h kill cadence
  (5–15 kills would have been expected), but it is not months.

What would reopen it: an OOM kill naming `jellyfin` again, or `anon` climbing
past ~4 GB while `arena_regions` or `doublemapper` go non-zero.

## Unrelated finding: two ffmpeg segfaults

```
ffmpeg[3121637]: segfault at 28 ip ...d17 error 4 in libavcodec.so.61
ffmpeg[3136211]: segfault at 28 ip ...d17 error 4 in libavcodec.so.61
```

Same instruction pointer offset, same fault address, `error 4` (user-mode read
of an unmapped address) — so this is one reproducible crash in `libavcodec`, hit
twice, not random corruption. It is not the memory problem and did not cause any
OOM kill, but it means some transcode or extraction died mid-operation. Logged
here rather than chased; if a specific title fails to play, start with this.
