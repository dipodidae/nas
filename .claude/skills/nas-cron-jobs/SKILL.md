---
name: nas-cron-jobs
description: Use when adding, editing, scheduling or debugging any cron job in this NAS repo. The crontab has four non-obvious requirements and getting any of them wrong produces a job that fails silently or never runs at all - each has already cost real downtime here.
---

# Cron jobs in this repo

29 scheduled jobs, all wrapped in `scripts/cron_job.py`. `cron/crontab` is the tracked
source of truth; `crontab -l` is the live copy. Keep them in sync.

## The four rules, each learned the hard way

### 1. Every line must `cd /home/tom/nas` first

Cron's `$HOME` is `/home/tom` and there is no `.venv` there. A missing `cd` means the job
never runs and produces no output. **This cost three months** on `media_ops_status.py`,
and this session reproduced it on the first new cron line it wrote. Grep before installing:

```bash
crontab -l | grep 'cron_job.py' | grep -v '^#' | grep -vc 'cd /home/tom/nas'   # must be 0
```

### 2. Declare `--ok-codes` unless you mean the default

`--ok-codes` defaults to `0,1`, so **exit 1 cannot alert**. That default exists because
`stack_watchdog.py` and `media_ops_status.py` legitimately exit 1 for "a condition is
active" — it is not arbitrary, but it is inherited by 26 of 29 jobs.

If your script's exit 1 means "something is wrong", either make it exit **2** or pass
`--ok-codes 0`. A day of Lidarr imports was lost to a warning that returned 0.

### 3. Use `--lock`, never an external `flock`

```
# WRONG - flock exits 1 without running anything, so the skip leaves NO trace
07 * * * * /usr/bin/flock -n /tmp/x.lock /usr/bin/env bash -c "cd ... && cron_job.py ..."

# RIGHT - the wrapper is outside the lock and records the skip
07 * * * * /usr/bin/env bash -c ". .venv/bin/activate && \
    python scripts/cron_job.py --name x --lock /tmp/x.lock --max-age-min 180 -- python scripts/x.py"
```

`--lock-wait N` is `flock -w N`; omitting it is `flock -n`. Three consecutive skips push
`nas-attention`, because a starved job produces no output and looks like a quiet period.

**`/tmp/nas-tubifarry-cleanup.lock` has five holders** — `:07`, `:22`, `:37`, `:52` and
`05:30` — and `05:30` takes it with `--lock-wait 600`, so it can hold straight through the
`:37` window.

### 4. Test in a real cron environment, not your shell

```bash
LINE=$(crontab -l | grep 'your-job' | sed 's|^[0-9*/, ]* ||')
cd /home/tom && env -i HOME=/home/tom PATH=/usr/bin:/bin:/usr/local/bin bash -c "$LINE"
```

`env -i` from `$HOME` is what catches rules 1 and the venv.

## Adding a job — the full sequence

```bash
crontab -l > /tmp/ct.bak                      # 1. always back up first
# 2. edit, add the line
crontab /tmp/ct.new
crontab -l > cron/crontab                     # 3. update the tracked copy
python scripts/gen_pipeline_tables.py --write # 4. regenerate the doc tables
make check                                    # 5. gate: fails if the tables are stale
```

Step 4 is not optional — `make check` asserts the generated cron-fleet tables in
`docs/music-pipeline-integration.md` match `cron/crontab` and the scripts' docstrings.
The hand-maintained versions pointed at the wrong script twice.

The table's "what it does" column is **the first line of your script's module
docstring**. Write that line as the thing you want in the table.

## Script contract

`0` success / `1` partial / `2` fatal. Side effects in `main()`, pure logic elsewhere so
it is testable. Anything destructive gets `--dry-run`, and dry-run is the default.

Add tests under `scripts/tests/` — they run in CI across Python 3.11/3.12/3.13.

## Debugging one that is misbehaving

```bash
grep -E "^--- .* exit=" logs/<job>.log | tail -5    # run headers
cat logs/cron-state/<job>.json                       # last_run/last_success/streaks
grep 'SKIPPED' logs/<job>.log | tail                 # lock starvation
```

`stack_watchdog.py` alerts when a job stops running at all, via `--max-age-min` —
so set that to something meaningfully larger than the interval, not equal to it.
