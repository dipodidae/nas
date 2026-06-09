# lidarr-bulk

Bulk-import artists and albums into Lidarr by pasting a blob of text — or, with
the **Discover ✨** tab, by describing what you want and letting GPT propose a
list of real albums. Single Nuxt 4 app served at
`https://lidarr-bulk.${PUBLIC_DOMAIN}` (behind SWAG on this stack).

## AI Discover tab

Set `OPENAI_API_KEY` (the stack reuses the same key as the playlist-generator)
to unlock a third tab. Type a prompt like *"The best 80s coldwave albums"*, pick
a count (1–50), and GPT returns canonical `Artist - Album` lines into the album
textarea. Review/trim the list, then **Add all** runs the normal album job —
each album is added to Lidarr and immediately searched.

Guardrails: a strict JSON-schema response (`{ albums: [...] }`), a system prompt
that forbids inventing albums and pins titles to their MusicBrainz-canonical
form, server-side dedupe + count clamp (never trusts the model's count), and
prompt-length limits. The tab hides itself when no key is configured
(`GET /api/ai/status`). Model is configurable via `OPENAI_MODEL` (default
`gpt-4o`).

## Why Fastify-likeNuxt 4

Single moving part. Same toolchain for the parsing UI, the SSE progress
stream, and the Lidarr proxy endpoints — no separate frontend/backend
processes, no extra CORS surface. Nitro's `defineEventHandler` covers the
small REST surface fine, and the build output is one `node` process.

## File tree (Nuxt 4 layout)

```
lidarr-bulk/
├── Dockerfile                    # multi-stage, runs as `node` user
├── nuxt.config.ts                # future.compatibilityVersion: 4
├── package.json
├── lidarr-bulk.subdomain.conf.sample
├── app/                          # client-side (Nuxt 4: client lives under app/)
│   ├── app.vue
│   ├── assets/css/main.css
│   ├── components/CandidateRow.vue
│   ├── composables/useJob.ts     # SSE consumer for /api/jobs/:id/stream
│   └── pages/
│       ├── index.vue             # tabs: Artists / Albums / Discover ✨
│       └── settings.vue          # root folder + profiles + monitor mode
├── server/                       # Nitro (Nuxt 4: still at root, not under app/)
│   ├── routes/
│   │   └── healthz.get.ts        # top-level /healthz (not /api/healthz)
│   ├── api/
│   │   ├── parse.post.ts
│   │   ├── settings.get.ts / settings.put.ts
│   │   ├── ai/
│   │   │   ├── suggest.post.ts     # prompt -> GPT -> album ParsedItems
│   │   │   └── status.get.ts       # { enabled, model } for the UI
│   │   ├── lidarr/
│   │   │   ├── profiles.get.ts
│   │   │   ├── lookup-artist.get.ts
│   │   │   └── lookup-album.get.ts
│   │   └── jobs/
│   │       ├── index.post.ts     # create a job
│   │       ├── [id].get.ts       # snapshot
│   │       ├── [id]/stream.get.ts  # SSE progress
│   │       └── [id]/choose.post.ts # resolve a needs-choice item
│   ├── middleware/
│   │   ├── 01.auth.ts            # optional bearer-token gate on /api/*
│   │   └── 02.ratelimit.ts       # in-memory sliding window
│   ├── plugins/init.ts
│   └── utils/
│       ├── env.ts                # zod env schema
│       ├── lidarr.ts             # Lidarr v1 HTTP client
│       ├── parsers.ts            # PURE — tested in tests/
│       ├── jobs.ts               # in-memory store + sequential worker
│       └── settings.ts           # /config/settings.json
├── shared/types.ts               # auto-imported by both app/ and server/
└── tests/parsers.test.ts
```

## API reference (Lidarr v1)

Endpoints verified against the live Lidarr 3.1.2-nightly instance on this
host (`curl -H "X-Api-Key: $K" http://127.0.0.1:8686/api/v1/...`). Auth
header is `X-Api-Key`. Endpoints used:

| Endpoint                      | Method | Used for                                    |
| ----------------------------- | ------ | ------------------------------------------- |
| `/api/v1/system/status`       | GET    | healthcheck                                 |
| `/api/v1/rootfolder`          | GET    | settings page                               |
| `/api/v1/qualityprofile`      | GET    | settings page                               |
| `/api/v1/metadataprofile`     | GET    | settings page                               |
| `/api/v1/artist/lookup?term=` | GET    | artist search                               |
| `/api/v1/album/lookup?term=`  | GET    | album search                                |
| `/api/v1/artist`              | POST   | add artist                                  |
| `/api/v1/album`               | POST   | add album (with embedded artist if missing) |
| `/api/v1/command`             | POST   | trigger `ArtistSearch` / `AlbumSearch`      |

The "add specific album" path posts to `/api/v1/album` with `monitored: true`
and an embedded `artist` object whose `addOptions.monitor = "none"` — that
adds the artist (if not already present) without monitoring all its other
albums, then monitors and searches just the requested one.

**Already in Lidarr is a nudge, not a dead end.** When a POST fails because the
item already exists, the worker looks the existing record up by its foreign
(MusicBrainz) id and gives it a shove instead of just reporting it. For an
artist: force it `monitored`, monitor the whole discography when the job's
monitor mode is `all`, then fire `ArtistSearch` (which grabs every monitored
but missing album). For an album: force it `monitored`, then fire `AlbumSearch`
**only if it's actually missing** (`statistics.percentOfTracks < 100`) — a
fully-downloaded album is just re-affirmed monitored. These items land in the
`nudged` status with a message describing what happened.

## Running locally (without Docker)

```bash
cd webapps/lidarr-bulk
pnpm install
cp .env.example .env
# Edit .env: set LIDARR_URL=http://127.0.0.1:8686 and LIDARR_API_KEY=<your key>
pnpm dev                # http://localhost:3000
pnpm test               # parser tests
pnpm typecheck          # vue-tsc strict pass
```

## Extending the parsers

`server/utils/parsers.ts` is pure — no Nuxt/Node-only imports. Add a new
shape:

1. Add a recognizer block to `parseAlbums` (or `parseArtists`) above the
   final `needs-review` fallback. Order matters: earlier blocks win.
2. Add a test case in `tests/parsers.test.ts` covering the new shape and at
   least one near-miss that should still fall to `needs-review`.
3. `pnpm test` and ship.

Don't try to be clever with ambiguous shapes — adding to `needs-review`
surfaces them in the UI for the user to confirm, which is better than a
silent mis-add.

## Hardening notes

- Runs as user `node` (uid 1000) inside the container.
- `cap_drop: ALL`, `no-new-privileges: true`, port bound to `127.0.0.1`.
- `APP_BEARER_TOKEN` (env) — if set, `/api/*` requires
  `Authorization: Bearer <token>`. Leave unset to disable.
- Rate limit: `RATE_LIMIT_PER_MINUTE` (default 30) sliding window per IP on
  `/api/*` (excluding the SSE stream).
- Body size cap: `BODY_LIMIT_BYTES` (default 256 KB) enforced in
  `/api/parse`. Tune `client_max_body_size` in the SWAG conf to match.
- SWAG conf ships with Authelia/Authentik/basic-auth includes commented out
  for easy enablement later.
