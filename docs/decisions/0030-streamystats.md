# ADR-0030 — Streamystats: three containers, its own database, and a corrected premise

**Date:** 2026-09-02
**Status:** accepted
**Related:** ADR-0006 (pinned tags), ADR-0009, ADR-0011, ADR-0016

## Decision

Add Streamystats — Jellyfin watch statistics, read from the Jellyfin API with no
playback-reporting plugin required — pinned at `v2.18.1`, loopback-only, with
**its own database**.

It is **three** containers, not one: a Next.js UI, a job server, and the DB.

## The premise it was chosen on was wrong, and the conclusion survives

Streamystats was picked over Jellystat because "it uses vector embeddings, and
this box already runs pgvector for `playlist-generator`", making a shared
database tempting.

**It does not use pgvector.** It requires **VectorChord**
(`tensorchord/vchord-postgres`), a different extension. Sharing
`playlist-generator-db` was never possible — the schema could not have loaded.

So "its own DB" turns out to be **forced** rather than merely preferable. That
is the right answer regardless, for the reason already encoded in this repo:
`playlist-generator-db` is manual-update-only because you never bump a database
engine under live data, and two applications running their own migrations
against one schema produces an outage nobody can cleanly revert.

The _actual_ value stands on its own: real play counts and watch history are
exactly the signal `playlist-generator` currently guesses at. **Wiring the two
together is a follow-up**, not this record — and it will be an API-to-API
integration, not a shared schema.

## Cost, stated plainly

- **+3 containers**, taking the stack from 29 to 32.
- **A second Postgres engine** (`pg17` + VectorChord, 559 MB image) alongside
  playlist-generator's pgvector. Two engines, two upgrade paths, both
  manual-update-only.
- **A large one-time sync.** The first full sync walks 3 libraries, 25 films and
  **169,732 music items** at roughly 66 s per 1,000-item page — on the order of
  three hours of Jellyfin API traffic and Postgres writes. It is one-time, but
  it lands on the NVMe that ADR-0023 records at **42 % endurance used**, so it
  is a real if modest write cost rather than free.

None of that is a reason not to do it; all of it is a reason to have written it
down.

## Hardening

Standard `svc-hardened-tz` shape: `cap_drop: ALL`, `no-new-privileges`, capped
logs, loopback publish, healthchecks.

`streamystats-db` adds `CHOWN`, `SETUID`, `SETGID`, `DAC_OVERRIDE`, `FOWNER` —
the Postgres entrypoint runs as root to `initdb` and chown `PGDATA`, then drops
to the `postgres` user. Same shape as the LSIO init set (ADR-0001), and no
wider.

No `swag=enable`, no proxy-conf: reach it at `127.0.0.1:3400` over an SSH
tunnel, like the \*arr WebUIs. No `autoheal` label — a statistics dashboard is
not worth an auto-restart path (ADR-0009).

## Two healthchecks that had to be measured

Both images are minimal in ways that break the obvious probe:

- **`streamystats-jobs` is a compiled binary with a shell but no `node`, `wget`,
  `curl` or `nc`.** A `fetch()`-based probe left the container permanently
  `starting` while the server was logging `status=running port=3005` — i.e. a
  false negative that would have looked like a broken service forever. The probe
  now reads the kernel's own socket table: `grep -q ":0BBD" /proc/net/tcp*`,
  where `0BBD` is 3005 in hex and state `0A` is LISTEN. That proves the port is
  bound and claims nothing more.
- The UI image **does** have `node`, so its probe uses `fetch`.

This is the third distroless/minimal-image healthcheck trap in this pass, after
Beszel's two (ADR-0028). The general rule now worth stating: **never write a
healthcheck without running its exact command inside the container first.** A
healthcheck that cannot run is not a failed check, it is a container that
reports unhealthy forever, or — worse, if the command silently succeeds — one
that reports healthy forever.

## A dedicated Jellyfin API key

Minted `streamystats` as its own key via `POST /Auth/Keys?App=streamystats`,
stored as `API_KEY_JELLYFIN_STREAMYSTATS`. Jellyfin now has three: `Jellyseerr`,
`arr-integrations` (ADR-0016's `API_KEY_JELLYFIN_ARR`) and this one.

The reason is the same one that produced `API_KEY_JELLYFIN_ARR`: each consumer
gets a key that can be revoked without breaking the others. Revoke with
`DELETE /Auth/Keys/<key>`.

## Registration went through the database on purpose

The setup wizard is a Next.js **server action**, not a REST endpoint, so it
cannot be driven headlessly. The `servers` table stores `api_key` as plain
`text` with no application-side encryption — verified from the schema before
touching it — so an `INSERT` is equivalent to what the wizard writes:

```sql
insert into servers (name, url, api_key, startup_wizard_completed)
values ('kartoffelschen', 'http://jellyfin:8096', '<key>', true);
```

Verified by the sync that followed: `servers=1 users=5 items=1025`, with
`libraries-sync` and `items-sync` progressing through all three libraries.

Had the column been encrypted, this route would have produced a server row the
application could not use — which is why the schema was read first rather than
assumed.

## Embeddings are off

`auto_generate_embeddings` defaults to `false` and stays there. It needs an
external embedding provider and an API key, and none of the value proposition
above depends on it. Turning it on is a separate decision with a separate cost.

## Jellyfin's volume mappings were not touched

Streamystats reads the Jellyfin **API**. It has no media mount at all, so
ADR-0016 is untouched — as it must be.
