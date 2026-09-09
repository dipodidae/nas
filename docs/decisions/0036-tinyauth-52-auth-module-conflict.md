# ADR-0036 — tinyauth v5.2.0 rejects SWAG's own `proxy.conf`, and its DB upgrade is one-way

**Date:** 2026-09-10
**Status:** accepted
**Incident:** every protected route served 500 for ~7 minutes on 2026-09-10 during a
routine `v5.1.3 → v5.2.0` bump
**Related:** ADR-0034 (the one door), ADR-0022 (confs are tracked)

## What happened

A deliberate tinyauth version bump — the ADR-0034 pin says the update must be
"chosen, never inherited", and it was, with the release notes read first — took
all 13 protected routes to `500`. tinyauth was **healthy** throughout, its login
page served `200`, and `nginx -t` passed. Only the auth **subrequest** failed.

Per ADR-0034 that 500 is the door **jammed shut**, not open, and the design held:
the unprotected routes (jellyfin, nextcloud, ntfy, the apex) kept serving
normally the whole time.

## Two faults, stacked

Both were pre-existing in this repo's conf and both were latent until v5.2.0.
Fixing the first only revealed the second.

### 1. `proxy_set_header Content-Length "";` → a malformed header → 400

The conf paired `proxy_pass_request_body off` with an explicit empty
`Content-Length`. That emits a literal `Content-Length: ` with no value, which
tinyauth's Go HTTP server rejects with `400 Bad Request` **before any auth logic
runs**. Proven on a raw socket from inside swag:

```
printf 'GET /api/auth/nginx HTTP/1.1\r\nHost: tinyauth\r\n...\r\nContent-Length: \r\n\r\n'
  -> HTTP/1.1 400 Bad Request
```

v5.1.3 tolerated it; v5.2.0's updated dependencies do not. `proxy_pass_request_body
off` already drops both the body and the header, so the line was never needed.
SWAG's current sample sets neither.

### 2. `include proxy.conf` sends **two auth modules'** headers → 400

The real one, and the reason the first fix appeared not to work. tinyauth v5.2.0
added an anti-spoofing check (upstream #1089) and says so plainly:

```
WRN Request carries headers for multiple auth modules, possible spoofing attempt, denying
ERR Failed to get proxy context from request error="conflicting auth module headers"
    path=/api/auth/nginx status=400
```

`/config/nginx/proxy.conf` sets the `X-Forwarded-*` family **and**
`X-Original-URL` / `X-Original-Method`, the latter for ingress-nginx-style auth
backends. Two families, two modules, denied. Bisected from inside swag against
the live endpoint — a single header is the whole difference:

| headers sent                       | result                              |
| ---------------------------------- | ----------------------------------- |
| `X-Forwarded-*` only               | **401** ← what `auth_request` needs |
| `X-Forwarded-*` + `X-Original-URL` | **400** → nginx 500 → door shut     |

**`proxy_set_header X-Original-URL "";` does not fix it.** proxy.conf is included
at the same level, so nginx emits its value alongside the empty one; a header set
by an include cannot be un-set beside it. The location therefore no longer
includes `proxy.conf` at all and declares its own minimal set instead.
`resolver.conf` is still included — it is what makes the variable `proxy_pass`
resolve at runtime, which is itself an ADR-0034 invariant.

**SWAG's own sample still includes `proxy.conf` with no clearing**, so this is an
upstream mismatch between the SWAG sample (2025/06/08) and tinyauth ≥ v5.2.0, not
a local mistake. Expect it to bite again on a SWAG sample refresh.

## The mistake worth recording: rolling back made it worse

On seeing the 500s the first move was `v5.2.0 → v5.1.3`. That was wrong twice
over.

First, the hypothesis was wrong — the failure was in the conf, not the version, so
the rollback could not have helped.

Second and worse: **v5.2.0 migrates its SQLite database to schema 11, and v5.1.3
has no down-migration for it.** v5.1.3 crash-looped:

```
failed to setup database: failed to migrate database:
no migration found for version 11: read down for version 11 migrations/sqlite: file does not exist
```

Because `swag` has `depends_on: tinyauth: condition: service_healthy`, a
crash-looping tinyauth meant **swag itself would not start** — turning a
protected-routes outage into a total one, including the routes that are
deliberately never gated. That is a strictly worse failure than the one being
fixed.

**tinyauth's upgrade is one-way, exactly like Jellyfin 12.0's (ADR-0035).** Take a
copy of `${CONFIG_DIRECTORY}/tinyauth` before bumping the tag; the tag alone is
not a rollback. Going _forward_ to v5.2.0 restored health in 10s.

## The rule

1. Back up `${CONFIG_DIRECTORY}/tinyauth` before any tinyauth tag change. The
   schema migration is one-way and the old binary will not start against it.
2. Diagnose the subrequest, not the version. `docker exec swag curl` against
   `http://tinyauth:3000/api/auth/nginx` with an explicit header set is the whole
   debugging loop; `401` is success, anything else becomes a client 500.
3. Read `/config/log/nginx/error.log` first. It named both faults exactly
   (`auth request unexpected status: 400`, and earlier `tinyauth could not be
resolved`) while every healthcheck stayed green.
4. Never roll a database-backed auth service back on a hypothesis. Confirm the
   cause first — the door being shut is survivable, swag not starting is not.

## Verified after the fix

```
$ scripts/check-door-live.sh
    ok: 13 doors closed, apex public, /ops.html gated
```

All seven protected subdomains plus `/ops.html` answer `302` to
`https://auth.4eva.me/login?login_for=app&redirect_uri=…` — the redirect coming
from tinyauth's own `X-Tinyauth-Location` header, so the identity-header path
works too. The apex stays `200`, and jellyfin/nextcloud/ntfy remain ungated.
