# ADR-0015 — Bazarr has no `/data` mount, and that is currently a defect

**Date:** 2026-09-02
**Status:** **open defect — documented, deliberately not fixed in the refactor**
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

## Why it was not fixed in the compose refactor

The refactor's contract was a provable no-op — `docker compose config` output
byte-identical before and after. Adding a mount is a semantic change that
recreates the container and changes what Bazarr can see on disk. That deserves
its own commit, its own verification, and a deliberate decision by the owner,
not a rider on a layout change.

The compose file therefore carries a comment stating the defect and pointing
here, rather than a comment claiming the omission is intentional.

## Remediation — pick one

**Option A (recommended, mirrors the other \*arrs).** Add to bazarr's
`volumes:` in `compose/media-manage.yaml`:

```yaml
      - ${SHARE_DIRECTORY}:/data
```

then `docker compose up -d bazarr`. Keeps `/movies` and `/tv` for
reversibility, exactly as ADR-0002 did for sonarr/radarr. No Bazarr config
change needed, because the paths it already stores start resolving.

**Option B.** Leave the mounts alone and configure Bazarr path mappings in its
UI (Settings → Sonarr/Radarr → Path Mappings): `/data/series` → `/tv` and
`/data/movies` → `/movies`. Avoids a new mount, but adds config that lives only
in Bazarr's SQLite and has to be remembered if the layout ever changes again.

Option A is preferred: one line, in version control, consistent with the three
services that already did this.

## Verification after fixing

```sh
docker exec bazarr python3 -c "
import sqlite3,os
c=sqlite3.connect('file:/config/db/bazarr.db?mode=ro',uri=True)
rows=[r[0] for r in c.execute('select path from table_episodes')]
print(sum(1 for p in rows if p and not os.path.exists(p)), 'of', len(rows), 'unresolvable')"
```

Expect `0 of 1087`. Then trigger a subtitle search on one episode and confirm
the `.srt` lands next to the video.
