# ADR-0046 — The album-art job was going backwards, and said `exit 0` every week

**Date:** 2026-09-16
**Status:** accepted
**Relates to:** ADR-0033 (lanes and alerting), `.claude/skills/hunting-silent-failure`

## Context

`scripts/album_art.py --overwrite-once` runs weekly (Sun 04:45) and had been reporting
success every run. It was losing ground. Its own logs, read three runs back:

| run | already marked done | unmarked backlog | processed | newly marked | exit |
| --- | ------------------- | ---------------- | --------- | ------------ | ---- |
| A   | 15098               | 1018             | 300       | +82          | 0    |
| B   | 15180               | 1296             | 300       | +116         | 0    |
| C   | 15296               | 1573             | 300       | +145         | 0    |

300 folders of work bought 82–145 folders of progress, and the backlog grew by ~278 a
week. On 2026-09-16 the library held **18,593 album directories, 969 with no `folder.jpg`
at all** — and Navidrome's own database agreed: 912 albums with neither embedded nor
external art, 941 audio folders with an empty `image_files`.

Two independent faults, each of which reported success.

## Fault 1 — an unfindable album re-entered the batch forever

The batch was `unmarked[:limit]` of an alphabetically sorted list, and a folder that
ended a run with no cover **stayed unmarked on purpose**, so it would be retried. That is
a reasonable rule for a transient miss and a trap for a permanent one: the same folders
re-occupied the head of the sorted list every week, so the limit was spent re-asking
questions that had already been answered. `Bestial Mockery — Chainsaw Destruction` and
`Monteverdi — Vespro della Beata Vergine` appear in consecutive runs' failure lists.

This is the **expiring guard** shape (hunting-silent-failure #1) inverted: not a hold that
releases itself, but a retry with no memory, whose cost grows until throughput reaches
zero. Nothing alerts, because from the outside every run processes exactly `--limit`
folders and exits 0.

**Decision:** a folder that no source can satisfy records `.album_art_none`, a JSON
sidecar holding an attempt count, and is skipped until its cooldown expires — **7 days
after the first miss, then 30, then 90**. Escalating, because a new release often gains
art within weeks and a 1994 noise demo will not. The sidecar is written atomically
(temp → `fsync` → `os.replace`); a truncated one parses as `{"v": 1}`, never as `{}`,
because `{}` would read as "never attempted" and silently reset the cooldown.

`miss_is_cooling` fails **open**: an unparseable timestamp means retry now. A guard that
failed closed here would retire an album from the queue permanently, which is the exact
failure this sidecar exists to prevent.

## Fault 2 — most of the "unfindable" albums were findable

sacad's `-t` (size tolerance) defaults to **25%**. The job ran at `--size 1000`, so every
cover below **750px was discarded** and the folder was reported as
`sacad_r: Unable to find cover for …` — the same message, and the same exit code, as an
album that genuinely exists on no source.

Measured on 20 randomly-sampled albums that had failed every previous weekly run:
re-running them at `500` with `-t 90` found art for **16 of them (80%)**.

Two other explanations were tested first and both were rejected on evidence:

- **Cover Art Archive via the MusicBrainz IDs already in the tags.** 80/80 sampled gap
  albums carry a `MusicBrainz Album Id`, so the lookup is free — and CAA returned **0 hits
  on 30 albums** across both the `release` and `release-group` endpoints. This library's
  underground half is simply not in CAA. Do not add it.
- **Navidrome showing embedded art over the file.** Its `CoverArtPriority` default is
  `cover.*, folder.*, front.*, embedded, external`, so `folder.jpg` already wins, and
  **zero** album folders hold a rival `cover.*`. Navidrome was reading the art correctly;
  the art was not there.

**Decision:** any folder the primary pass leaves empty gets a second, relaxed pass at
`--fallback-size 500 --fallback-tolerance 90` before a miss is recorded. A smaller cover
beats no cover.

## Fault 3 — good art was locked behind the done-marker

Of 17,627 covers, **1,179 are under 500px and 2,067 under 600px**; 804 of the sub-500px
ones were already marked done and would never have been revisited.

**Decision:** `--upgrade-below PX` re-asks for art in marked folders whose cover is
narrower than PX. Two conditions, and the second is what stops it churning: the marker now
records **both what was asked for and what was achieved**, so a 400px cover that is 400px
because no source has better records `target: 1000` and is skipped from then on. A legacy
text marker records no target, so it is eligible for exactly one re-sweep.

**A cover is never replaced by a smaller one.** The relaxed fallback can land a 300px
image over a 900px one, so the existing file is copied aside before the pass and put back
if the result is a downgrade. An upgrade sweep that makes art worse is worse than no
sweep.

## Alerting

The job could not report a bad week. `cron_job.py` treats `0,1` as fine, so exit 1 cannot
alert. A gap-fill batch of **20 or more folders in which not one gained a cover** now
exits **2**: the measured hit rate for gap folders is ~80%, so 0% is a network or source
outage, not an obscure library.

## Consequences

- `--limit` is 800, not 300 (measured 4.3 s/folder → ~60 min).
- Album folders now carry up to two hidden sidecars, `.album_art_done` and
  `.album_art_none`, both JSON. `config_backup.py` does not touch the media tree.
- The width measured from an image is cached into the marker, or an `--upgrade-below` run
  re-opens ~15k images every week to re-derive an answer that cannot change. `--dry-run`
  writes nothing, including that cache.
- The one-time catch-up (`--limit 0`) covered 4,550 folders: 952 gap, 2,191 unspent
  overwrite, 1,407 upgrade.
