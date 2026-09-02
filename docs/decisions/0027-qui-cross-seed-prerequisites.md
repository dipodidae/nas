# ADR-0027 — qui's cross-seed needs a filesystem grant, and that is the risky part

**Date:** 2026-09-02
**Status:** accepted — the mount and its assertions land; **cross-seed itself
stays disabled** pending a live end-to-end proof
**Depends on:** ADR-0002 (one mount, hardlinks), ADR-0007 (who writes qBittorrent
settings), ADR-0014 (qui's role), ADR-0017 (a deleter that cannot see slskd)
**Measurement:** `docs/qui-cross-seed-and-orphans.md`

## The fact this turns on

**qui creates the hardlinks itself.** It does not ask qBittorrent to. So
enabling cross-seed is not a settings toggle — it requires giving qui a view of
the data it previously did not have (its only volume was `/config`).

That widening is the risk in this change, not the cross-seeding.

## Decision

1. Mount `${SHARE_DIRECTORY}/downloads:/downloads` into qui — the **same host
   path at the same container path** as qBittorrent's, and **nothing wider**.
2. Assert the pairing at config time and the shared filesystem at runtime.
3. Leave cross-seed **off**, and record what arming it requires.
4. Leave the orphan scanner **off**, with a measured reason.
5. State the settings-writer precedence explicitly.

## Why `downloads` and not `${SHARE_DIRECTORY}:/data`

The \*arrs mount the whole share because they move media across it. qui does
not: cross-seed only ever touches what qBittorrent manages. A torrent UI has no
business reading `/music` or `/movies`, so it does not.

## The two things that must hold

- **Path identity.** qui writes link paths that qBittorrent is then told to seed
  from. A mismatch produces links at paths qBittorrent cannot find, and the
  symptom is "cross-seed never matches" — which reads as an indexer problem and
  would be debugged in the wrong place for a long time.
- **One filesystem.** Hardlinks cannot cross a mount point; a target on another
  filesystem silently becomes a **copy**. ADR-0002 exists because that cost
  0.96 TiB. `make check` asserts the host paths match; `make verify-runtime`
  compares the actual device numbers, which is the only real proof, and it runs
  daily because a mount can be changed after the config was reviewed.

Proven before the commit landed: both containers report device `2049`, and a
20 MiB file hardlinked **by qui** showed `%h = 2` from both containers for a
**20 MiB** disk delta — a copy would have been 40.

## ext4 has no reflink

`use_reflinks` must stay `0`. `/dev/sda1`'s feature list has no reflink support,
so hardlink mode is the only option available. A constraint of the filesystem,
not a shortcoming of the plan — nothing is being given up that this disk could
have provided.

## Why cross-seed is still off

The gate for enabling it is a live end-to-end proof: one genuinely cross-seeded
torrent showing `%h > 1` with a disk delta of ~0. That depends on a real match
existing on one of the 15 already-synced indexers, which cannot be manufactured
on demand. So the precondition ships proven and the feature ships off; arming it
is a separate, smaller decision with the risky part already reviewed.

`fallback_to_regular_mode` must be `0` when it is armed. "Regular" mode reuses
the **original** files in place, letting qBittorrent act on data another torrent
owns. A failed hardlink should be a visible failure, not a silent downgrade to
touching originals.

## Precedence: who writes qBittorrent's settings

`qbittorrent_settings_enforce.py` re-asserts `DisableOSCache` and the upload cap
against the **live session** every 5 minutes, because the WebUI can revert them
with no trace in this repo (ADR-0007).

qui's **automations** can also write instance settings. Two writers on a
5-minute cycle would fight, and the loser would be whichever ran second.

**Rule:** `qbittorrent_settings_enforce.py` is the sole writer of qBittorrent
session preferences. qui automations stay **off** (`automations` has 0 rows). An
automation that only moves or tags torrents is a different question and needs its
own record; one that writes preferences must not exist.

## The orphan scanner stays off, and here is the number

Computed independently rather than by configuring qui's scanner — which avoided
arming a second deletion engine and produced the argument against ever doing so.

Corrected orphan set under `/downloads`: **1,551 files, 17.68 GB**, of which

| area               | files | GB    |
| ------------------ | ----- | ----- |
| `complete/slskd`   | 1,432 | 16.55 |
| `incomplete/slskd` | 61    | 0.15  |
| `complete/manual`  | 58    | 0.98  |

**94 % by size is slskd data, and slskd is not a qBittorrent torrent.** qui sees
only the qBittorrent instance, so every file slskd ever downloaded looks
orphaned. With `auto_cleanup_enabled` it would propose deleting 1,432 music
files that are working exactly as intended.

That is ADR-0017's hazard in a new place — _Lidarr's only client is slskd, which
Cleanuparr cannot see_ — same shape, different tool. If it is ever armed,
`ignore_paths` must exclude `complete/slskd` and `incomplete/slskd` **first**,
and `auto_cleanup_enabled` stays `false` until a human has read a preview.

## The methodology warning worth keeping

The first orphan report was **wrong in the destructive direction**: 1,593 files /
205.84 GB, whose ten largest were live in-progress Bond downloads. The cause was
joining `save_path` (the _final_ destination) instead of `download_path` (where
incomplete data actually sits). Nine of 59 torrents were mid-download.

Any future orphan logic must claim `save_path`, `download_path`, `content_path`
**and** the `.!qB` partial suffix, and protect the whole subtree of any torrent
below 100 %. Over-claiming references is the only safe direction, and a
dry-run's first answer is not to be trusted just because it is large.
