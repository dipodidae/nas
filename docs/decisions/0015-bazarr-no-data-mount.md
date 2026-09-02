# ADR-0015 — Bazarr has no `/data` mount, and that is currently a defect

**Date:** 2026-09-02
**Status:** **fixed 2026-09-02** (Option A applied and verified)
**Related:** ADR-0002 (the repath that caused it)

## The question that was asked

Bazarr mounts `/movies` and `/tv` but, unlike sonarr/radarr/lidarr, has no
`${SHARE_DIRECTORY}:/data`. Does it need one? It writes subtitles next to the
media — does it hardlink or move anything?

## The ADR-0002 reasoning does not apply

Correct: Bazarr never imports, moves, or hardlinks. It writes subtitle sidecars
next to a media file it already has a path to, and runs `subcleaner` over them
as post-processing. There is no cross-mount `link()` for `EXDEV` to break.

So the *hardlink* rationale for `/data` is genuinely absent here.

## But it needs `/data` for a different reason, and is broken without it

Bazarr does not discover media itself. It asks Sonarr and Radarr for paths and
**consumes those paths verbatim.** Since the ADR-0002 repath, Sonarr and Radarr
report `/data/series/...` and `/data/movies/...`. Bazarr mounts neither, and its
`path_mappings` / `path_mappings_movie` are both `[]`.

Measured 2026-09-02:

```
$ docker exec bazarr ls -d "/data/series/The Wire"
ls: cannot access '/data/series/The Wire': No such file or directory
$ docker exec bazarr ls -d "/tv/The Wire"
/tv/The Wire

table_episodes: 1087 stored, 1087 unresolvable inside the container
table_movies:     24 stored,   24 unresolvable inside the container
```

Every stored path begins `/data/`. Bazarr's own log carries matching
`does not exist` entries against `/data/movies`.

Sonarr's API confirms the source of the paths:

```
$ curl -H "X-Api-Key: …" localhost:8989/api/v3/series
  /data/series/Agatha Christie's Poirot
  /data/series/The Wire
  /data/series/The Returned
```

This is a **side effect of ADR-0002 that was not noticed at the time**: the
repath fixed hardlinking for Sonarr and Radarr and silently broke Bazarr's view
of the same files.

## The fix applied (Option A)

Added to bazarr's `volumes:` in `compose/media-manage.yaml`:

```yaml
      - ${SHARE_DIRECTORY}:/data
```

`/movies` and `/tv` are kept for reversibility, exactly as ADR-0002 did for
sonarr/radarr. No Bazarr config change was needed — the paths it already stores
simply start resolving.

This mirrors the three services that already do it, is one line in version
control, and needed no Bazarr-side state.

## Option B, considered and not taken

Leave the mounts alone and configure Bazarr path mappings in its UI
(Settings → Sonarr/Radarr → Path Mappings): `/data/series` → `/tv`,
`/data/movies` → `/movies`. Rejected: it adds config that lives only in
Bazarr's SQLite, is invisible to version control, and has to be remembered if
the layout ever changes again.

## Verification (run 2026-09-02, after `docker compose up -d bazarr`)

```
episodes: 1087 stored, 0 unresolvable
movies:     24 stored, 0 unresolvable

$ docker exec bazarr ls -d "/data/series/The Wire"
/data/series/The Wire
```

Container reached `healthy` 25 s after recreate. Before the fix the same two
counts were 1087/1087 and 24/24 unresolvable.

Re-check any time with:

```sh
docker exec bazarr python3 -c "
import sqlite3,os
c=sqlite3.connect('file:/config/db/bazarr.db?mode=ro',uri=True)
rows=[r[0] for r in c.execute('select path from table_episodes')]
print(sum(1 for p in rows if p and not os.path.exists(p)), 'of', len(rows), 'unresolvable')"
```

Remaining follow-up, not part of this fix: Bazarr holds **24** movies against
Radarr's **37**. That is a separate sync question, not a path question — all 24
it knows about now resolve.
