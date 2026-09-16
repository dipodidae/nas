# ADR-0048 — A 0-byte `-wal` is the receipt for a checkpoint, not evidence of a dirty one

**Date:** 2026-09-16
**Status:** accepted
**Amends:** ADR-0041 — whose conclusion ("assert on the effect: exit 0 and no `-wal`") is
right in spirit and too literal in code
**Found by:** ADR-0047, which made the backup fast enough to actually reach this check

## What happened

ADR-0047 cut the pre-upgrade copy from ~17 minutes to about a minute. The very next run of
`pnpm stack:update` got past the copy for the first time and **halted**:

```
==> jellyfin: 12.1ubu2604-ls49 -> 12.1ubu2604-ls50
    backup: FAILED -- jellyfin stopped with exit 0 but left 2 -wal/-shm file(s):
            introskipper.db-wal, introskipper.db-shm.
    HALTING: a tag is not a rollback for this service.
```

`jellyfin` had stopped **cleanly** — exit 0, and `jellyfin.db` itself left no `-wal` at
all, which is ADR-0041's fix working exactly as designed. The two files named belong to
the **introskipper plugin**, and on disk they were:

```
-rw-r--r-- 1 tom tom        0 Sep  9 22:56 introskipper.db-wal
-rw-r--r-- 1 tom tom    32768 Sep 16 01:01 introskipper.db-shm
-rw-r--r-- 1 tom tom  1069056 Sep  9 21:41 introskipper.db
```

**Zero bytes.** `wal_is_clean()` asserted on the _existence_ of a `-wal`/`-shm` name, so a
file with nothing in it blocked a one-way upgrade.

## Why zero bytes means the opposite of what the check assumed

A `-wal` holds committed frames not yet folded into the `.db`; that is the whole reason a
`.db`-only copy can be stale. Frames live **in the `-wal` file**, so a 0-length one holds
none — it is below even the 32-byte WAL header. A `-shm` is a shared-memory _index into_
the `-wal`; it carries no committed data of its own and is rebuilt on demand.

SQLite normally unlinks both on a clean close — verified here, a plain `close()` leaves
neither file. A connection that persists its WAL leaves them in place at 0 bytes instead,
which is what introskipper does. Measured against a real database, 2026-09-16:

|                                         | `-wal` | `-shm` |
| --------------------------------------- | -----: | -----: |
| open, frames not yet folded in          | 61,832 |   live |
| after `PRAGMA wal_checkpoint(TRUNCATE)` |  **0** | 32,768 |
| after a plain clean `close()`           | absent | absent |

Byte for byte the introskipper state. And copying the `.db` **alone** out of that
truncated state reads back **5000 of 5000 rows** with `PRAGMA integrity_check` → `ok`.

So the 0-byte `-wal` is not a database mid-write. It is the receipt proving the checkpoint
already happened.

## Decision

`wal_is_clean()` asserts on **content, not existence**:

- a `-wal` with **size > 0** is dirty — the copy would be stale, halt;
- a `-wal` at **0 bytes** is checkpointed — proceed;
- a `-shm` is evidence of nothing either way and is no longer consulted.

The failure message now names the offending files **with their byte counts**, because
"left 2 -wal/-shm file(s)" was true and still sent the reader in the wrong direction.

ADR-0041's own measured incident — `jellyfin.db-wal` at 6,336,592 bytes beside a
2,785,280-byte `-shm` after a 10.5s exit-137 stop — still fails this check, and is pinned
as a test so it cannot stop failing.

## Why this is not a weakening

The check's job is to refuse a backup that would silently lose committed data. Data can
only be lost from a `-wal` that **has** frames. Asserting on the filename caught that
case, but it also caught a case with nothing at stake — and it did so on a **one-way**
upgrade, where the script correctly refuses to proceed without a proven backup. A check
that cannot be satisfied does not protect the upgrade; it prevents it, and the pressure it
creates is pressure to skip the backup.

## Consequences

- The wrong response to this failure would have been to `rm` the 0-byte `-wal`. It is a
  file SQLite manages; deleting it fixes the message and teaches nothing.
- Anything else that persists its WAL now passes without a special case, because the rule
  is about bytes rather than about introskipper.
