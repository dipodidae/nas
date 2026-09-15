# ADR-0044 — Navidrome sits behind the door, except `/rest`

**Date:** 2026-09-15
**Status:** accepted
**Extends:** ADR-0034 — the one-door rule and its path-scoped exceptions

## Context

Jellyfin already serves the music tree, but its music-client story is poor. Navidrome
speaks **Subsonic**, which is what every serious mobile music client actually implements
(Symfonium, substreamer, DSub, play:Sub). It reads the same `${SHARE_DIRECTORY}/music` that
Lidarr writes.

## Decisions

### 1. `protect` on `location /`, and `/rest` deliberately left open

ADR-0034's rule says anything with a native mobile client cannot sit behind forward auth,
because a `302` to a login page is not something such a client can follow. Taken whole,
that would put Navidrome in the `never` column.

But the rule applies to the _path the client uses_, not the hostname. Navidrome's entire
Subsonic surface is `/rest/*`, and the browser UI is everything else. So it gets the same
path-scope the six \*arr confs already use for `/api`: the door on `location /`, and
`location /rest` proxied straight through.

That is a path-scope, **not a hole** — `/rest` keeps Subsonic's own salted-token
authentication, which Navidrome enforces. Proven anonymously through SWAG on 2026-09-15:

```
GET https://navidrome.4eva.me/                  -> 302 https://auth.4eva.me/login?...
POST (with body) https://navidrome.4eva.me/     -> 302 https://auth.4eva.me/login?...
GET https://navidrome.4eva.me/rest/ping.view    -> 200
    <subsonic-response status="failed" ...>
      <error code="10" message="missing parameter: 'u'"/>
```

The `200` is nginx passing the request through; the `failed` body is Navidrome refusing it.
Both halves matter — a `200` with _content_ would have meant an open door.

`/rest` also carries the audio stream for the **web** player, so gating it would have broken
playback in the browser too, not just on phones.

`scripts/check-door-live.sh` asserts this path stays un-redirected. It is the half that
breaks silently: if the `location /rest` block ever loses its place, `location /` catches
the path instead, every phone stops playing, and the browser UI plus every other door check
stay perfectly green.

### 2. `:ro` on the music mount is correct here, unlike ADR-0039

ADR-0039 is fresh enough to be worth naming explicitly. That `:ro` bit because Jellyfin
writes trickplay tiles **next to the media**. Navidrome keeps its database, cache and
downloaded artwork under `/data` and never writes the music tree, so read-only costs
nothing. Revisit only if playlist export is ever enabled.

### 3. It introduces no new path namespace

`nas-music-pipeline` warns about three namespaces for one directory (`/music`,
`/data/music`, and the host path). Navidrome adds a fourth container path, `/music` — but
nothing cross-references it: it has no \*arr integration, no bridge script translates its
paths, and no other service reads its database. The ADR-0003 namespaces are unaffected.

### 4. Consequences recorded

- **`user:`, not PUID/PGID.** Navidrome is not a linuxserver image; upstream states it
  ignores `PUID`/`PGID` outright. The image's own default user is root, but the compose
  `user:` overrides it, so nothing ever runs as root inside to repair a root-owned `/data` —
  the ADR-0014 `qui` trap exactly. It is therefore **in** `make bootstrap`'s chown loop,
  where `adguardhome` is not (ADR-0043).
- **`ND_SCANNER_SCHEDULE`, not `ND_SCANSCHEDULE`.** The latter is an older name, is
  silently ignored, and presents as "the scanner just never runs". Verified against the
  0.64 options table.
- **`ND_ENABLEINSIGHTSCOLLECTOR=false`.** Anonymous usage reporting is on by default
  upstream.
- **The tag is pinned and manual-update-only.** Navidrome migrates its SQLite schema
  forward on every start and ships no down-migration, so an older binary will not open a
  database a newer one has touched. The tag is not a rollback; back up
  `${CONFIG_DIRECTORY}/navidrome` before bumping.
- **No `autoheal=true`.** A restart part-way through a scan of a 1.8 TB library costs more
  than the restart can win back, and "web server dies while the scanner runs" is not a
  failure mode seen here.
