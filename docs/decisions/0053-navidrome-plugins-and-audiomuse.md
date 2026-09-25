# ADR-0053 — Navidrome plugins, and AudioMuse-AI answers Instant Mix

**Date:** 2026-09-25
**Status:** accepted
**Extends:** ADR-0044 (Navidrome), ADR-0046 (album art), ADR-0034 (the door)

## Decision

Three Navidrome plugins, pinned by sha256 in `navidrome/plugins/plugins.lock` and
installed by `make navidrome-plugins`:

| Plugin            | Version | Role                                                              |
| ----------------- | ------- | ----------------------------------------------------------------- |
| `audiomuseai`     | 10.0.0  | Instant Mix / `getSimilarSongs2` / similar artists from AudioMuse |
| `nd-lyrics`       | 8.1.0   | Lyrics from LRCLIB → lrcmux → NetEase → KuGou → lyrics.ovh        |
| `coverartarchive` | 1.0.0   | Album art from MusicBrainz + CAA, **last** in the chain           |

plus **AudioMuse-AI 3.6.2** (`compose/audiomuse.yaml`: Postgres, Flask, two workers),
which analyses every track's audio (MusiCNN + CLAP) and answers similarity from the
audio itself instead of Last.fm's listening graph. It sits behind the tinyauth door at
`audiomuse.${PUBLIC_DOMAIN}`.

The mood-playlist plugin (`craiglush/navidrome-mood-plugin`) was evaluated and **not**
installed — see the end.

## Why AudioMuse

Last.fm similarity is built from listening data, and it is thinnest exactly where this
library is thickest: the underground half ADR-0046 already found missing from every art
source. AudioMuse needs no metadata at all. By effect, on the first 87 analysed tracks
(Witchhammer, Mare, Den Saakaldte, Muslimgauze, KK Null…) it answered Instant Mix for a
Witchhammer seed with the other Witchhammer tracks first, then Anthrax, Mare and Azaghal.
That was from an 87-track pool, so the tail was noise.

## What was measured

### Instant Mix is AudioMuse's, and falls back gracefully

- Plugin on: Navidrome's `getSimilarSongs2` and AudioMuse's `/api/similar_tracks` for the
  same seed share **90%** of ids, and AudioMuse's Flask log shows the call arriving with
  `User-Agent: Navidrome/0.64.1`.
- Plugin disabled: Navidrome still returns 10 songs — **0%** shared. That is Last.fm,
  and nothing logs the change. That silence is why `check-navidrome-plugins.py` compares
  **overlap** rather than "was the list non-empty".
- AudioMuse stopped, plugin on: Navidrome falls back to Last.fm in **2.7 s**. It does not
  error. So `navidrome` does not `depends_on` AudioMuse.

`ND_AGENTS=audiomuseai,deezer,lastfm,listenbrainz,coverartarchive`. Navidrome uses the
**first** agent that answers similar songs, so `audiomuseai` must lead; `make check`
asserts it (`navidrome-agent-chain`).

### Throughput — the first pass is ~9 days, and the shape is two workers

The library is **169,077 tracks / 13,521 h** of audio. The metric is audio-seconds
analysed per wall-second, because tracks here run from 90 s to 20 min and s/track lies.

| Configuration                                         | × realtime |
| ----------------------------------------------------- | ---------- |
| 1 worker, `cpus: 8`, defaults (per-song model reload) | 30.3       |
| 1 worker, `cpus: 14`, no per-song reload              | 35.3       |
| **2 workers, `cpus: 8` each**, no per-song reload     | **64.6**   |

One worker analyses one track at a time and ONNX stops scaling well past ~8 threads: 75%
more CPU bought 16%. The queue is in Postgres with per-album advisory locks, so a second
identical worker takes the next album, and throughput doubles. 13,521 h ÷ 64.6 ≈ **8.7
days**.

Both workers run at `cpu_shares: 128`, one eighth of every other container's weight, so
they only get idle CPU. Measured at load average 17 on 16 threads: Navidrome `search3` took
74 ms, Jellyfin `/health` 36 ms and Lidarr `/ping` 12 ms. This is a real throttle, not a
return of the Pi-era caps ADR-0001 removed. AudioMuse sizes its thread pools from the
cgroup CPU limit, so `cpus:` also sets the pool size.

### Three AudioMuse defaults were wrong for this box

All live in AudioMuse's `app_config` table. **Past first boot it reads only Postgres and TZ
from the environment**, so they are set by `scripts/audiomuse_setup.py`
(`make audiomuse-setup`) through the same `/api/setup` the browser wizard uses.

1. **`LYRICS_ASR_ENABLE=false`.** Whisper-small with beam 5 is the one stage that turns a
   9-day pass into a months-long one. Lyrics come from the LRCLIB slot instead; misses get
   AudioMuse's instrumental sentinel.
2. **`MUSICSERVER_LYRICS_TIMEOUT=0`.** AudioMuse asks Navidrome for lyrics first, and with
   `nd-lyrics` installed, Navidrome answers a miss by trying all five providers: ~3 s of wait
   per track and ~5 third-party requests, × 169k. The LRCLIB slot is one request to the
   provider nd-lyrics would try first.
3. **`PER_SONG_MODEL_RELOAD=false`.** The default reloads MusiCNN and CLAP for every track.
   That guards GPU VRAM, and this container has no GPU.

**LRCLIB slot trap:** `LYRICS_API_1_URL_TEMPLATE` must be the **bare** URL
(`https://lrclib.net/api/get`). The first attempt used `{artist_param}` placeholders, which
raised `KeyError('artist_param')` on every track. It was logged as a lyrics MISS, not an
error. After the fix, the same run fetched Swedish lyrics for Den Saakaldte.

The setup save answers `"status": "partial"` **every time**. The worker-restart ack budget
is shorter than a restart; both workers logged `received: restart` and a new `starting` line
~2 s later. The script reports it as a note, not a failure.

### Lyrics — proven by effect

`getLyricsBySongId` for Judas Priest "Painkiller" returned 42 **synced** lines, with
`provider 'lrclib' returned lyricsfile lyrics` in Navidrome's log. The first config used
`providerMode: sync` (query every provider for the best sync level). It was changed to
**`priority`** (first hit wins): LRCLIB's hits are usually synced already, and sync mode's
extra calls are paid on every miss. Navidrome's own web UI does not render plugin lyrics
(upstream limitation); Symfonium, Feishin and substreamer do.

`writeLyrics` is off and the plugin gets **read-only** library access. It asks for
filesystem access only to write `.lrc` sidecars, `/music` is mounted `:ro`, and a plugin
declaring the `library` permission will not enable without a grant at all.

### Cover Art Archive — installed as asked, and measured at zero again

125 albums had no local art. Reprocessing them with the new chain
(`navidrome artwork reprocess --kind al --source absent`) filled **53 from Last.fm** and
**0 from CAA**: every CAA lookup 404'd. This matches ADR-0046's 0/30. The Last.fm hits are a
side effect of commit d7a8f4b enabling that agent after these albums were first resolved:
their trace read `external: no enabled agent provides album images`.

CAA stays **last** in the chain. It costs nothing while `folder.jpg` wins (CoverArtPriority
tries `external` last), and it will catch the mainstream releases that do reach CAA.

## Things that are not where you would look

- **The plugin table is outside git.** Enabled flag, config and grants live in the `plugin`
  table of `navidrome.db`. A restored DB brings every plugin back **disabled**, and Instant
  Mix silently becomes Last.fm's again. `make verify-runtime` runs
  `check-navidrome-plugins.py`, which was proven to fail (rc=1, "DISABLED" and "0% shared")
  with the plugin off.
- **`navidrome plugin edit/enable` writes the DB; the running server does not re-read it.**
  `navidrome_plugins.sh` ends with a restart for that reason.
- **`docker exec -i` in a `while read` loop eats the loop's stdin**: the first version
  enabled one plugin and exited 0. Only the one call that needs stdin attaches it.
- **A plugin's id is its filename stem**, and `ND_AGENTS`/`ND_LYRICSPRIORITY` reference
  that id. `make check` fails a chain entry that is neither built-in nor in the lock.
- **The Navidrome principal is `audiomuse`, non-admin**, created through the native API
  (`navidrome user create` needs a TTY for the password). AudioMuse streams every track as
  that user over `http://navidrome:4533`, never through SWAG, and has no music mount of its
  own. There is no fourth path namespace for `/mnt/drive/music`.
- **AudioMuse runs as root with every capability dropped.** It writes model caches into
  root-owned `/app/model`. It has no volumes because all its state is in `audiomuse-db`.
  Upstream's `/app/plugin/installed` mount is for AudioMuse's own plugin system, unused
  here, and a `${PUID}`-owned bind mount there would not be writable without
  `DAC_OVERRIDE`.
- **`audiomuse-db` is not in the nightly tar** (a running cluster does not restore). It is
  in `stack_update.py`'s `STORES`/`ONE_WAY`, so a tag bump `pg_dump`s it first. Losing it
  costs ~9 days of CPU, not data.
- **Upgrades move in lockstep**: AudioMuse image, `audiomuseai` plugin and Navidrome.
  Upstream says a skewed trio is the usual cause of errors.
- A **nightly analysis** cron (`30 3 * * *`) lives in AudioMuse's `cron` table. It picks up
  new imports, skips analysed tracks, and does nothing while a pass is live (a partial
  unique index allows one main task).

## Not done

- **The mood plugin.** It stores every track's scores in a plugin KV store capped at
  **50 MB** (169k tracks × ~250 B is at the cap before overhead). It replaces Instant Mix
  too, so it fights `audiomuseai` for the same agent slot. It needs a second full-library
  audio decode by an analyser built from its repo, and it was last pushed 2026-03 against
  Navidrome `develop` 0.61. AudioMuse already produces the same mood/energy/danceability
  features (`Other Features: danceable:…, aggressive:…` per track) and does text search
  over them ("calm piano").
- **AudioMuse clustering playlists.** Run on a partial library they are noise, and the
  `audiomuse` principal would own them, invisible to a human account. Revisit after the
  first pass finishes.
- **The second worker is for the first pass.** One worker handles nightly increments;
  `audiomuse-worker-2` can be removed once the pass is done if the RAM (~1.3 GB) matters.
