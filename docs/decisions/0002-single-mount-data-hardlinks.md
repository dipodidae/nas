# ADR-0002 — `${SHARE_DIRECTORY}:/data`: hardlinks cannot cross a mount point

**Date:** 2026-09-01
**Status:** accepted
**Applies to:** sonarr, radarr, lidarr (staged only — ADR-0003)
**Background:** `docs/arr-qbittorrent-pollution.md` §7 "Import paths and
hardlinks — the biggest disk finding", and "The hardlink defect (0.96 TiB)"

## Problem

Hardlinks cannot cross a mount point, even when both sides live on one
filesystem. With each library and the download tree exposed as **separate bind
mounts** (`/tv` + `/downloads`, `/movies` + `/downloads`, `/music` +
`/downloads`), every import silently fell back to a full copy despite
`copyUsingHardlinks=True`. Cost: **0.96 TiB of duplication.**

Silently is the operative word — nothing reported an error. The *arrs did what
they were told; `link()` failed and the copy path took over.

## Evidence

Probed directly inside the containers:

```
ln /downloads/...        /tv/...            -> EXDEV
ln /data/downloads/...   /data/series/...   -> ok
```

## Decision

Add `${SHARE_DIRECTORY}:/data` as a single-mount view of the whole share.
Under `/data` both trees share one mount, so `link()` works.

**The per-library legacy mounts are kept deliberately, alongside the new one.**
They make the change reversible, and removing them is a separate step once the
new paths have bedded in. This is why sonarr and radarr each have three media
mounts that look redundant. They are not.

## Consequences

- This prevents *future* duplication. It does not reclaim the existing
  0.96 TiB — those library copies are already separate inodes, and they collapse
  only as old torrents age out and new imports hardlink in their place.
- Integrity after the repath: Sonarr 59/59 series, 1080 episode files,
  2.51 TiB, all unchanged. Radarr 37/37 movies, 0.25 TiB, unchanged.
- qBittorrent was **not** touched and does not need the unified mount:
  hardlinking happens inside the *arr containers.
- Lidarr is the exception — see ADR-0003.
- Bazarr does not need it at all — see ADR-0015.

## Invariant

`sonarr`, `radarr` and `lidarr` must all mount `${SHARE_DIRECTORY}:/data`.
Checked by `scripts/check-invariants.sh`.
