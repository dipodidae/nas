# ADR-0049 — Lidarr triggers Navidrome's scan, and only two of its seven checkboxes do anything

**Date:** 2026-09-16
**Status:** accepted
**Extends:** ADR-0044 — Navidrome sits behind the door, except `/rest`

## Context

ADR-0044 added Navidrome reading the same `${SHARE_DIRECTORY}/music` that Lidarr writes,
with `ND_SCANNER_SCHEDULE=1h`. An album imported at 12:05 is therefore invisible to every
Subsonic client until 13:00. Lidarr ships a native `Subsonic` connector whose whole purpose
is to close that gap, so the ask was to wire it.

Two things were measured first, and both changed the shape of the answer.

### Navidrome's filesystem watcher is already running, and it works

Nothing in `compose/media-serve.yaml` sets it, but `Scanner.WatcherEnabled` defaults to
**true** in 0.64 and this deployment has been using it since it came up:

```
1477  Watcher: Scan completed
1323  Watcher: Triggering scan for changed folders  numTargets=1
 131  Watcher: Triggering scan for changed folders  numTargets=2
   1  Watcher: Triggering scan for changed folders  numTargets=4937
```

So this connector is **not** the only thing standing between an import and Navidrome — it
is a second, independent trigger. That is worth having (inotify over a bind mount is exactly
the kind of thing that dies quietly, and this stack has a documented taste for that failure
shape), but it must not be described as the mechanism. It is redundancy, and the ADR says so
because the next person to debug "Navidrome is not seeing new albums" needs to know there are
three paths, not one: the watcher, the hourly schedule, and this.

The watcher is also what makes the verification below defensible: an API-triggered scan logs
`Scanner: Starting scan` with **no** preceding `Watcher: Triggering scan for changed folders`
line. That absence is the attribution.

### `startScan` is admin-gated, and Navidrome has no scan-only role

Measured against 0.64.0, as a freshly created regular user:

```
GET /rest/ping.view           -> 200  status="ok"
GET /rest/getScanStatus.view  -> 200  status="ok"   (full scanStatus payload)
GET /rest/startScan.view      -> 200  status="failed" error code 50
                                      "User is not authorized for the given operation"
```

Two `200`s that prove nothing, and the one call that matters failing inside a `200`. There is
no intermediate role — `startScan` checks `is_admin` and that is the whole gate. So the
choice was a Navidrome admin or no trigger at all.

## Decisions

### 1. A dedicated Navidrome principal, which is a Navidrome admin

`NAVIDROME_LIDARR_USER` / `NAVIDROME_LIDARR_PASSWORD` in `.env`, created with
`navidrome user create` and promoted with `navidrome user edit --set-admin`. Never a human
account, and never a password reused anywhere else — Lidarr sends it as a **plaintext `p=`
query parameter** (the Subsonic legacy scheme; Navidrome accepts both `p=` and `p=enc:<hex>`
and Lidarr uses the former).

This is a deliberate departure from the three-principal least-privilege pattern ADR-0033 set
for ntfy, and it is not one we could avoid. What it costs is bounded and worth writing down:
the account can create and delete Navidrome users and read every library. What it does **not**
get is a route off `nas-network` — the connector points at the container name `navidrome` on
port 4533 with `useSsl` off, never at `navidrome.${PUBLIC_DOMAIN}`. ADR-0044 deliberately
leaves `/rest` un-gated so Subsonic phone clients work, which makes the public hostname
reachable and therefore tempting; sending this password through it would put an admin
credential in a query string on the internet. `check-lidarr-navidrome-notification.py`
asserts the host and the `useSsl` flag for that reason, not for tidiness.

### 2. `updateLibrary` is the only field that does anything

The connector saves cleanly, and its Test button goes green, with `updateLibrary` off. It
simply never requests a scan. Asserted.

### 3. Only `OnReleaseImport` and `OnRename` call `Update()`

This is the part that would otherwise be rediscovered by someone wondering why deletions do
not reach Navidrome. From Lidarr's own `Subsonic.cs`:

| Trigger           | Calls                         | Gated on                                     |
| ----------------- | ----------------------------- | -------------------------------------------- |
| `OnReleaseImport` | `Notify()` **and `Update()`** | `Settings.Notify` / `Settings.UpdateLibrary` |
| `OnRename`        | **`Update()`**                | `Settings.UpdateLibrary`                     |
| `OnGrab`          | `Notify()` only               | `Settings.Notify`                            |
| `OnArtistAdd`     | `Notify()` only               | `Settings.Notify`                            |
| `OnArtistDelete`  | `Notify()` only               | `Settings.Notify`                            |
| `OnAlbumDelete`   | `Notify()` only               | `Settings.Notify`                            |
| `OnTrackRetag`    | `Notify()` only               | `Settings.Notify`                            |
| `OnHealthIssue`   | `Notify()` only               | `Settings.Notify`                            |

`Notify()` posts a Subsonic **chat message** (`addChatMessage`), which is not a scan and is
of no use here, so `notify` is off. Every trigger in the bottom seven rows is therefore
**inert** — and Lidarr's UI presents all of them as ordinary checkboxes.

This was measured before it was read. `onArtistDelete` was ticked first, and a real artist
delete (added with `monitor: none` and `searchForMissingAlbums: false`, deleted with
`deleteFiles=false&addImportListExclusion=false`, so nothing on disk was touched) produced
`NotificationService|No tags set for this notification` in Lidarr's debug log and **nothing
whatsoever** in Navidrome — no request, no scan, no `lastScan` movement.

So the enabled set is `onReleaseImport`, `onUpgrade` (which is what lets `OnReleaseImport`
fire for a replaced file rather than only a new one) and `onRename`. The other five are
asserted **off**, in that direction deliberately: a ticked inert checkbox is worse than an
unticked one, because it reads like a working feature.

Deletions consequently do **not** reach Navidrome from Lidarr. The watcher and the hourly
scan both handle them, which is why this is a recorded consequence rather than a problem.

### 4. The Test button is not evidence, and here is what is

Lidarr's `Test()` calls `GetVersion()` — a ping. It never calls `startScan`. Firing it
returned HTTP `200` while Navidrome's log stayed empty and `lastScan` did not move. The
`200` is still worth one thing and one thing only: `Test()` catches
`SubsonicAuthenticationException` and fails with "Incorrect username or password", so a
green test does prove the **stored credentials authenticate**.

The scan itself was proven by replaying the exact request `SubsonicServerProxy.Update()`
builds — `GET /rest/startScan?u=&p=&c=Lidarr&v=1.15.0`, no `f` parameter, so an XML
response — from inside the Lidarr container, over `nas-network`, with the credentials Lidarr
has stored:

```
$ docker exec lidarr curl -s "http://navidrome:4533/rest/startScan?u=lidarr&p=<pw>&c=Lidarr&v=1.15.0"
<subsonic-response ... status="ok" ...><scanStatus scanning="true" .../></subsonic-response>

lastScan  17:56:13.712  ->  18:00:53.195
navidrome  18:00:51  Scanner: Starting scan       fullScan=false numLibraries=1
           18:00:53  Scanner: Finished scanning all libraries  duration=1.99s
```

No `Watcher: Triggering` line in that window, so the scan is attributable to the request and
to nothing else.

### 5. It is asserted at runtime, because none of it is in this repo

The connector lives in Lidarr's SQLite DB; the principal lives in Navidrome's. A config
restore, a tag bump or someone tidying users in the Navidrome UI undoes either without
touching a tracked file, and every one of the resulting failures is silent —
`make check` cannot see any of it.

`scripts/check-lidarr-navidrome-notification.py`, wired into `make verify-runtime`, asserts
the field values, both directions of the trigger set, that `.env`'s credentials still
authenticate, and that the principal still holds `adminRole`. It probes `getUser`, never
`startScan`, so running the check does not kick off a library scan.

All four of its failure paths were driven red and restored: wrong password (`2`), unset env
(`2`), Lidarr unreachable (`2`), `--set-regular` on the principal (`1`), and
`updateLibrary=false` plus a ticked `onAlbumDelete` (`1`).

## Consequences

- A new album reaches Navidrome in seconds instead of up to an hour.
- Navidrome now has **three** independent triggers: this connector, the built-in filesystem
  watcher, and the hourly `ND_SCANNER_SCHEDULE`. Do not remove one on the grounds that
  another exists without saying which failure it was covering.
- Lidarr holds a Navidrome admin credential. If Lidarr is compromised, so is Navidrome.
- Each import costs one incremental scan, measured at ~2 s against 168 099 tracks across
  18 607 folders. Navidrome serializes scans, so a burst from the backlog drip coalesces.
- `make bootstrap` and ADR-0044 are unaffected; no compose change was needed.
