# ADR-0047 — The pre-upgrade backup is downtime, so it must not copy what regenerates

**Date:** 2026-09-16
**Status:** accepted
**Relates to:** ADR-0037 (the nightly archive and its exclude list), ADR-0041 (the stop
that precedes this copy), ADR-0035 (why a jellyfin rollback point has to exist at all)

## Context

`pnpm stack:update` was reported as **stuck**. It was not. It was copying.

`sqlite_backup()` in `scripts/stack_update.py` takes the rollback point for a one-way
upgrade, and its shape is: stop the service, assert the WAL checkpointed (ADR-0041), copy
the tree. The copy was an unfiltered `cp -a` of `${CONFIG_DIRECTORY}/<service>`.

Measured on this host, 2026-09-16, for the `jellyfin` `12.1ubu2604-ls49 -> ls50` bump:

| | files | bytes |
| --- | ---: | ---: |
| whole tree | 197,840 | 21.9 GB |
| of which `data/metadata` | 176,150 | 19 GB |

The destination is `/mnt/drive/backups/stack-update`, a different filesystem from the
root LV the config tree lives on, so every file is a real read-and-write. The aborted run
had moved **14 GB / 121,124 files in ~10 minutes** — about 200 files/s — putting the full
copy at ~17 minutes.

`jellyfin` is **stopped** for all of it. That is the actual defect: seventeen minutes of a
user-visible service being down in order to copy 19 GB of poster art that Jellyfin
re-fetches on demand. `lidarr` is the same shape and worse odds — it is on `:nightly`, so
it takes this path often, and its 13.3 GB is 8.1 GB of `MediaCover` plus 1.8 GB of its own
`Backups/` that this archive supersedes.

The knowledge needed to avoid this already existed **in this repo, ten lines long**, in
`config_backup.py`:

```python
"jellyfin/data/metadata/**",  # ~18 GB of re-fetchable artwork and NFO
"*/MediaCover/**",            # *arr poster caches: 7.7 GB in lidarr alone
```

`stack_update.py` duplicated none of it. Two backup tools, one fact about which subtrees
regenerate, and only one of them knew it.

## Why it read as a hang

`run()` captures output. `cp` is silent by design. So a 17-minute step printed nothing
between `==> jellyfin: ...ls49 -> ...ls50` and its result, with a `timeout=3600` that
would not have fired for another 43 minutes. There was no way to tell a slow copy from a
wedged one from the outside, which is the only reason `^C` looked like the right move.

## Decision

**1. The copy skips what regenerates, and the list is imported, not restated.**
`stack_update.py` imports `DEFAULT_EXCLUDES` from `config_backup.py`. "Which subtrees
regenerate" is one fact; it is worth more here than there, because `config_backup` runs
against a live stack and this runs with the service stopped.

**2. `rsync -aR` from the config root, not `cp -a` of the service dir.** The shared
patterns are written relative to the config root (`jellyfin/data/metadata/**`, not
`data/metadata/**`), so the transfer uses a `/./` pivot — `rsync -aR ${CONFIG}/./jellyfin
${DEST}/` — which makes the transfer-relative paths carry the `jellyfin/` prefix the
patterns expect. The destination shape is unchanged: `${DEST}/<service>`.

Two properties of rsync's filter engine are load-bearing and both fail quietly:

- the **first** matching rule wins, so every re-include must precede the excludes;
- an excluded directory is **never descended into**, so a re-include beneath one must
  name each parent directory too. `!a/b/c/**` expands to `a/`, `a/b/`, `a/b/c/`,
  `a/b/c/**`. Without that expansion the only `!` pattern in the shared list
  (`!nextcloud/www/nextcloud/config/**`) would be silently unreachable.

**3. The copy asserts a database landed.** Skipping subtrees is the entire point of the
change, so an over-broad pattern is now the live risk — and it would present as a fast,
clean, *empty* backup, discovered on the day someone needed to roll back. Every service in
`STORES` with `Store.SQLITE` keeps at least one `*.db` under its config dir; a copy that
lands none is a failure, not a backup. For a one-way service that halts the run, which is
the correct outcome.

**4. The step announces itself.** One line naming the destination and the fact that the
service is down until the copy ends. A silent multi-minute step under a captured stdout
is indistinguishable from a hang.

## Result

Every `Store.SQLITE` service, verified against the real config tree with the new rules:

| service | files | → | GB | → | store DBs kept |
| --- | ---: | --- | ---: | --- | --- |
| jellyfin | 197,839 | 677 | 21.88 | 1.75 | 4/4 |
| lidarr | 40,841 | 135 | 13.32 | 2.67 | 1/1 |
| sonarr | 792 | 97 | 0.22 | 0.13 | 1/1 |
| radarr | 595 | 75 | 0.19 | 0.07 | 1/1 |
| prowlarr | 711 | 703 | 0.16 | 0.14 | 1/1 |
| qbittorrent | 197 | 195 | 0.04 | 0.04 | 1/1 |
| bazarr | 23 | 10 | 0.02 | 0.01 | 1/1 |
| beszel | 5 | 3 | 0.01 | 0.00 | 2/2 |
| tinyauth | 2 | 2 | 0.00 | 0.00 | 1/1 |

The only databases dropped are each `*arr`'s `logs.db` (`**/logs.db*`, excluded on
purpose; 285 MB in lidarr alone), which is a log store, not the service's state.

`jellyfin`'s downtime for a backup goes from ~17 minutes to **25.1 s measured**, and the
protected bytes — `jellyfin.db`, `system.xml` (`EnableLegacyAuthorization`, ADR-0035),
`encoding.xml` (the one-invalid-value-discards-the-file trap), `network.xml`, `users/`,
`plugins/` — are all still in it.

The `ls49 -> ls50` bump that prompted all this then ran end to end in **5:17 total**:
backup ok (677 files, 1,747,854,338 bytes), tag bumped in `media-serve.yaml` +
`diun/manifest.yml`, `running: 12.1ubu2604-ls50` confirmed from the container rather than
the plan, and `make check` / `make lint` / `make verify-runtime` all green.

## Consequences

- A restore from one of these backups repopulates artwork by re-fetching it. That is the
  accepted cost, and it is the same trade the nightly archive already makes (ADR-0037).
- `rsync` is now a hard dependency of `stack_update.py`. It is present on this host and in
  the linuxserver base images, but it is a new one and worth knowing.
- Backups taken **before** 2026-09-16 are whole-tree copies and restore differently
  (artwork included). Nothing reads them automatically; this is a note for a human.
