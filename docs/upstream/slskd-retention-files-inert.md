# Draft upstream issue — slskd `retention.files.*` appears inert on 0.26.0.0

**Not filed.** Needs a human to submit to `slskd/slskd`. Everything below is observed on
this host; nothing is inferred except where marked.

---

**Title:** `retention.files.complete` / `retention.files.incomplete` do not remove files (0.26.0.0)

**Version:** `0.26.0.0 (0.26.0.0+e42a525d)`, Docker `slskd/slskd:latest`,
digest `sha256:ecd4026d4f8fb504e2cc55323efa2c1f5b56d20d3686b018249cc36b48ea17a6`,
.NET 10.0.10 on Linux, `storage_mode: disk`.

## What I expected

With `retention.files.complete: 20160` (14 days) and `retention.files.incomplete: 43200`
(30 days), files older than those thresholds are removed from the downloads and
incomplete directories.

## What happens

Nothing is removed, and nothing is logged.

The configuration is loaded — this is from `GET /api/v0/options`, i.e. the **running**
config, not the YAML:

```json
"retention": {
  "search": 1440,
  "transfers": {
    "download": { "succeeded": 1440, "errored": 10080, "cancelled": 60, "failed": 10080 }
  },
  "files": { "complete": 20160, "incomplete": 43200 },
  "logs": 180
}
```

Against that, on disk:

| Setting            | Threshold | Observed                                                |
| ------------------ | --------- | ------------------------------------------------------- |
| `files.complete`   | 14.0 days | **75 directories older**, oldest **23.8 days**, 1.63 GB |
| `files.incomplete` | 30.0 days | **1299 directories**, oldest **77.4 days**              |

And the container log contains **zero** occurrences of the word "retention" across a full
startup and 19 hours of operation:

```
$ docker logs slskd 2>&1 | grep -ci '\bretention\b'
0
```

## What does work

`retention.transfers.*` is clearly functioning. The transfer DB holds 24 records, all
`Queued, Remotely`, and zero `Completed,*` rows — and a host script that used to clear
stale completed records went from removing 21/day to 1/day over the two days after
retention was configured. So the transfer half of the feature works; only the file half
appears not to.

## Hypothesis (unverified)

`retention.transfers.download.succeeded` is 1440 (24 h) and `retention.files.complete` is
20160 (14 days). If file retention is driven off the transfer record, the record is
destroyed thirteen days before the file clock expires, and the files become permanently
unreferenced. I have not verified this and it may be entirely wrong — I am reporting the
observation, not the mechanism.

If that is the cause, it might be worth either warning when `files.*` exceeds the
corresponding `transfers.*` retention, or documenting that ordering constraint.

## Reproduction

1. Set `retention.transfers.download.succeeded: 1440` and `retention.files.complete: 20160`.
2. Download normally for a few weeks.
3. Confirm via `GET /api/v0/options` that both are loaded.
4. `find <downloads dir> -maxdepth 1 -type d -mtime +14` — expect empty, observe non-empty.

## Why it matters downstream

We disabled nothing on the strength of this, but we nearly did: the natural conclusion
from "retention is configured" was that our own cleanup script was redundant. It is not —
it is the only thing reclaiming disk. A silent no-op here reads as "the native feature has
this covered".
