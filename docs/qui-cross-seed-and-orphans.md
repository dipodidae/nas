# qui: cross-seed prerequisites, and what an orphan scan would actually find

Measured 2026-09-02 against qui **v1.28.0**. Two separate pieces of work, both
deliberately stopping short of arming anything.

## Part 1 — cross-seed: the precondition, proven

### qui does the linking itself

This is the fact the whole design turns on, and it is easy to get backwards.
Per qui's own documentation, **qui creates the hardlinks** — it does not ask
qBittorrent to. So qui needs its own view of the data, and before this work it
had none: its only volume was `${CONFIG_DIRECTORY}/qui:/config`.

Confirmed from the running instance's own database (`instances` table):

```
has_local_filesystem_access = 0
use_hardlinks               = 0
hardlink_base_dir           = ''
use_reflinks                = 0
```

So "turn on what is already paid for" is not a settings toggle. It requires
**widening a service's filesystem view**, which is the actual risk in this
change — bigger than the cross-seeding itself.

### The mount, and why it is `downloads` and not the share

`${SHARE_DIRECTORY}/downloads:/downloads` — the **same host path at the same
container path** as qBittorrent's. Two reasons, both mechanical:

1. qui writes link paths that qBittorrent is then told to seed from. Disagree on
   paths and qui creates links qBittorrent cannot find, which surfaces as a
   cross-seed that never matches — i.e. it looks like an indexer problem.
2. Hardlinks cannot cross a mount point. A link target on another filesystem
   becomes a silent **copy**; this stack has already paid 0.96 TiB for that
   lesson (ADR-0002).

It is **not** given `${SHARE_DIRECTORY}:/data` like the \*arrs. Cross-seed only
ever touches what qBittorrent manages, and a torrent UI has no business reading
`/music` or `/movies`. `make check` asserts both the alignment and the absence
of anything wider.

### Proven, not assumed

```
qui         /downloads device: 2049
qbittorrent /downloads device: 2049
```

A 20 MiB file created by **qui**, hardlinked by **qui**, inspected from both
containers:

```
2 links  20971520 bytes  /downloads/.crossseed-probe/src.bin
2 links  20971520 bytes  /downloads/cross-seed/.probe/linked.bin
qbittorrent sees it too:  2 links  /downloads/cross-seed/.probe/linked.bin

disk delta for the file + its link: 20 MiB   (a copy would be ~40)
```

`%h = 2` and a 20 MiB delta for 40 MiB of apparent data is the whole
precondition. Probe removed afterwards. `make verify-runtime` now compares the
two device numbers on every run, because a mount can be changed later and the
config-time check cannot see that.

### Reflink mode is not available here, and that is a constraint not a limitation

`/dev/sda1` is **ext4**, which has no reflink support at all — confirmed from
`tune2fs -l`'s feature list. So the choice is hardlink mode or nothing;
`use_reflinks` must stay `0`. Nothing is lost that this filesystem could have
offered.

### Left DISABLED, deliberately

The mount, the assertions and this document land now. **Cross-seed itself stays
off** (`cross_seed_settings` has no row), because the gate for enabling it is a
live end-to-end proof — one genuinely cross-seeded torrent showing `%h > 1` with
a disk delta of ~0 — and that depends on an actual match existing on one of the
15 indexers, which cannot be manufactured on demand.

What is ready when someone wants to arm it:

| setting                       | value                   | why                           |
| ----------------------------- | ----------------------- | ----------------------------- |
| `has_local_filesystem_access` | `1`                     | the mount now exists          |
| `use_hardlinks`               | `1`                     | the only mode ext4 supports   |
| `hardlink_base_dir`           | `/downloads/cross-seed` | same mount as the source data |
| `use_reflinks`                | `0`                     | ext4 has no reflink           |
| `fallback_to_regular_mode`    | `0`                     | see below                     |

`fallback_to_regular_mode = 0` on purpose: "regular" mode reuses the _original_
files in place, which lets qBittorrent act on data another torrent owns. A
failed hardlink should be a visible failure, not a silent downgrade to touching
originals.

### Precedence against `qbittorrent_settings_enforce.py`

qui's **automations** can write instance settings. `qbittorrent_settings_enforce.py`
re-asserts `DisableOSCache` and the upload cap against the _live session_ every
5 minutes (ADR-0007), because the WebUI can silently revert them.

**Two writers on a 5-minute cycle would fight.** So the rule is explicit:
`qbittorrent_settings_enforce.py` is the **sole** writer of qBittorrent session
preferences, and qui's automations stay **off**. `automations` currently has 0
rows. Do not enable one that writes preferences; a rule that only moves or tags
torrents is a different matter and needs its own decision.

## Part 2 — the orphan scan, read-only

Not run through qui's scanner: computing it independently avoids configuring a
second deletion engine at all, and it produced a finding that argues against
ever arming qui's.

### The first answer was wrong, in the dangerous direction

A naive walk — every file under `/downloads` not matching `save_path + filename`
— reported **1,593 orphans, 205.84 GB**, whose ten largest were Bond REMUXes:
Goldfinger, Dr. No, A View to a Kill, The Man with the Golden Gun.

**All of those are live, in-progress downloads.** qBittorrent's `save_path` is
the _final_ destination; while a torrent is incomplete its data sits under
`download_path`. Nine of the 59 torrents were mid-download at the time. Acting
on that report would have deleted active work.

Corrected by claiming every plausible location per torrent — `save_path`,
`download_path`, `content_path`, and the `.!qB` partial suffix — and protecting
the whole subtree of any torrent below 100 % progress:

|                   | files | GB        |
| ----------------- | ----- | --------- |
| **naive (wrong)** | 1,593 | 205.84    |
| **corrected**     | 1,551 | **17.68** |

Over-claiming references is the only safe direction for a report like this.

### What the corrected orphans actually are

| area               | files | GB    |
| ------------------ | ----- | ----- |
| `complete/slskd`   | 1,432 | 16.55 |
| `incomplete/slskd` | 61    | 0.15  |
| `complete/manual`  | 58    | 0.98  |

Hardlinked orphans (where deleting frees nothing): **0**.

### The finding: qui's orphan scanner must never run unbounded here

**94 % of the orphans by size are `slskd` data, and slskd is not a qBittorrent
torrent.** qui knows only about the qBittorrent instance, so every file slskd
has ever downloaded looks orphaned to it. Pointed at `/downloads` with
`auto_cleanup_enabled`, it would propose deleting 1,432 music files that are
working exactly as intended.

This is ADR-0017's hazard in a new place: _Lidarr's only client is slskd, which
Cleanuparr cannot see._ Same shape, different tool. It is the reason
`failedImport.skipIfNotFoundInClient` must stay `true` there, and the reason
qui's orphan scanner stays off here.

If it is ever enabled, `orphan_scan_settings.ignore_paths` **must** exclude
`complete/slskd` and `incomplete/slskd` first, and `auto_cleanup_enabled` must
stay `false` until a preview has been read by a human.

### The one genuinely reclaimable set

`complete/manual` — 58 files, 0.98 GB — is residue from before Auto-TMM was
enabled, when torrents landed outside category-driven save paths. Small, and not
urgent. Not deleted here: this document is a report, not an action.
