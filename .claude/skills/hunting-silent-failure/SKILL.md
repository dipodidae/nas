---
name: hunting-silent-failure
description: Use when auditing, reviewing or debugging anything in this NAS stack that holds state between runs - cursors, watermarks, locks, cooldowns, retry ceilings, backoffs, state files. Names the six failure shapes this repo has actually produced so they can be recognised instead of rediscovered.
---

# Hunting silent failure

Six distinct shapes, all found in this repo, all of which reported success. When you
audit something, walk this list against it rather than reading the code hoping to notice.

## 1. The expiring guard

**A safety mechanism that expires into the exact bug it guards against.**

The Jellyfin bridge held its cursor on an unmappable folder so the album would be
retried. Holding made the cursor age. Past 2000 records, the held records fell outside
the fetch window, so `unmapped` computed as empty, the run "succeeded", the cursor jumped
forward and **the alert stopped**. The longer the guard held, the closer it came to
releasing itself.

Its tell is that there is no tell — the lost records were never fetched, so nothing in
any log mentions them.

> **Ask of every hold, retry ceiling, backoff and cooldown: can the thing it is holding
> for fall outside the window that would detect it?** If yes, the guard has an expiry
> date and the expiry is silent.

## 2. Fail-open state

`except (OSError, JSONDecodeError): return {}` — and `{}` means "nothing has happened
yet", so the job re-does the world and then **saves that over the real state**.

The bridge's absent / empty / truncated / future-schema cases were byte-identical to a
healthy run: `nothing to report`, exit 0. Check what the empty value _means_ to the
caller, not whether the exception is handled.

Related: **a non-atomic write manufactures this case.** `path.write_text(json.dumps(x))`
truncates on a crash. Use temp file → `fsync` → `os.replace`. Only some scripts here do.

## 3. A `<=` comparison against a non-unique key

Lidarr's history held **125 distinct dates across 600 records**, one second shared by 22.
A `date <= cursor` test skips everything sharing the cursor's second, including records
written after the cursor was taken.

Prefer a monotonic unique key (`id`). Before trusting one, prove it: count distinct
values, count inversions.

## 4. A default that became load-bearing

`transfers.download.retry` is absent from `slskd.yml` and present in the running config —
slskd's own defaults. Every measurement of the stuck reaper assumes them, and defaults
change on upgrade, where the change reads as "the reaper got worse".

**Read the effective config, not the file.** If a number matters, pin it
(`scripts/check-slskd-effective-config.py`).

## 5. Configured and inert

slskd's `retention.files.*` is set correctly and does nothing: 75 dirs past the 14-day
threshold, 1299 past the 30-day one, and zero occurrences of "retention" in the container
log. `retention.transfers.*` works fine — half a subsystem working is the hard case.

Never conclude a feature works because it is configured. Find the effect or the log line.
If neither exists, it is inert, and anything you were about to retire in its favour stays.

## 6. An invisible skip

`flock -n` exits 1 **without running anything**, so the wrapper never started and the
skip left no log line, no state update, no output. A skipped run was byte-identical to a
clean one. Five jobs share one lock here and one holds it for 600s.

Ask: when this is _not_ done, what is written down? If the answer is nothing, that is the
bug.

## Grep starters

```bash
# state files, and who writes them atomically (only some do)
grep -ln 'json.dump\|write_text' scripts/*.py | xargs grep -Ln 'os.replace'

# fail-open state loads
grep -n -A4 'def load_state' scripts/*.py | grep -B3 'return {}'

# comparisons against a persisted key
grep -nE '(date|since|cursor|last_seen)\s*(<=|>=|<|>)' scripts/*.py

# jobs that cannot alert on exit 1
grep 'cron_job.py' cron/crontab | grep -v '^#' | grep -v -- '--ok-codes'
```

## When you find one

Fix the _class_, not the instance. Both times this repo's docs pointed at the wrong
script, the docstrings were right — so the tables are now generated and gated
(`scripts/gen_pipeline_tables.py`, asserted by `make check`) rather than corrected.

Add a failure row to `docs/music-pipeline-integration.md` §7 with its **tell**, and grow
§10 rather than shrinking it.
