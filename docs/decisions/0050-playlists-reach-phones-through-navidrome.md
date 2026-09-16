# ADR-0050 — Playlists reach a phone through Navidrome, in both directions

**Date:** 2026-09-16
**Status:** accepted
**Extends:** ADR-0044 (Navidrome behind the door, `/rest` open), ADR-0049 (Lidarr triggers the scan)

## Context

Jellyfin held 37 audio playlists, 3764 tracks, in its own config as `playlist.xml`
with absolute paths in Jellyfin's namespace (`/data/movies/music/...`, ADR-0016).
Nothing else could read them, and Jellyfin is not what a phone speaks — Symfonium,
substreamer, DSub and play:Sub all speak **Subsonic**, which here means Navidrome.

Two directions were needed, and they are deliberately different mechanisms:

- **out of Jellyfin**, once, for the playlists that already exist
  (`scripts/export_jellyfin_playlists.py`);
- **into Navidrome**, continuously, for each new playlist the generator makes
  (`webapps/jellyfin-playlist-generator`, the "Push to Navidrome" button).

## Decisions

### 1. The export writes `.m3u8` into the music tree; the push uses the API

Not an inconsistency — they optimise for different things.

The **export** writes files to `${SHARE_DIRECTORY}/music/Playlists/`. Navidrome
auto-imports them (`autoimportplaylists`, on by default), and a file-imported
playlist is **owned by the first admin — `tom`** (measured), which is the only
mechanism that yields a playlist owned by a human without holding that human's
password. It also carries `sync=1`, so re-running the export updates in place.

The **push** goes through Subsonic `createPlaylist`, because it must be
immediate. A file-based push waits on a scan, and a scan can be unavailable for
hours: a full re-read of 168,099 tracks took ~2 h during this work and rejected
every other scan with `already scanning`. A button that silently does nothing
for two hours is not a button. The push therefore never touches the scanner.

The cost of the API route is that the playlist belongs to whoever authenticates,
so `navidrome_user` is a setting, its description says to use the account you
browse with, and the password is left blank on purpose — `/navidrome/status` then
reports `configured: false` and the button stays hidden rather than creating
playlists under the wrong owner.

### 2. Paths are relative, and the Jellyfin prefix is read, not hardcoded

The same tree is `/data/movies/music` to Jellyfin, `/music` to Navidrome,
`/data/music` to Lidarr and `/mnt/drive/music` on the host. Only a relative path
is true in all four, so the `.m3u8` files carry `../Artist/Album/track.ext` —
which Navidrome resolves from a subfolder (measured, so no `ND_PLAYLISTSPATH` is
needed) and which any other player reads as long as `Playlists/` stays beside
the music.

The Jellyfin prefix comes from `/Library/VirtualFolders`, the entry whose
`CollectionType` is `music`. ADR-0003's repath broke two consumers that had a
prefix compiled into them; a tool that writes paths gets its prefix from the
service that owns them. A playlist matching **no** known root is exit 2, the
`lidarr_jellyfin_bridge.py` rule, not a warning.

### 3. A Subsonic `path` is synthesised from tags and must never be matched on

The single most dangerous thing found here. `search3` returns

```
"path": "Kreator/Flag of Hate/01-02 - Take Their Lives.mp3"
```

for a file that is really `Kreator/1986 - Flag of Hate/02 - Take Their Lives.mp3`.
It is built from tags, not the filesystem. Matching on it would look exact,
succeed often, and mismatch silently. The push therefore scores candidates on
title, artist and duration, and a unit test asserts the score is **unchanged**
when a misleading `path` is injected.

### 4. Subsonic reports failure inside a `200`, so a status check is not a check

`ping` with a wrong password is `HTTP 200` with `status="failed"` and error 40.
The existing Jellyfin credential test in the playlist-generator returns ok on
`status_code == 200`; copying that pattern would have gone green for a wrong
Navidrome password. Everything here unwraps `subsonic-response` and only treats
`status == "ok"` as success — verified by driving a wrong password and watching
it report `available: false`.

The same shape hides authorisation: a non-admin `startScan` is a `200` carrying
error 50 (ADR-0049).

### 5. Names live in `#PLAYLIST:`, not in the filename

Navidrome prefers the `#PLAYLIST:` tag over the filename (measured), so the
filename can be sanitised hard while the real name survives — including
`Слово пацана. Пыяла/Музыка с сериала / Аигел`, whose slashes cannot go in a
filename at all. Collisions after sanitising are broken deterministically,
because letting the second overwrite the first would lose a playlist while
reporting success.

### 6. A re-push replaces; a re-export prunes only what it wrote

Navidrome will hold two playlists with the same name. Jellyfin's copy of this
library already demonstrates where that ends: two `✦ 80s Italo Disco Party`,
two `✦ Danceable 80s Electronic`, from the generator being run twice. The push
therefore deletes same-named playlists **owned by us** after the replacement
demonstrably exists, and reports `replaced_count`.

The export's `--prune` is gated on an `#EXT-X-JELLYFIN-PLAYLIST-ID` marker line,
so it can never delete a playlist someone made by hand and dropped in the same
folder.

### 7. `Playlists/` inside the music tree is invisible to every existing consumer

Checked before writing anything, not after: `music_library_sweep.py` and
`album_art.py` both key off **audio** file extensions, and Lidarr reports no
unmapped folder for it. A directory of `.m3u8` files is therefore inert to all
three.

## Consequences

- 37 playlists / 3764 tracks are in Navidrome, owned by `tom`, reachable from
  any Subsonic client. `getPlaylists` — the call Symfonium makes — returns 37
  playlists summing to exactly 3764 tracks, matching the files byte for byte.
- **Navidrome does not reap a synced playlist when its `.m3u8` disappears.**
  Three test playlists survived the file being deleted, a quick scan and a
  restart. Renaming an exported playlist therefore leaves the old one behind
  until it is deleted through the API. The exporter's `--prune` cleans the
  files, not Navidrome's rows.
- **A full scan survives a restart.** `full_scan_in_progress` on the `library`
  row is what relaunches it, not the `LastScanType` property, which Navidrome
  rewrites back to `full` on boot. Cancelling one means stopping Navidrome,
  clearing that column and starting again — back up the database first.
- ADR-0044's `:ro` on the music mount still holds. Navidrome only **reads** the
  `.m3u8` files; nothing here enables Navidrome's own playlist export, which is
  the one thing that would need write access.
