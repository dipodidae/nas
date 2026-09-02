# Invariant Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Revision 2 (2026-09-02).** Rev 1 was reviewed against the live host and five tasks were wrong: Watchtower's upstream is archived, monitor-only still pulls, Lidarr's root-folder POST needs five more fields, the Phase D method rewrote nothing it claimed to, and the cron entry had no working directory. Every external claim below has been verified on this box; the command that produced each number is quoted next to it.

**Goal:** Turn every "rule you must remember" in this stack into either a rule that is *mechanically impossible to violate silently*, or a rule that no longer needs to exist because its root cause is gone.

**Architecture:** Three moves, in order of value. (1) **Remove the capability to fail** — Watchtower is replaced with a maintained fork and demoted to monitor-only, which retires the single worst failure mode in this repo's history rather than defending against it. (2) **Convert remembered rules into executable assertions** — five invariants are currently only prose in an ADR; they become assertions in `scripts/check-invariants.sh`, which then runs in CI so drift cannot merge. (3) **Close the real gaps** — the two capability waivers, a latent `CAP_KILL` bug in SWAG found during review, Jellyfin's floating tag, Lingarr's missing route, and the absent off-box backup.

Every task follows the same shape, which is TDD applied to infrastructure: **add the assertion first, watch it fail against the live config, then change the config until it passes.** That ordering is what makes "no regression" checkable rather than hoped for.

**Tech Stack:** Docker Compose v5.5.0 (`include:` + `extends`), Docker Engine 29.7.2 / API 1.55, Bash + Python 3.11+ (stdlib only in `check-invariants.sh`), GitHub Actions, restic (new), pytest/ruff for the existing suites.

**Spec:** There is no separate spec document. This plan implements two live sections of `README.md` — [Rules that will bite you](../../../README.md#rules-that-will-bite-you) (11 rules) and [Known gaps](../../../README.md#known-gaps) (8 items) — against the reasoning already recorded in `docs/decisions/` (ADR-0001 … ADR-0019). Read the ADR named in a task before changing what that task touches; the ADR is the "why" and this plan is only the "how".

---

## Global Constraints

- **The server is live and must stay live.** No task may leave a service down. Every task that recreates a container ends by waiting for `healthy` and asserting the container still exists.
- **`make check` and `make lint` must pass at the end of every task.** A task that leaves them failing is not finished.
- **One task, one commit.** Every task is independently revertible via `git revert`.
- **Never touch Jellyfin's volume mappings.** Owner standing instruction; three systems are calibrated to `/data/movies` (ADR-0016).
- **Never remove `KILL` from qbittorrent's `cap_add`** (ADR-0004), never unpin its tag or drop below 5.2.2 (ADR-0005), never make the stale-lock init script an unconditional `rm -f` (ADR-0005).
- **Never make slskd's healthcheck login-aware** (ADR-0009). Never restart slskd to fix a Soulseek login.
- **Never enable Lidarr in any Cleanuparr module**, and `failedImport.skipIfNotFoundInClient` stays `true` (ADR-0017).
- **`autoheal` restarts unhealthy `qbittorrent`/`slskd` within ~30 s.** When hand-cycling either, expect it. Remove the `autoheal=true` label to suppress; never stop autoheal (ADR-0010).
- **Capability floors, verified empirically not guessed:** `memswap_limit == mem_limit` wherever `mem_limit` is set; `AUTOHEAL_DEFAULT_STOP_TIMEOUT` ≥ max `stop_grace_period` **among autoheal-monitored services** (currently `qbittorrent`, `2m0s`); `CURL_TIMEOUT` > that.
- **Secrets never enter a compose file, a commit, or this plan.** New credentials go in `.env` (gitignored) and are documented, redacted, in `.env.example`.
- **`.env` variable changes must be reflected in BOTH `.env.example` and `AGENTS.md`'s env list.**
- **Exit-code convention for anything under `scripts/`:** `0` success / `1` partial / `2` fatal; side effects in `main()`, pure logic elsewhere (`AGENTS.md`).
- **Baseline for regression proof:** capture `docker compose config > /tmp/plan-baseline.yml` before Task 1 and diff against it at every phase boundary. Only changes a task explicitly declares are permitted.

### Two traps this plan used to walk into

**`.env` is not safely sourceable.** Line 25 is `WATCHTOWER_SCHEDULE=0 0 4 * * *`, unquoted. `set -a; . ./.env; set +a` therefore sets `WATCHTOWER_SCHEDULE=0`, then tries to run `0 4 * * *` as a command (`./.env:25: command not found: 0`) and glob-expands `*` against the working directory. **Task 6 Step 0 quotes that value once, before any other task sources `.env`.** Until it does, read single variables with `sed -n 's/^KEY=//p' .env` rather than sourcing.

**`compose.override.yaml` is gitignored** (`.gitignore:45-46`) *and* auto-loaded by Compose. A forgotten one is invisible to `git status` and silently changes every later `docker compose` command in this plan. **No task writes it.** Failure injection uses an explicit throwaway path instead:

```bash
inject() {  # usage: inject <<'YAML' ... YAML
  cat > /tmp/inject.yaml
  trap 'rm -f /tmp/inject.yaml' EXIT
}
# then run the checker against it:
COMPOSE_EXTRA=-f\ /tmp/inject.yaml ./scripts/check-invariants.sh
```

`scripts/check-invariants.sh` gains support for this in Task 1 Step 0: it reads an optional `COMPOSE_EXTRA` from the environment and appends it to its `docker compose config` invocation. Task 5's `verify-runtime` also asserts no `compose.override.yaml` exists, so a forgotten one becomes an error rather than a mystery.

---

## Phase map

Ordered by blast radius, ascending. Do not reorder — Phase A makes the later phases verifiable.

| Phase | Tasks | Touches a running container? | Risk |
|---|---|---|---|
| **A — Make the rules executable** | 1–6 | No | None. Config-read + docs + CI only |
| **B — Remove the failure capability** | 7–10 | Recreates `watchtower`, `jellyfin`, `swag` | Low, except Task 9 (swag recreate) |
| **C — Close the capability waivers** | 11–12 | Recreates `playlist-generator{,-db}` | Medium |
| **D — Finish the hardlink migration** | 13–15 | Lidarr DB surgery, offline | **High** |
| **E — Off-box backup** | 16–17 | No | Low (new capability) |

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `scripts/check-invariants.sh` | *Modify.* The single executable definition of every stack invariant. Gains 7 assertions, a `COMPOSE_EXTRA` hook, and one HTTP reader. | A |
| `.github/workflows/ci.yml` | *Modify.* Gains an `invariants` job so no violation can merge. | A |
| `README.md` | *Modify.* Corrects the bazarr non-gap; tracks gap closure. | A, and each later phase |
| `docs/decisions/0020-watchtower-replaced-and-demoted.md` | *Create.* Records the fork swap and retiring Watchtower's write access. | B |
| `compose/infra.yaml` | *Modify.* `watchtower` → maintained fork, monitor-only. | B |
| `compose/media-serve.yaml` | *Modify.* Pin Jellyfin's tag. | B |
| `compose/proxy.yaml` | *Modify.* `swag` gains `KILL`. | B |
| `swag/proxy-confs/lingarr.subdomain.conf` | *Create, tracked.* SWAG ships no lingarr sample. | B |
| `webapps/playlist-generator/compose.yaml` | *Modify.* Real capability sets for both services. | C |
| `docs/decisions/0018-capability-gaps.md` | *Modify.* Closed, with the measured sets. | C |
| `scripts/lidarr_repath_db.py` | *Create.* Offline SQLite prefix rewrite — the only method that moves what needs moving. | D |
| `scripts/tests/test_lidarr_repath_db.py` | *Create.* Unit tests for its pure logic, against a synthetic DB. | D |
| `docs/decisions/0003-lidarr-data-mount-staged.md` | *Modify.* Outcome recorded either way. | D |
| `scripts/offsite_backup.sh` | *Create.* restic wrapper, dry-run default. | E |
| `.env.example`, `AGENTS.md` | *Modify.* New `RESTIC_*` vars; prune 10 dead vars. | A, E |
| `Makefile` | *Modify.* `backup-offsite`, `verify-runtime`. | A, E |

---
# Phase A — Make the rules executable

No container is touched in this entire phase. It is pure gain and fully revertible.

---

### Task 1: Assert that no autoheal-monitored healthcheck probes an external dependency

ADR-0009 is the most dangerous rule in the repo to forget, and it is currently enforced by nothing but a comment. A future edit to a login-aware slskd healthcheck would create a permanent restart spiral.

Rev 1 asserted this for slskd by name. That is one numbered section per service, forever. The general rule is the one worth writing: **a healthcheck on an autoheal-monitored service must test liveness of *that* service and nothing else** — because autoheal turns any failing probe into a restart, and restarting service A cannot fix service B. slskd is simply today's instance.

**Files:**
- Modify: `scripts/check-invariants.sh`

**Interfaces:**
- Consumes: the existing `services`, `fail()`, `ok()` helpers and the `# ====` section layout already in the script.
- Produces: a new check id `autoheal-healthchecks-local`, and the `COMPOSE_EXTRA` test hook every later task's failure injection depends on.

- [ ] **Step 0: Add the `COMPOSE_EXTRA` hook (needed by every failure injection in this plan)**

`compose.override.yaml` is gitignored *and* auto-loaded, so injecting failures through it leaves an invisible landmine. Give the checker an explicit, throwaway override path instead. In `scripts/check-invariants.sh`, replace the two `docker compose config` invocations:

```bash
# after: cd "$(dirname "$0")/.." || exit 2
# shellcheck disable=SC2206  # deliberate word splitting: COMPOSE_EXTRA is "-f path"
EXTRA=(${COMPOSE_EXTRA:-})

if ! docker compose "${EXTRA[@]}" config -q 2>/dev/null; then
  echo "FATAL: 'docker compose config' failed -- the compose model does not render." >&2
  docker compose "${EXTRA[@]}" config -q
  exit 2
fi

COMPOSE_EXTRA="$COMPOSE_EXTRA" VERBOSE=$VERBOSE python3 - <<'PY'
```

and inside the Python heredoc:

```python
    _extra = os.environ.get("COMPOSE_EXTRA", "").split()
    raw = subprocess.run(
        ["docker", "compose", *_extra, "config", "--format", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
```

Verify the hook itself before relying on it:

```bash
cd ~/nas
printf 'services:\n  slskd:\n    environment:\n      - CANARY=1\n' > /tmp/inject.yaml
COMPOSE_EXTRA="-f /tmp/inject.yaml" ./scripts/check-invariants.sh -v >/dev/null && echo "hook renders"
COMPOSE_EXTRA="-f /tmp/inject.yaml" docker compose -f compose.yaml -f /tmp/inject.yaml config --format json \
  | python3 -c "import sys,json;print('CANARY' in json.dumps(json.load(sys.stdin)['services']['slskd']))"
rm -f /tmp/inject.yaml
```

Expected: `hook renders`, then `True`.

- [ ] **Step 1: Write the failing assertion**

Insert as a new numbered section immediately before the `# Report` block in `scripts/check-invariants.sh`:

```python
# ==========================================================================
# 11. autoheal-monitored healthchecks must probe THIS service only
# ==========================================================================
# autoheal converts a failing healthcheck into a restart. A probe that depends
# on anything outside the container therefore restarts the wrong thing, and
# keeps doing it. The live instance is slskd: its web server can be up while
# its Soulseek login is dead, the login handshake times out after a hardcoded
# 5000ms while slsknet holds a ghost session, and a restart re-collides with
# that ghost (32->64->128s backoff, never recovers). The cure is staying DOWN
# 15-30 min, so an auto-restart is the exact opposite of it. Login state is
# watched alert-only by scripts/slskd_login_watch.py. ADR-0009.
#
# This is deliberately generic: the next service to get autoheal=true inherits
# the rule instead of needing its own numbered section.
FOREIGN_PROBE = (
    # slskd: Soulseek session state, not web-server liveness (ADR-0009)
    "isloggedin", "/api/v0/server", "/api/v0/session",
    # generic cross-service reach: a probe naming a host that is not itself
    "http://qbittorrent", "http://slskd", "http://prowlarr", "http://lidarr",
    "http://sonarr", "http://radarr", "http://jellyfin", "http://nextcloud",
)
_ah = []
for name, svc in sorted(services.items()):
    labels = svc.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(x.split("=", 1) for x in labels if "=" in x)
    if str(labels.get("autoheal", "")).lower() != "true":
        continue
    hc = svc.get("healthcheck") or {}
    probe = " ".join(str(x) for x in (hc.get("test") or [])).lower()
    if not probe or hc.get("disable"):
        fail("autoheal-healthchecks-local", "ADR-0009",
             f"{name} has autoheal=true but no healthcheck. autoheal then cannot "
             "restart it when its own process actually dies, which is the one "
             "case a restart does help.")
        continue
    hits = [p for p in FOREIGN_PROBE if p in probe and f"//{name}" not in probe]
    if hits:
        fail("autoheal-healthchecks-local", "ADR-0009",
             f"{name}'s healthcheck probes {hits}, which is state outside this "
             "container. With autoheal=true, every transient failure of that "
             "dependency restarts THIS service, and a restart cannot fix it. "
             "Keep the probe to local liveness (a web-UI spider or curl -f "
             "against its own port).")
    else:
        _ah.append(name)
if _ah:
    ok("autoheal-healthchecks-local", f"liveness-only on {', '.join(_ah)}")
```

- [ ] **Step 2: Prove the assertion can fail**

```bash
cd ~/nas
cat > /tmp/inject.yaml <<'YAML'
services:
  slskd:
    healthcheck:
      test: [CMD, curl, -f, 'http://localhost:5030/api/v0/server']
YAML
COMPOSE_EXTRA="-f /tmp/inject.yaml" ./scripts/check-invariants.sh
```

Expected: `FAIL autoheal-healthchecks-local [ADR-0009] slskd's healthcheck probes ['/api/v0/server'] …`, exit 1.

Also prove the missing-healthcheck arm:

```bash
cat > /tmp/inject.yaml <<'YAML'
services:
  slskd:
    healthcheck:
      disable: true
YAML
COMPOSE_EXTRA="-f /tmp/inject.yaml" ./scripts/check-invariants.sh | grep autoheal-healthchecks
rm -f /tmp/inject.yaml
```

Expected: `FAIL … slskd has autoheal=true but no healthcheck.`

- [ ] **Step 3: Prove it passes against the real config**

```bash
./scripts/check-invariants.sh -v | grep autoheal-healthchecks
```

Expected: `ok   autoheal-healthchecks-local   liveness-only on qbittorrent, slskd` — the two services carrying `autoheal=true` today.

- [ ] **Step 4: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): no autoheal-monitored healthcheck may probe a dependency

ADR-0009 was enforced by a comment only. Written as a general rule rather than
a per-service one: autoheal turns a failing probe into a restart, so a probe
that depends on anything outside the container restarts the wrong thing and
keeps doing it. slskd's Soulseek login is today's instance; the next service to
get autoheal=true inherits the rule instead of needing its own section.

Also adds the COMPOSE_EXTRA hook, so failure injection uses an explicit
throwaway -f path instead of compose.override.yaml -- which is gitignored AND
auto-loaded, i.e. invisible to git status and silently active if forgotten.

Verified in three directions with deliberate overrides."
```

---

### Task 2: Derive the autoheal timeout floor from the model instead of trusting a comment

ADR-0010's rule is arithmetic over the live model, so it should be computed, not remembered. Today raising qbittorrent's `stop_grace_period` to 180 s would silently invalidate autoheal's 150 s and nothing would say so.

Three corrections to rev 1, all found by review:

1. `_secs` returns `None` for anything it cannot parse (`1m30.5s`, `1h`), and `max()` over a list containing `None` raises `TypeError` — an unhandled traceback, which the script's own exit convention would then report as "invariant violated" rather than "checker broke".
2. The floor was computed over **every** service. autoheal only ever restarts containers labelled `autoheal=true`, so a long grace period on an unmonitored service would force the timeout up for no reason.
3. autoheal honours a per-container `autoheal.stop.timeout` label that overrides the global default. A service with `autoheal.stop.timeout=20` and `stop_grace_period: 2m0s` passes a global-only assertion and still gets SIGKILLed.

**Files:**
- Modify: `scripts/check-invariants.sh`

**Interfaces:**
- Consumes: `services`, `env_of()`, `fail()`, `ok()`.
- Produces: check id `autoheal-timeouts`.

- [ ] **Step 1: Confirm the live population before asserting over it**

```bash
cd ~/nas
docker compose config --format json | python3 -c "
import sys,json
d=json.load(sys.stdin)['services']
for s,v in sorted(d.items()):
    l=v.get('labels') or {}
    l=l if isinstance(l,dict) else dict(x.split('=',1) for x in l if '=' in x)
    if l.get('autoheal')=='true' or v.get('stop_grace_period'):
        print(f\"  {s}: autoheal={l.get('autoheal')} grace={v.get('stop_grace_period')} \"
              f\"per-container={l.get('autoheal.stop.timeout')}\")"
```

Expected today: `qbittorrent: autoheal=true grace=2m0s per-container=None` and `slskd: autoheal=true grace=None per-container=None`. qbittorrent is the only service in the stack with a `stop_grace_period` at all, so the current numbers are correct by luck — the assertion is what keeps them correct.

- [ ] **Step 2: Write the failing assertion**

Append as section 12, before the `# Report` block:

```python
# ==========================================================================
# 12. autoheal's own timeouts must exceed the longest graceful stop it can hit
# ==========================================================================
# autoheal ignores compose's stop_grace_period and uses its own stop timeout
# (default 10s). Its restart call blocks for that whole timeout, so a shorter
# CURL_TIMEOUT makes it log a failure and re-issue the restart every
# AUTOHEAL_INTERVAL while the first is still in flight -- measured 2026-09-01
# as three overlapping requests against a restart that succeeded at t+150s.
#
# Scope: ONLY services autoheal actually monitors. A long grace period on an
# unmonitored service is autoheal's business never. And a per-container
# `autoheal.stop.timeout` label overrides the global default, so it has to be
# checked against that service's own grace period, not the global floor.
# ADR-0010.
def _secs(v):
    """Compose duration ('2m0s', '90s', '1m') or bare seconds; None if unparseable."""
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
    if not m or not any(m.groups()):
        return None
    h, mi, se = m.groups()
    return int(int(h or 0) * 3600 + int(mi or 0) * 60 + float(se or 0))

def _labels(svc):
    labels = svc.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(x.split("=", 1) for x in labels if "=" in x)
    return labels

ae = env_of("autoheal")
stop_to = _secs(ae.get("AUTOHEAL_DEFAULT_STOP_TIMEOUT", "10"))
curl_to = _secs(ae.get("CURL_TIMEOUT", "30"))

monitored = {s: v for s, v in services.items()
             if str(_labels(v).get("autoheal", "")).lower() == "true"}
graces, unparseable = {}, []
for s, v in monitored.items():
    if not v.get("stop_grace_period"):
        continue
    secs = _secs(v["stop_grace_period"])
    (unparseable.append(f"{s}={v['stop_grace_period']!r}") if secs is None
     else graces.__setitem__(s, secs))

if stop_to is None or curl_to is None:
    fail("autoheal-timeouts", "ADR-0010",
         "could not parse AUTOHEAL_DEFAULT_STOP_TIMEOUT="
         f"{ae.get('AUTOHEAL_DEFAULT_STOP_TIMEOUT')!r} / CURL_TIMEOUT="
         f"{ae.get('CURL_TIMEOUT')!r}.")
elif unparseable:
    fail("autoheal-timeouts", "ADR-0010",
         f"unparseable stop_grace_period on {', '.join(unparseable)} -- the "
         "floor cannot be computed, so it cannot be trusted. Use a plain "
         "compose duration like '120s' or '2m0s'.")
else:
    worst = max(graces.values(), default=0)
    worst_svc = max(graces, key=graces.get) if graces else "(none)"
    # A per-container override must clear THAT service's own grace period.
    per_bad = []
    for s, v in monitored.items():
        override = _secs(_labels(v).get("autoheal.stop.timeout", "")) if \
            _labels(v).get("autoheal.stop.timeout") else None
        if override is not None and override < graces.get(s, 0):
            per_bad.append(f"{s}: autoheal.stop.timeout={override}s < its own "
                           f"stop_grace_period {graces[s]}s")
    if stop_to < worst:
        fail("autoheal-timeouts", "ADR-0010",
             f"AUTOHEAL_DEFAULT_STOP_TIMEOUT={stop_to}s < the longest "
             f"stop_grace_period among autoheal-monitored services ({worst}s, "
             f"{worst_svc}). autoheal would SIGKILL mid-flush, which is exactly "
             "the ungraceful kill that orphans qbittorrent's lockfile.")
    elif per_bad:
        fail("autoheal-timeouts", "ADR-0010",
             "per-container override defeats the global floor -- " + "; ".join(per_bad))
    elif curl_to <= stop_to:
        fail("autoheal-timeouts", "ADR-0010",
             f"CURL_TIMEOUT={curl_to}s must be strictly greater than "
             f"AUTOHEAL_DEFAULT_STOP_TIMEOUT={stop_to}s, or the restart call is "
             "cut off mid-stop, logged as failed, and re-issued on top of the "
             "one still in flight.")
    else:
        ok("autoheal-timeouts",
           f"stop={stop_to}s > worst monitored grace {worst}s ({worst_svc}); "
           f"curl={curl_to}s; {len(monitored)} monitored")
```

- [ ] **Step 3: Prove all four failure arms**

```bash
cd ~/nas
run() { COMPOSE_EXTRA="-f /tmp/inject.yaml" ./scripts/check-invariants.sh | grep autoheal-timeouts; }

# (a) grace period outgrows autoheal
cat > /tmp/inject.yaml <<'YAML'
services: {qbittorrent: {stop_grace_period: 300s}}
YAML
run
# (b) unparseable grace period -- rev 1 crashed here with a TypeError
cat > /tmp/inject.yaml <<'YAML'
services: {qbittorrent: {stop_grace_period: 1m30.5s}}
YAML
run
# (c) per-container override defeats the global floor
cat > /tmp/inject.yaml <<'YAML'
services: {qbittorrent: {labels: [autoheal=true, autoheal.stop.timeout=20]}}
YAML
run
# (d) inverted curl timeout
cat > /tmp/inject.yaml <<'YAML'
services: {autoheal: {environment: [CURL_TIMEOUT=100]}}
YAML
run
rm -f /tmp/inject.yaml
```

Expected, in order: `< the longest stop_grace_period among autoheal-monitored services (300s, qbittorrent)`; `unparseable stop_grace_period on qbittorrent='1m30.5s'`; `autoheal.stop.timeout=20s < its own stop_grace_period 120s`; `CURL_TIMEOUT=100s must be strictly greater than … 150s`. All four must be a `FAIL` line, never a traceback.

- [ ] **Step 4: Prove it passes clean**

```bash
./scripts/check-invariants.sh -v | grep autoheal-timeouts
```

Expected: `ok   autoheal-timeouts   stop=150s > worst monitored grace 120s (qbittorrent); curl=180s; 2 monitored`

- [ ] **Step 5: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): compute autoheal's timeout floor from the live model

ADR-0010's rule is arithmetic over max(stop_grace_period), so derive it rather
than trusting a comment: raising qbittorrent's grace period to 180s used to
silently invalidate autoheal's 150s.

Three things the first draft got wrong, all caught in review:
- an unparseable duration made _secs return None, and max() over it raised
  TypeError -- a traceback the exit convention would misreport as a violation;
- the floor was computed over every service, but autoheal only restarts the
  ones labelled autoheal=true;
- a per-container autoheal.stop.timeout label overrides the global default, so
  a service could pass the global check and still be SIGKILLed.

All four failure arms verified with deliberate overrides."
```

---

### Task 3: Assert Jellyfin's volume mappings are exactly the two intended mounts, source included

The owner's standing instruction (ADR-0016) is the least self-evident rule here — `${SHARE_DIRECTORY}:/data/movies:ro` looks like a bug, so it is the thing a well-meaning future editor is most likely to "fix". Three systems break together if they do.

Rev 1 asserted only `(target, read_only)`, which threw away `source` — so repointing `/data/movies` at a *different host path* would have passed the check that exists to freeze the mapping. The source is half the invariant.

**Files:**
- Modify: `scripts/check-invariants.sh`

**Interfaces:**
- Consumes: `services`, `fail()`, `ok()`.
- Produces: check id `jellyfin-mounts-frozen`.

- [ ] **Step 1: Confirm the current mount shape before asserting it**

```bash
cd ~/nas
docker compose config --format json | python3 -c "
import sys,json
for v in json.load(sys.stdin)['services']['jellyfin']['volumes']:
    print(v['source'], '->', v['target'], 'ro' if v.get('read_only') else 'rw')"
grep -E '^(CONFIG|SHARE)_DIRECTORY=' .env
```

Expected exactly two lines — `…/.docker-config/jellyfin -> /config rw` and `/mnt/drive -> /data/movies ro` — and `SHARE_DIRECTORY=/mnt/drive`.

- [ ] **Step 2: Write the failing assertion**

Append as section 13:

```python
# ==========================================================================
# 13. Jellyfin's volume mappings are frozen (owner standing instruction)
# ==========================================================================
# ${SHARE_DIRECTORY}:/data/movies:ro is intentional even though it looks
# misnamed. Every Jellyfin library path, the *arr mapFrom/mapTo mappings, and
# playlist-generator's LOCAL_PATH_PREFIX/JELLYFIN_PATH_PREFIX pair are all
# calibrated to it, so they would have to change in lockstep. ADR-0016.
#
# SOURCE is asserted as well as target: repointing /data/movies at a different
# host path breaks the same three systems while leaving the target intact, and
# a target-only check would call that ok.
_share = os.environ.get("SHARE_DIRECTORY") or _env_file_value("SHARE_DIRECTORY")
_conf = os.environ.get("CONFIG_DIRECTORY") or _env_file_value("CONFIG_DIRECTORY")
jf = services.get("jellyfin", {})
got = {(v.get("source"), v.get("target"), bool(v.get("read_only")))
       for v in (jf.get("volumes") or [])}
if not _share or not _conf:
    warn("jellyfin-mounts-frozen", "ADR-0016",
         "SHARE_DIRECTORY / CONFIG_DIRECTORY unreadable, so the mount SOURCES "
         "cannot be verified; checking targets only.")
    got = {(t, r) for _s, t, r in got}
    want = {("/config", False), ("/data/movies", True)}
else:
    want = {(os.path.join(_conf, "jellyfin"), "/config", False),
            (_share, "/data/movies", True)}
if got != want:
    fail("jellyfin-mounts-frozen", "ADR-0016",
         "Jellyfin's volume mappings changed and must not.\n"
         f"       expected: {sorted(want)}\n"
         f"       actual:   {sorted(got)}\n"
         "       /data/movies must stay a READ-ONLY mount of the WHOLE share, "
         "from ${SHARE_DIRECTORY} and nowhere else. Three systems are "
         "calibrated to it: Jellyfin's library paths, the *arr mapFrom/mapTo "
         "mappings, and playlist-generator's LOCAL_PATH_PREFIX/"
         "JELLYFIN_PATH_PREFIX pair.")
else:
    ok("jellyfin-mounts-frozen", "/config rw + ${SHARE_DIRECTORY}:/data/movies ro")
```

This needs one shared helper, added once near the top of the Python block (Task 4 reuses it):

```python
def _env_file_value(key, path=".env"):
    """Read one KEY=value from .env. Returns None if unreadable -- .env is
    gitignored and absent in CI, and a missing file must degrade to a warning,
    not a traceback."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        return None
    return None
```

- [ ] **Step 3: Prove it fails on both tempting "fixes"**

```bash
cd ~/nas
# (a) renaming the target -- the obvious "fix"
cat > /tmp/inject.yaml <<'YAML'
services:
  jellyfin:
    volumes: ["${SHARE_DIRECTORY}:/data/media:ro"]
YAML
COMPOSE_EXTRA="-f /tmp/inject.yaml" ./scripts/check-invariants.sh | grep -A4 jellyfin-mounts
# (b) keeping the target, repointing the SOURCE -- what rev 1 called ok
cat > /tmp/inject.yaml <<'YAML'
services:
  jellyfin:
    volumes: ["${SHARE_DIRECTORY}/movies:/data/movies:ro"]
YAML
COMPOSE_EXTRA="-f /tmp/inject.yaml" ./scripts/check-invariants.sh | grep -A4 jellyfin-mounts
rm -f /tmp/inject.yaml
```

Expected: both `FAIL`, with the second showing `/mnt/drive/movies` where `/mnt/drive` was expected. Note compose *appends* to `volumes:` rather than replacing, so both injections show up as an extra mount — either way the set differs from `want` and the check fires.

- [ ] **Step 4: Prove it passes clean**

```bash
./scripts/check-invariants.sh -v | grep jellyfin-mounts
```

Expected: `ok   jellyfin-mounts-frozen   /config rw + ${SHARE_DIRECTORY}:/data/movies ro`

- [ ] **Step 5: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): freeze Jellyfin's volume mappings, sources included

The owner instruction in ADR-0016 protects the least self-evident config in the
stack -- /data/movies looks misnamed, so it is what someone will 'fix'. Three
systems are calibrated to it and break together.

Asserts (source, target, read_only), not just (target, read_only): repointing
/data/movies at a different host path breaks the same three systems while
leaving the target intact, and a target-only check called that ok. Degrades to
a warning where .env is unreadable (CI) rather than crashing."
```

---
### Task 4: Assert the real fix behind ADR-0007 — against the running session, not a file

`mem_limit: 4g` is the *backstop*. The actual fix for qBittorrent's 21.1 GB cgroup peak is the OS-cache bypass — and it lives outside the compose model entirely, where a person can revert it through the WebUI with no trace in this repo.

Rev 1 read `qBittorrent.conf` and, worse, `sed -i`'d the **live** file to prove the assertion could fail, then argued that was safe because the process was never restarted. That argument does not hold: autoheal restarts qbittorrent within ~30 s of it going unhealthy, so a restart inside the test window would silently bring `EnableOSCache` back. It was the one place the document did the thing it exists to prevent.

Two corrections:

1. **Read the API, not the file.** `qBittorrent.conf` is written at shutdown and read at startup, so it can disagree with the running session in both directions. `GET /api/v2/app/preferences` is what is actually in effect.
2. **Never write the live file.** The negative test runs against a fixture.

And one thing rev 1 missed entirely. Verified live today:

```
disk_io_read_mode  = 0    # DisableOSCache
disk_io_write_mode = 0    # DisableOSCache
disk_io_type       = 0    # Default -> libtorrent's mmap_disk_io on 64-bit
```

`Session\DiskIOType` does not appear in `qBittorrent.conf` at all, i.e. it is at its default. **`DisableOSCache` is the mitigation; it is not the removal.** libtorrent 2.x still mmaps torrent data, and the kernel still accounts those pages to the cgroup — which is the actual root of the 21.1 GB number. Setting `DiskIOType` to the POSIX-compliant backend is what stops libtorrent using mmap at all.

That is a real behaviour change with a throughput cost, so this task **asserts** the current state and **measures** the alternative; it does not flip it blind. See Step 5.

**Files:**
- Modify: `scripts/check-invariants.sh`

**Interfaces:**
- Consumes: `os`, `json`, `urllib.request`, `fail()`, `ok()`, `warn()`, and `_env_file_value()` from Task 3. Reads `QBITTORRENT_USER`/`QBITTORRENT_PASS`/`QBITTORRENT_HOST` the same way `scripts/qbittorrent_settings_enforce.py` does.
- Produces: check ids `qbit-oscache-disabled` and `qbit-diskio-type`.

- [ ] **Step 1: Confirm the live values, and that the API is the right source**

```bash
cd ~/nas
U=$(sed -n 's/^QBITTORRENT_USER=//p' .env); P=$(sed -n 's/^QBITTORRENT_PASS=//p' .env)
curl -s -c /tmp/qc -d "username=$U&password=$P" http://127.0.0.1:8080/api/v2/auth/login; echo
curl -s -b /tmp/qc http://127.0.0.1:8080/api/v2/app/preferences | python3 -c "
import sys,json; d=json.load(sys.stdin)
for k in ('disk_io_read_mode','disk_io_write_mode','disk_io_type'): print(f'  {k} = {d[k]}')"
rm -f /tmp/qc
grep -nE 'DiskIO' .docker-config/qbittorrent/qBittorrent/qBittorrent.conf
```

Expected: `0 / 0 / 0` from the API, and the conf showing `Session\DiskIOReadMode=DisableOSCache` / `…WriteMode=DisableOSCache` with **no** `DiskIOType` line. Note the plain `curl` without a cookie returns `Forbidden` — qBittorrent's localhost auth-bypass is off on this host, so the checker must log in.

- [ ] **Step 2: Write the failing assertion**

Append as section 14. This is the first check that leaves the compose model, so it degrades to a warning when qBittorrent is unreachable (as in CI) rather than failing the build:

```python
# ==========================================================================
# 14. qBittorrent's disk-IO settings are what ADR-0007 actually depends on
# ==========================================================================
# mem_limit: 4g is only the backstop. libtorrent 2.x mmaps torrent data and the
# kernel accounts those pages to the cgroup; with OS cache enabled this cgroup
# peaked at 21.1GB (journalctl, 2026-09-01) and contributed to host-wide OOM
# kills. These settings live in qBittorrent's own config, which a person can
# revert through the WebUI with no trace in this repo. ADR-0007.
#
# Read from the RUNNING session, not qBittorrent.conf: the file is written at
# shutdown and read at startup, so it can disagree with reality in both
# directions. The API is what is in effect.
_QBT_MODES = {0: "DisableOSCache", 1: "EnableOSCache"}
_QBT_IOTYPE = {0: "Default (mmap on 64-bit)", 1: "MemoryMappedFiles", 2: "POSIX-compliant"}

def _qbt_preferences():
    """Live preferences dict, or None if unreachable/unauthorised."""
    host = (os.environ.get("QBITTORRENT_HOST")
            or _env_file_value("QBITTORRENT_HOST") or "http://localhost:8080").rstrip("/")
    user = os.environ.get("QBITTORRENT_USER") or _env_file_value("QBITTORRENT_USER")
    pw = os.environ.get("QBITTORRENT_PASS") or _env_file_value("QBITTORRENT_PASS")
    if not user or not pw:
        return None
    import http.cookiejar, urllib.error, urllib.parse, urllib.request
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        opener.open(urllib.request.Request(
            f"{host}/api/v2/auth/login",
            data=urllib.parse.urlencode({"username": user, "password": pw}).encode(),
            headers={"Referer": host}), timeout=10).read()
        body = opener.open(urllib.request.Request(
            f"{host}/api/v2/app/preferences", headers={"Referer": host}),
            timeout=10).read()
        return json.loads(body)
    except (OSError, ValueError):
        return None

_prefs = _qbt_preferences()
if _prefs is None:
    warn("qbit-oscache-disabled", "ADR-0007",
         "qBittorrent's API is unreachable from here (expected in CI); could "
         "not verify disk_io_read_mode/disk_io_write_mode = DisableOSCache. "
         "`make verify-runtime` checks this on the host.")
else:
    _bad = {k: _QBT_MODES.get(_prefs.get(k), _prefs.get(k))
            for k in ("disk_io_read_mode", "disk_io_write_mode")
            if _prefs.get(k) != 0}
    if _bad:
        fail("qbit-oscache-disabled", "ADR-0007",
             f"live qBittorrent session has {_bad}, not DisableOSCache. This is "
             "the mitigation for the 21.1GB cgroup peak; mem_limit 4g is only "
             "the backstop and will now be doing all the work. Fix in the WebUI "
             "(Tools > Options > Advanced > 'Disk IO read/write mode' = "
             "'Disable OS cache') -- qbittorrent persists it itself; editing "
             "qBittorrent.conf under a running qbittorrent gets overwritten.")
    else:
        ok("qbit-oscache-disabled", "both modes DisableOSCache (live session)")

    # Separate check, deliberately a warning: DiskIOType is the setting that
    # decides whether libtorrent mmaps at all. DisableOSCache mitigates the
    # symptom; DiskIOType=POSIX removes the mechanism. Flipping it has a
    # throughput cost, so this reports rather than enforces until Step 5's
    # measurement says otherwise. ADR-0007.
    _t = _prefs.get("disk_io_type")
    if _t in (0, 1):
        warn("qbit-diskio-type", "ADR-0007",
             f"disk_io_type={_t} ({_QBT_IOTYPE.get(_t)}). libtorrent still mmaps "
             "torrent data, so the kernel keeps those pages in the cgroup's page "
             "cache even with DisableOSCache set -- that is the actual source of "
             "the 21.1GB accounting. The POSIX-compliant backend removes the "
             "mechanism rather than mitigating it. Measure before switching "
             "(plan Task 4 Step 5); this is a warning, not a rule.")
    else:
        ok("qbit-diskio-type", _QBT_IOTYPE.get(_t, _t))
```

- [ ] **Step 3: Prove it fails — against a fixture, never the live file or the live session**

The negative test injects a fake preferences endpoint rather than touching anything real:

```bash
cd ~/nas
python3 - <<'PY' &
import http.server, json, threading
class H(http.server.BaseHTTPRequestHandler):
    def _send(self, b):
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self): self._send(b"Ok.")
    def do_GET(self): self._send(json.dumps(
        {"disk_io_read_mode":1,"disk_io_write_mode":0,"disk_io_type":0}).encode())
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", 18080), H).serve_forever()
PY
FAKE=$!
sleep 1
QBITTORRENT_HOST=http://127.0.0.1:18080 QBITTORRENT_USER=x QBITTORRENT_PASS=y \
  ./scripts/check-invariants.sh | grep -E "qbit-oscache|qbit-diskio"
kill $FAKE
```

Expected: `FAIL qbit-oscache-disabled [ADR-0007] live qBittorrent session has {'disk_io_read_mode': 'EnableOSCache'} …` and the `qbit-diskio-type` warning.

> Nothing in this step touches `${CONFIG_DIRECTORY}/qbittorrent/`, the running container, or the real API. That matters: autoheal restarts qbittorrent within ~30 s of it going unhealthy, so a test that mutated the live config could have it reloaded out from under you mid-test.

- [ ] **Step 4: Prove it passes against the real session**

```bash
./scripts/check-invariants.sh -v | grep -E "qbit-oscache|qbit-diskio"
```

Expected: `ok   qbit-oscache-disabled   both modes DisableOSCache (live session)` plus the `qbit-diskio-type` warning describing `Default (mmap on 64-bit)`.

- [ ] **Step 5: Measure whether `DiskIOType=POSIX` is worth taking (do not flip it blind)**

The warning above states a hypothesis. Test it rather than acting on it. First confirm the enum, because the numeric mapping is read off qBittorrent's UI order and is the one thing here not verified from a first-party doc:

```bash
# Set it in the WebUI (Tools > Options > Advanced > 'Disk IO type'), then read
# back what the API and the conf call it. Do NOT guess the integer.
U=$(sed -n 's/^QBITTORRENT_USER=//p' .env); P=$(sed -n 's/^QBITTORRENT_PASS=//p' .env)
curl -s -c /tmp/qc -d "username=$U&password=$P" http://127.0.0.1:8080/api/v2/auth/login >/dev/null
curl -s -b /tmp/qc http://127.0.0.1:8080/api/v2/app/preferences \
  | python3 -c "import sys,json;print('disk_io_type =',json.load(sys.stdin)['disk_io_type'])"
rm -f /tmp/qc
```

Then take a 24 h before/after on the number that actually matters — the cgroup's page-cache footprint, not RSS:

```bash
cat /sys/fs/cgroup/system.slice/docker-$(docker inspect -f '{{.Id}}' qbittorrent).scope/memory.stat \
  | grep -E '^(file|anon|file_mapped) '
docker stats --no-stream qbittorrent
```

Record both readings and the observed throughput in `docs/decisions/0007-*.md`. Flip the setting only if the mmap pages are in fact the bulk of the footprint **and** throughput is unaffected; otherwise write down that `DisableOSCache` plus `mem_limit` is the accepted position and why. Either outcome closes the question; leaving it unmeasured does not.

- [ ] **Step 6: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): assert qBittorrent's disk-IO settings from the live session

mem_limit 4g is the backstop; the OS-cache bypass is the fix, and it lives in
qBittorrent's own config where the WebUI can silently revert it.

Reads GET /api/v2/app/preferences rather than qBittorrent.conf: the file is
written at shutdown and read at startup, so it can disagree with the running
session in both directions. The negative test runs against a fixture HTTP
server -- the first draft sed -i'd the LIVE conf and argued that was safe
because the process was never restarted, which ignores that autoheal restarts
qbittorrent within ~30s of it going unhealthy.

Adds a second, deliberately-warning check on disk_io_type. It is 0 (Default)
today, so libtorrent still mmaps and the kernel still accounts those pages to
the cgroup -- DisableOSCache is the mitigation, not the removal. Flipping it
has a throughput cost, so the plan measures before deciding.

Warns rather than fails when the API is unreachable, so CI stays green."
```

---

### Task 5: Run the invariant checker in CI, and add a runtime counterpart that actually runs

Every assertion added above only protects the server if it runs without a human choosing to run it. The pre-commit hook is bypassable with `--no-verify`; CI is not.

But note what CI *cannot* see. Roughly a third of these assertions degrade to warnings without `.env`, the live containers, or the qBittorrent API — so CI is necessary and not sufficient. `make verify-runtime` is where the rest of the enforcement lives, and rev 1 left it as a target nothing ever invoked. Step 5 puts it on a cron with the same ntfy alerting as every other job.

Three bugs in rev 1's `verify-runtime`, all found by review:

- `grep -iv healthy | grep -i unhealthy` can never match: the first grep deletes every line containing `healthy`, which includes `unhealthy`. Verified — it prints `none` unconditionally.
- `docker compose ps` without `-a` hides exited containers, which is precisely the ADR-0006 failure being checked for.
- `strtonum()`/`and()` are gawk extensions. `awk` on this host *is* gawk 5.3.2 (`readlink -f $(command -v awk)` → `/usr/bin/gawk`), so it works today — but the target should say `gawk` rather than depend on that, and it should check `CapEff`/`CapBnd` too, not only `CapPrm`.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`
- Modify: crontab (recorded in `README.md`)

**Interfaces:**
- Consumes: `scripts/check-invariants.sh` (exit `0` pass / `1` violation / `2` fatal), and CI's existing "Create minimal .env" step.
- Produces: a CI job named `invariants`, added to the `needs:` list of the existing summary gate; a `verify-runtime` Make target.

- [ ] **Step 1: Confirm the checker passes with only CI's fabricated `.env`**

CI builds `.env` from `.env.example`. Reproduce that exactly, in a temp dir, so the live `.env` is untouched:

```bash
cd ~/nas
mkdir -p /tmp/ci-sim && rm -rf /tmp/ci-sim/* && git archive HEAD | tar -x -C /tmp/ci-sim
cd /tmp/ci-sim
awk 'NF && $0 !~ /CLOUDFLARE_API_TOKEN/ {print}' .env.example > .env
./scripts/check-invariants.sh; echo "exit=$?"
cd ~/nas
```

Expected: exit `0`. Warnings are expected here and are not failures: `qbit-oscache-disabled` (no API), `qbit-diskio-type`, `cap-drop-all` (Phase C closes it), and possibly `jellyfin-mounts-frozen` if `.env.example` carries no real `SHARE_DIRECTORY`. If it exits `1`, fix the assertion before wiring CI — a red CI on first run teaches people to ignore CI.

- [ ] **Step 2: Add the CI job**

In `.github/workflows/ci.yml`, insert after the `compose-validate` job:

```yaml
  invariants:
    name: Compose Invariants
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Create minimal .env for compose validation
        run: |
          awk 'NF && $0 !~ /CLOUDFLARE_API_TOKEN/ {print}' .env.example > .env
          echo 'CLOUDFLARE_API_TOKEN=dummy_token' >> .env
      # Asserts the incident-derived rules in docs/decisions/. Each failure
      # names the ADR that explains why the rule exists. Checks that need the
      # live host (qBittorrent's API, container state) degrade to warnings
      # here and are enforced by `make verify-runtime` on the box instead.
      - name: Assert stack invariants
        run: ./scripts/check-invariants.sh --verbose
```

- [ ] **Step 3: Add it to the summary gate**

In the same file, change the summary job's dependency list and its check. Replace:

```yaml
    needs: [compose-validate, lint-js, python]
```

with:

```yaml
    needs: [compose-validate, invariants, lint-js, python]
```

and add `invariants` to the printed results and the failure condition alongside the existing three, matching the surrounding style:

```yaml
          echo "Invariants: ${{ needs.invariants.result }}"
```

```yaml
          if [ '${{ needs.compose-validate.result }}' != 'success' ] || [ '${{ needs.invariants.result }}' != 'success' ] || [ '${{ needs.lint-js.result }}' != 'success' ] || [ '${{ needs.python.result }}' != 'success' ]; then
```

- [ ] **Step 4: Add a working `make verify-runtime`**

This target validates *reality* — that the running containers match the rules, which config alone cannot prove. Append to `Makefile`, and add `verify-runtime` to the `.PHONY` list:

```makefile
verify-runtime: ## Assert the RUNNING containers match the invariants (not just the config)
	@rc=0; \
	echo "==> every compose service has a container (ADR-0006)"; \
	for s in $$(docker compose config --services); do \
	  docker inspect "$$s" >/dev/null 2>&1 || { echo "    !!! $$s: NO CONTAINER"; rc=1; }; \
	done; [ $$rc -eq 0 ] && echo "    all present"; \
	echo "==> no stray compose.override.yaml"; \
	if [ -e compose.override.yaml ] || [ -e compose.override.yml ]; then \
	  echo "    !!! compose.override.yaml present. It is gitignored AND auto-loaded,"; \
	  echo "        so git status will not show it and every compose command is affected."; \
	  rc=1; \
	else echo "    none"; fi; \
	echo "==> qbittorrent holds CAP_KILL at runtime (ADR-0004)"; \
	docker exec qbittorrent sh -c 'grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status' \
	  | gawk '{ v=strtonum("0x" $$2); if (!and(v, 32)) { printf "    !!! %s lacks KILL\n", $$1; bad=1 } } \
	          END { if (bad) { print "        every stop becomes a 120s SIGKILL"; exit 1 } \
	                print "    ok: KILL in Prm/Eff/Bnd" }' || rc=1; \
	echo "==> swag nginx can signal its own workers (ADR-0021)"; \
	docker exec swag sh -c 'for p in /proc/[0-9]*; do \
	    case "$$(tr -d "\0" < $$p/cmdline 2>/dev/null)" in "nginx: worker process") \
	      kill -0 $$(basename $$p) 2>/dev/null && echo "    ok: worker signalable" \
	        || { echo "    !!! EPERM signalling nginx worker -- reload and graceful stop are broken"; exit 1; }; \
	      break;; esac; done' || rc=1; \
	echo "==> nothing but dockerproxy has the Docker socket (ADR-0013)"; \
	bad=$$(docker ps -q | xargs -r docker inspect \
	  --format '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' \
	  | grep docker.sock | grep -v '^/dockerproxy ' || true); \
	  if [ -z "$$bad" ]; then echo "    ok: dockerproxy only"; else echo "    !!! $$bad"; rc=1; fi; \
	echo "==> unhealthy or exited containers"; \
	u=$$(docker compose ps -a --format '{{.Name}}\t{{.Status}}' \
	     | grep -iE 'unhealthy|exited|restarting' || true); \
	  if [ -z "$$u" ]; then echo "    none"; else echo "$$u" | sed 's/^/    !!! /'; rc=1; fi; \
	exit $$rc
```

- [ ] **Step 5: Run it, and prove each arm can fail**

```bash
cd ~/nas && make verify-runtime; echo "exit=$?"
```

Expected: `all present`, `none`, `ok: KILL in Prm/Eff/Bnd`, `ok: worker signalable` *(this one fails until Task 9 — see below)*, `ok: dockerproxy only`, `none`, exit 0.

> **Expect the swag arm to FAIL here.** That is the point: it is the assertion-before-fix for Task 9. Verified today — `kill -0` against a uid-1000 nginx worker from root inside swag returns `Operation not permitted`, because swag's `cap_add` has no `KILL`. Leave it failing, land the target, and let Task 9 turn it green.

Now prove the unhealthy arm is not the dead code rev 1 shipped:

```bash
printf 'a\tUp 2 hours (healthy)\nb\tUp 3 hours (unhealthy)\n' | grep -iE 'unhealthy|exited|restarting'
printf 'a\tUp 2 hours (healthy)\nb\tUp 3 hours (unhealthy)\n' | grep -iv healthy | grep -i unhealthy || echo "   (rev 1's version: matches nothing, as expected)"
```

- [ ] **Step 6: Put `verify-runtime` on a cron so it is not a target nobody runs**

CI cannot see the live host, so nothing was enforcing the runtime half. Wire it like every other job — `cd` first, `cron_job.py` for the ntfy alert on failure or staleness:

```bash
( crontab -l; cat <<'CRON'
15 6 * * * /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/cron_job.py --name verify-runtime --max-age-min 1560 -- make verify-runtime >> logs/verify_runtime.log 2>&1"
CRON
) | crontab -
crontab -l | grep verify-runtime
```

Note the leading `cd /home/tom/nas &&`, matching all 14 existing entries. Cron's working directory is `$HOME`, so without it `scripts/`, `logs/` and the compose files all resolve somewhere else.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/ci.yml Makefile
git commit -m "ci: gate on the invariant checker, and add a verify-runtime that works

The pre-commit hook is bypassable with --no-verify; CI is not. But CI cannot
see the live host -- about a third of these assertions degrade to warnings
without .env, the containers, or qBittorrent's API -- so the runtime half is
now a cron'd make target reporting to ntfy, not a target nobody invokes.

Three bugs in the first draft of verify-runtime, all found in review:
- 'grep -iv healthy | grep -i unhealthy' can never match, because the first
  grep deletes every line containing 'healthy' -- including 'unhealthy'. It
  printed 'none' unconditionally;
- docker compose ps without -a hides exited containers, which is exactly the
  ADR-0006 failure being checked for;
- strtonum()/and() are gawk extensions; the target now says gawk explicitly and
  checks CapEff/CapBnd as well as CapPrm.

Also asserts no stray compose.override.yaml (gitignored AND auto-loaded, so
invisible to git status), and that swag's nginx can signal its own workers --
which fails today and is fixed in Task 9."
```

---

### Task 6: Quote the schedule, correct the bazarr non-gap, and prune the dead env vars

Two Known-gaps entries are wrong, and a wrong gap list is worse than none — it trains people to skim it. And `.env` has a line that breaks every script that sources it.

- [ ] **Step 0: Quote `WATCHTOWER_SCHEDULE` — do this before any other task sources `.env`**

Line 25 is `WATCHTOWER_SCHEDULE=0 0 4 * * *`, unquoted. Compose reads `.env` as a key-value file and is unaffected, but every `set -a; . ./.env; set +a` in this plan — and in `offsite_backup.sh`, where it is silenced with `2>/dev/null` — sets `WATCHTOWER_SCHEDULE=0`, then tries to execute `0 4 * * *` as a command and glob-expands `*` against the working directory.

```bash
cd ~/nas
cp .env /tmp/env-before-quote.bak
sed -n '25p' .env
sed -i "s|^WATCHTOWER_SCHEDULE=\(.*\)$|WATCHTOWER_SCHEDULE='\1'|" .env
sed -n '25p' .env
# it must now source cleanly, and compose must be unchanged
( set -a; . ./.env; set +a; echo "schedule=[$WATCHTOWER_SCHEDULE]" )
docker compose config > /tmp/after-quote.yml
diff /tmp/plan-baseline.yml /tmp/after-quote.yml && echo "MODEL UNCHANGED"
```

Expected: `schedule=[0 0 4 * * *]` with no `command not found: 0`, and an empty model diff. Then fix the same line in `.env.example` so a fresh clone does not reintroduce it.

- [ ] **Step 1: Prove the bazarr "gap" is not a gap**

```bash
cd ~/nas
KR=$(sed -n 's/^API_KEY_RADARR=//p' .env)
curl -s -H "X-Api-Key: $KR" http://127.0.0.1:7878/api/v3/movie \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('radarr movies:',len(d),'with files:',sum(1 for m in d if m.get('hasFile')))"
```

Expected: `radarr movies: 37 with files: 24`. Bazarr tracks only movies that have a file — you cannot subtitle a file you do not have — so 24 is correct and there is nothing to fix.

- [ ] **Step 2: Remove that entry from `README.md`**

Delete this bullet from the Known gaps list:

```markdown
- **Bazarr knows about 24 movies against Radarr's 37.** A sync question, not a
  path question — all 24 resolve correctly.
```

Replace it with nothing. Instead, add the explanation to the troubleshooting section's "Subtitles aren't appearing" block, after the expected-output line, so the number stops looking alarming:

```markdown
Bazarr tracks only movies that **have a file** — 24 of Radarr's 37 — so a lower
count there is correct, not a sync fault.
```

- [ ] **Step 3: Prove each variable is dead before deleting it**

`docker compose config` being unchanged proves nothing about `scripts/`, which read `.env` directly. Grep for each name across everything that is not documentation:

```bash
cd ~/nas
for v in API_KEY_BAZARR API_KEY_LAZYLIBRARIAN DEEPL_API_KEY JELLYFIN_CACHE_DIRECTORY \
         OVERSEERR_PUBLISHED_URL PLEX_IDENTIFIER PLEX_SERVER_NAME \
         RYM_SCRAPE_ENABLED RYM_SCRAPE_DELAY_MIN RYM_SCRAPE_DELAY_MAX PLEX_TOKEN; do
  hits=$(git grep -l "$v" -- ':!*.md' ':!.env.example' | tr '\n' ' ')
  printf '%-28s %s\n' "$v" "${hits:-<dead>}"
done
```

Expected: all ten dead, and `PLEX_TOKEN  scripts/enable_bazarr_plex.py`.

> Review flagged `API_KEY_BAZARR` as probably live, on the reasoning that `enable_bazarr_plex.py` must authenticate to Bazarr. It does not: it edits `${CONFIG_DIRECTORY}/bazarr/config/config.yaml` on disk and prints "Restart Bazarr". It never speaks to Bazarr's HTTP API, so it needs no key. Verified above — run the grep anyway rather than trusting this paragraph.

- [ ] **Step 4: Delete the ten dead variables from `.env`**

`.env` is gitignored and unrecoverable, so back it up first:

```bash
cd ~/nas
cp .env /tmp/env-before-prune.bak
for v in API_KEY_BAZARR API_KEY_LAZYLIBRARIAN DEEPL_API_KEY JELLYFIN_CACHE_DIRECTORY \
         OVERSEERR_PUBLISHED_URL PLEX_IDENTIFIER PLEX_SERVER_NAME \
         RYM_SCRAPE_ENABLED RYM_SCRAPE_DELAY_MIN RYM_SCRAPE_DELAY_MAX; do
  sed -i "/^${v}=/d" .env
done
diff <(grep -oE '^[A-Z_]+=' /tmp/env-before-prune.bak | sort) \
     <(grep -oE '^[A-Z_]+=' .env | sort)
```

Expected: exactly those ten lines removed. Delete whichever of them are also in `.env.example`; some were only ever there, so the two lists will not shrink by the same amount — that is fine, and it is what rev 1's "nine dead / ten lines removed" arithmetic was confused about.

- [ ] **Step 5: Prove nothing broke**

```bash
make lint && make check
docker compose config > /tmp/after-prune.yml
diff /tmp/after-quote.yml /tmp/after-prune.yml && echo "MODEL UNCHANGED"
. .venv/bin/activate && python scripts/test_scripts.py 2>&1 | tail -3
```

Expected: model unchanged, 19/19 smoke tests pass. If the model changed, one of those variables was live after all — restore from `/tmp/env-before-prune.bak` and investigate.

- [ ] **Step 6: Reconcile the documentation**

In `.env.example`, change the "NOT USED ANYWHERE" comment block's framing from "were present in `.env`" to "have been removed from `.env`", remove any remaining `RYM_SCRAPE_*` / `PLEX_*` template entries higher up the file, and quote `WATCHTOWER_SCHEDULE` as in Step 0. Then delete the same ten names from `AGENTS.md`'s env list.

- [ ] **Step 7: Commit**

```bash
git add README.md .env.example AGENTS.md
git commit -m "docs: quote WATCHTOWER_SCHEDULE, drop the bazarr non-gap, prune ten dead env vars

WATCHTOWER_SCHEDULE='0 0 4 * * *' was unquoted, so any script doing
'set -a; . ./.env' set it to 0, then tried to run '0 4 * * *' as a command and
glob-expanded * against the cwd. offsite_backup.sh silenced that with
2>/dev/null. Compose was unaffected; shells were not.

Radarr has 37 movies but only 24 with files, and bazarr only tracks movies that
have a file -- so 24 was always correct. Listing it as a gap trained people to
skim the gap list. Explanation moved to the troubleshooting block instead.

Ten variables removed after git grep proved each dead across scripts/ and
compose/, not just after checking the compose model -- scripts read .env
directly, so an unchanged model proves nothing about them. PLEX_TOKEN stays:
scripts/enable_bazarr_plex.py still reads it."
```

---

**Phase A gate.** Before Phase B:

```bash
cd ~/nas
make lint && make check
make verify-runtime; echo "verify-runtime exit=$? (1 is EXPECTED here -- swag CAP_KILL, fixed in Task 9)"
docker compose config > /tmp/phase-a.yml
diff /tmp/plan-baseline.yml /tmp/phase-a.yml && echo "PHASE A WAS A PURE NO-OP ON THE MODEL"
```

The diff **must** be empty. Phase A adds assertions and corrects docs; it changes no service. `verify-runtime` is expected to exit 1 on the swag arm only — every other arm must pass.

---
# Phase B — Remove the capability to fail

---

### Task 7: Replace Watchtower, and take away its ability to delete containers

This is the highest-value task in the plan. Rules 3 and the `WATCHTOWER_TIMEOUT` rule, one ADR (0006), one 13-hour outage, one 7-day outage, and six "deliberately unlabeled" annotations all exist to defend against **one** thing: Watchtower's non-atomic stop→remove→create abandoning a container.

Two facts, both verified today, decide the shape of this task.

**The upstream is archived.** `containrrr/watchtower` was archived on 2025-12-17 with a banner reading "This project is no longer maintained". It will not get another fix.

**It is nevertheless very much alive on this host, and it wrote to a container this morning.** Review predicted Watchtower could not talk to Docker Engine 29 at all, so the failure mode was already unreachable. That is not what the box says:

```
$ docker version --format '{{.Server.Version}} (API {{.Server.APIVersion}})'
29.7.2 (API 1.55)
$ docker logs watchtower --tail 6
04:01:40 Found new ghcr.io/autobrr/qui:latest image (4b2a3832d0e1)
04:01:52 Stopping /qui (e75731f79100) with SIGTERM
04:01:54 Creating /qui
04:01:54 Session done  Failed=0 Scanned=17 Updated=1
$ docker inspect watchtower --format '{{.State.Status}} restarts={{.RestartCount}}'
running restarts=0
```

No API-version error, no restart loop — it goes through `dockerproxy` rather than the socket, and the compose file pins `DOCKER_API_VERSION=1.40`. So the 13-hour-outage capability is live right now, and this task is not academic.

That leaves two problems and this task fixes both: **swap the image for the maintained fork**, and **demote it to monitor-only** so the recreate capability is gone regardless of who maintains it.

> **Behaviour change, stated plainly:** the 16 labelled services stop auto-updating. That is the point. Updates become deliberate (`make pull` + `make up`). If unattended patching matters more than never losing a container, do the fork swap and skip the monitor-only half — but the incident record in ADR-0006 argues hard against that.

> **What monitor-only does *not* do**, per Watchtower's own documentation, and what rev 1 got wrong:
> - It **still pulls.** *"Due to Docker API limitations the latest image will still be pulled from the registry. The HEAD digest checks allows watchtower to skip pulling when there are no changes, but to know what has changed it will still do a pull whenever the repository digest doesn't match the local image digest."*
> - `--cleanup` becomes **inert.** *"watchtower will remove the old image after restarting a container with a new image"* — no restart, no removal. Pulled images therefore accumulate.
>
> This host already prunes weekly (`0 3 * * 0 … docker image prune -f`), so the accumulation is bounded at 7 days rather than unbounded. Step 7 asserts that cron still exists rather than assuming it.

**Files:**
- Create: `docs/decisions/0020-watchtower-replaced-and-demoted.md`
- Modify: `compose/infra.yaml`
- Modify: `scripts/check-invariants.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: `env_of()`, `fail()`, `ok()` in the checker.
- Produces: check id `watchtower-monitor-only`. Task 8's Jellyfin pin is independent of this.

- [ ] **Step 1: Verify the fork is a drop-in before committing to it**

```bash
docker pull nickfedor/watchtower:1.22.0
docker run --rm --entrypoint /watchtower nickfedor/watchtower:1.22.0 --help 2>&1 \
  | grep -E '^\s+-[emc],|--monitor-only|--label-enable|--cleanup|--notification-url'
```

Expected — the three flags this stack depends on, with the original label namespace intact:

```
  -c, --cleanup          Remove previously used images after updating
  -e, --label-enable     Watch containers where the com.centurylinklabs.watchtower.enable label is true
  -m, --monitor-only     Will only monitor for new images, not update the containers
```

`com.centurylinklabs.watchtower.enable` is what makes this a drop-in: the 16 labels and 10 deliberate omissions in the compose files keep their meaning untouched. The fork also **autonegotiates the Docker API version** and is tested against v1.43+, so the current `DOCKER_API_VERSION=1.40` pin is removed in Step 4 — the server is API 1.55.

If the fork's `--help` does not show all three, stop: do the monitor-only half against `containrrr/watchtower:latest` and open the replacement as its own task.

- [ ] **Step 2: Write the assertion first — it must fail now**

Append to `scripts/check-invariants.sh` as section 15:

```python
# ==========================================================================
# 15. Watchtower is monitor-only, and not the archived upstream
# ==========================================================================
# Its stop->remove->create is not atomic: when the remove fails it logs
# Failed=1 and moves on WITHOUT creating a replacement, leaving no container
# at all. qbittorrent, 2026-09-01, 13h. monitor-only removes the capability
# rather than defending against it.
#
# The image check is separate and softer: containrrr/watchtower was archived
# 2025-12-17 ("no longer maintained"). It still works against Docker 29 here,
# so this is a maintenance risk rather than a live fault. ADR-0020.
wt = services.get("watchtower", {})
wt_env = env_of("watchtower")
if str(wt_env.get("WATCHTOWER_MONITOR_ONLY", "")).lower() != "true":
    fail("watchtower-monitor-only", "ADR-0020",
         "WATCHTOWER_MONITOR_ONLY is not 'true'. Watchtower can then stop and "
         "remove containers, and its recreate is not atomic -- a failed remove "
         "leaves NO container, which restart: unless-stopped cannot fix and "
         "autoheal cannot heal. It cost 13h of qbittorrent on 2026-09-01 and "
         "7 days on 2026-08-19. Recreating belongs to `docker compose up -d`.")
else:
    ok("watchtower-monitor-only", "notify-only; compose does the recreating")

if wt.get("image", "").startswith("containrrr/watchtower"):
    warn("watchtower-image-maintained", "ADR-0020",
         "containrrr/watchtower was archived 2025-12-17 and will get no further "
         "fixes. It still works against Docker 29.7.2 here, so this is a "
         "maintenance risk, not a live fault -- but the next Docker API change "
         "has nobody to answer it. nickfedor/watchtower is a drop-in: same "
         "com.centurylinklabs.watchtower.* labels, same WATCHTOWER_* env vars.")
elif wt.get("image"):
    ok("watchtower-image-maintained", wt["image"])
```

```bash
./scripts/check-invariants.sh | grep -E "watchtower-monitor-only|watchtower-image"
```

Expected: `FAIL watchtower-monitor-only …` and `WARN watchtower-image-maintained …`.

- [ ] **Step 3: Write ADR-0020**

Create `docs/decisions/0020-watchtower-replaced-and-demoted.md`:

```markdown
# ADR-0020 — Watchtower is replaced and demoted to monitor-only

**Date:** 2026-09-02
**Status:** accepted
**Amends:** ADR-0006, which defended against this failure per-service

## Decision

Two changes to one service.

1. **Image:** `containrrr/watchtower:latest` → `nickfedor/watchtower:1.22.0`.
   The upstream was archived on 2025-12-17 with the banner "This project is no
   longer maintained". The fork keeps the `com.centurylinklabs.watchtower.*`
   label namespace and the `WATCHTOWER_*` environment contract, so every label
   and every deliberate omission in this repo keeps its meaning.
2. **Mode:** `WATCHTOWER_MONITOR_ONLY=true`. Watchtower detects and reports new
   images. It never stops, removes or creates a container again.

`DOCKER_API_VERSION=1.40` is dropped with the swap: the fork autonegotiates and
this host serves API 1.55.

## Why the demotion, and why it is not already moot

ADR-0006 handled the non-atomic recreate by opting six services out, one at a
time, each with a comment explaining itself. That defence has three problems:

1. It is opt-*out*, so the dangerous default applies to every new service until
   someone remembers.
2. It only protects the services someone thought to protect. The 16 still
   labelled were all exposed to the same failure — qbittorrent was simply the
   one that drew the short straw.
3. It required a second rule (`WATCHTOWER_TIMEOUT` ≥ the longest
   `stop_grace_period`) that exists solely because Watchtower does the stopping.

It would be convenient if the archive had made this self-solving — if Watchtower
could no longer talk to Docker 29 and the capability were already gone. It has
not. On 2026-09-02 at 04:01, running against Engine 29.7.2 / API 1.55, it
stopped, removed and recreated `qui` with `Failed=0 Scanned=17 Updated=1`, from
a container with `restarts=0`. The capability is live. Monitor-only removes it.

## What is lost

Unattended patching. 16 services no longer update themselves at 04:00.

That is an acceptable trade at this scale: one host, one operator, a stack where
losing qBittorrent for 13 hours or 7 days both actually happened, and where
`docker compose up -d` is a one-command recreate that does not abandon
containers.

## What monitor-only does not do

Per Watchtower's documentation, monitor-only **still pulls** images — HEAD
digest checks let it skip a pull when nothing changed, but it pulls whenever the
repository digest differs from the local one. And `--cleanup` only removes an
old image *after* a container is restarted with a new one, so under monitor-only
it never fires and pulled images accumulate.

`WATCHTOWER_CLEANUP=true` is therefore left set but inert. The accumulation is
bounded by the existing weekly `docker image prune -f` cron (Sundays 03:00),
which is now load-bearing rather than housekeeping; `make verify-runtime` does
not check crontab, so ADR-0012's job table is where that is recorded.

## The notification blind spot this creates

Watchtower reports updates for the tag a container was started from. Two
consequences worth stating rather than discovering:

- A **pinned** tag never reports an update. After Task 8, `jellyfin` and
  `qbittorrent` are both pinned.
- An **unlabelled** container is never checked. `jellyfin` and `qbittorrent` are
  both unlabelled (ADR-0006).

So the two services whose updates most want to be chosen deliberately get no
notification at all — and relabelling them would not help, because the pin
silences them anyway. Closing that needs a *version-aware* watcher (DIUN,
WUD, or Renovate against the compose files), which is a different tool and its
own decision. Recorded as an open gap in `README.md` rather than half-solved
here.

## What stays true from ADR-0006

The per-service labels stay as they are. They now express "do not even tell me
about updates to this" rather than "do not touch this", which is still
meaningful for the four locally-built images Watchtower cannot pull anyway.
`scripts/stack_watchdog.py` remains the detector for a missing container,
because Watchtower is no longer the only thing that could cause one.

`WATCHTOWER_TIMEOUT` also stays. It is inert under monitor-only, but leaving it
costs nothing and it must be correct again the moment anyone reverts this.
```

- [ ] **Step 4: Make the change**

In `compose/infra.yaml`, in the `watchtower` service: replace the `image:` line, delete the `DOCKER_API_VERSION` line, and add the monitor-only flag.

```yaml
    # INVARIANT: NOT containrrr/watchtower -- archived 2025-12-17, "no longer
    # maintained". This fork keeps the com.centurylinklabs.watchtower.* labels
    # and the WATCHTOWER_* env contract, so every label in this repo and every
    # deliberate omission keeps its meaning. ADR-0020.
    image: nickfedor/watchtower:1.22.0
    container_name: watchtower
    environment:
      - DOCKER_HOST=tcp://dockerproxy:2375
      # DOCKER_API_VERSION removed with the fork swap: it autonegotiates, and
      # this host serves API 1.55. ADR-0020.
      - WATCHTOWER_SCHEDULE=${WATCHTOWER_SCHEDULE:-0 0 4 * * *}
      - WATCHTOWER_LABEL_ENABLE=true
      # Inert under monitor-only (cleanup only removes an image AFTER a
      # container is restarted with a new one), left set so it is correct again
      # if anyone reverts. The weekly `docker image prune -f` cron is what
      # actually bounds the images monitor-only keeps pulling. ADR-0020.
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_INCLUDE_RESTARTING=true
      - WATCHTOWER_ROLLING_RESTART=false
      - WATCHTOWER_REMOVE_VOLUMES=false
      # INVARIANT: monitor-only. Watchtower detects updates and reports them;
      # it must never stop, remove or create a container. Its recreate is not
      # atomic and a failed remove leaves NO container at all -- 13h of
      # qbittorrent on 2026-09-01, 7 days on 2026-08-19. It did recreate `qui`
      # at 04:01 on 2026-09-02, so this capability is live, not historical.
      # Recreating is `docker compose up -d`'s job. ADR-0020.
      - WATCHTOWER_MONITOR_ONLY=true
```

Leave `WATCHTOWER_TIMEOUT` and the three `WATCHTOWER_NOTIFICATION_*` lines exactly as they are.

- [ ] **Step 5: Verify the model changed in exactly three ways**

```bash
cd ~/nas
make lint
docker compose config > /tmp/after-t7.yml
diff /tmp/phase-a.yml /tmp/after-t7.yml
```

Expected: `image:` changed, `DOCKER_API_VERSION: "1.40"` removed, `WATCHTOWER_MONITOR_ONLY: "true"` added. Nothing else, and nothing under any other service.

- [ ] **Step 6: Apply and confirm it took effect**

```bash
docker compose up -d watchtower
sleep 20
docker logs watchtower --tail 40
```

Expected in the startup banner: the fork's version line, `Only checking containers using enable label`, and an explicit statement that it is in monitor-only mode. Confirm the negative directly — there must be no `Stopping /` or `Creating /` line on any subsequent run.

```bash
./scripts/check-invariants.sh | grep -E "watchtower|FAIL" || echo "no failures"
make verify-runtime; echo "exit=$? (1 still expected: swag CAP_KILL, Task 9)"
```

- [ ] **Step 7: Confirm the pull-accumulation backstop actually exists**

Monitor-only keeps pulling, and `--cleanup` no longer fires. Verify the weekly prune is real rather than assumed:

```bash
crontab -l | grep -c 'docker image prune'      # expect: 1
crontab -l | grep 'docker image prune'
docker images --format '{{.Repository}}:{{.Tag}}' | wc -l
docker system df
```

Record the image count here and re-check it after a week. If it climbs past roughly double, the weekly prune is not keeping up and the cron needs to move to daily.

- [ ] **Step 8: Update `README.md`**

In "Updating services", retitle "Scheduled (16 services)" to "Detected, not applied (16 services)" and replace its body:

```markdown
Watchtower runs on `WATCHTOWER_SCHEDULE` (default `0 0 4 * * *`), checks the 16
labelled containers for newer images, and **reports** what it finds to ntfy. It
is `WATCHTOWER_MONITOR_ONLY=true` and never stops, removes or creates anything:
its recreate is not atomic and a failed remove leaves no container at all. That
capability is gone rather than defended against. The image is
`nickfedor/watchtower`, a maintained drop-in fork — `containrrr/watchtower` was
archived in December 2025. → [ADR-0020](docs/decisions/0020-watchtower-replaced-and-demoted.md)

Applying an update is `make pull && make up`, or one of the watched
single-service targets below.

Monitor-only still *pulls* the images it checks, and `WATCHTOWER_CLEANUP` only
removes an image after a container is restarted with it — so pulled images
accumulate until the weekly `docker image prune -f` cron (Sundays 03:00).
```

In the invariants table, replace the `qbittorrent`/`jellyfin` Watchtower-label row with:

```markdown
| Watchtower is `MONITOR_ONLY` | Its recreate is not atomic; a failed remove leaves **no container at all** (13 h, then 7 days). The capability is removed, not defended | [0020](docs/decisions/0020-watchtower-replaced-and-demoted.md) |
```

And add one Known-gaps entry, because Task 8 creates it:

```markdown
- **No update notification for `jellyfin` or `qbittorrent`.** Both are pinned
  and both are unlabelled, and Watchtower reports against the tag a container
  was started from — so a pinned tag is silent even if relabelled. Closing this
  needs a version-aware watcher (DIUN / WUD / Renovate against the compose
  files), not a Watchtower setting. → [ADR-0020](docs/decisions/0020-watchtower-replaced-and-demoted.md)
```

- [ ] **Step 9: Commit**

```bash
git add compose/infra.yaml scripts/check-invariants.sh docs/decisions/0020-watchtower-replaced-and-demoted.md README.md
git commit -m "feat(watchtower): maintained fork, and monitor-only

Two problems, one service.

containrrr/watchtower was archived 2025-12-17 ('no longer maintained').
nickfedor/watchtower:1.22.0 is a drop-in: same com.centurylinklabs.watchtower.*
labels, same WATCHTOWER_* env contract, so all 16 labels and all 10 deliberate
omissions keep their meaning. DOCKER_API_VERSION=1.40 dropped with it -- the
fork autonegotiates and this host serves API 1.55.

WATCHTOWER_MONITOR_ONLY=true removes the recreate capability. Its non-atomic
stop->remove->create is the single worst failure mode in this repo's history:
13h of qbittorrent on 2026-09-01, 7 days on 2026-08-19. ADR-0006 defended
against it by opting six services out one at a time, which is opt-OUT and left
the other 16 exposed to the identical failure.

This is not already moot: at 04:01 on 2026-09-02, against Engine 29.7.2 / API
1.55, Watchtower stopped, removed and recreated qui with Failed=0 Scanned=17
Updated=1. The capability was live.

Cost, stated in ADR-0020 rather than discovered: 16 services stop self-updating;
monitor-only still pulls, so images accumulate until the weekly prune cron; and
jellyfin/qbittorrent get no update notification at all, since a pinned tag is
silent whether labelled or not. That last one is logged as an open gap needing
a version-aware watcher.

Assertion written before the change and verified failing, then passing."
```

---
### Task 8: Pin Jellyfin's image tag

With Watchtower now unable to write, an unpinned tag is a smaller risk — but `make pull-jellyfin` still takes whatever `:latest` is that day, which is how you find out about a regression during a film rather than on a Tuesday morning.

Read Task 7's ADR-0020 §"The notification blind spot this creates" before starting. Pinning here is what makes `jellyfin` permanently silent to Watchtower, and that is a deliberate trade, not an oversight: it is already unlabelled, so it was silent anyway, and relabelling would not change it because a pinned tag never reports an update. The gap entry Task 7 Step 8 adds is the honest record of that.

**Files:**
- Modify: `compose/media-serve.yaml`
- Modify: `scripts/check-invariants.sh`
- Modify: `README.md`, `docs/decisions/0006-watchtower-opt-outs.md`

- [ ] **Step 1: Find the exact tag currently running**

```bash
docker image inspect lscr.io/linuxserver/jellyfin:latest \
  --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
docker inspect jellyfin --format 'running: {{.Config.Image}}'
```

Expected at time of writing: `10.11.11ubu2604-ls47`. **Use whatever this command actually prints**, not the value quoted here.

- [ ] **Step 2: Pin it**

In `compose/media-serve.yaml`, replace the jellyfin `image:` line, keeping the comment style used by qbittorrent:

```yaml
    # INVARIANT: tag is PINNED. An update must be chosen, never taken by
    # surprise -- a Jellyfin regression surfaces mid-playback. Bump this
    # deliberately with `make pull-jellyfin`, which waits for healthy.
    # Consequence, accepted: pinned + unlabelled means Watchtower will never
    # report a Jellyfin update. ADR-0006, ADR-0020.
    image: lscr.io/linuxserver/jellyfin:10.11.11ubu2604-ls47
```

- [ ] **Step 3: Verify the pin resolves to the same image already running**

This is the no-regression check that matters — the pinned tag must be the digest that is running right now, so applying it is a no-op:

```bash
cd ~/nas
RUNNING=$(docker inspect jellyfin --format '{{.Image}}')
PINNED=$(docker image inspect "$(docker compose config --format json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['services']['jellyfin']['image'])")" \
  --format '{{.Id}}')
[ "$RUNNING" = "$PINNED" ] && echo "SAME IMAGE — applying is a no-op" || echo "DIFFERENT — investigate before up -d"
```

Expected: `SAME IMAGE — applying is a no-op`.

- [ ] **Step 4: Add the assertion**

Append as section 16. Note the tag split: `rpartition(":")` on a registry that carries a port (`registry.local:5000/img`) returns the port, and a digest-pinned image (`img@sha256:…`) would silently "pass" a check whose whole subject is pinning. Split on the last path segment instead.

```python
# ==========================================================================
# 16. Jellyfin's tag is pinned too
# ==========================================================================
# Not for watchtower's sake any more (ADR-0020), but because a Jellyfin
# regression is discovered mid-playback. An update must be chosen. ADR-0006.
def _image_tag(image):
    """Tag of an image ref, or '' if untagged. Handles a registry port and a
    digest pin, neither of which a bare rpartition(':') survives."""
    if "@" in image:
        return "@" + image.rsplit("@", 1)[1]     # digest pin counts as pinned
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else ""

jf_tag = _image_tag(services.get("jellyfin", {}).get("image", ""))
if not jf_tag or jf_tag == "latest":
    fail("jellyfin-tag-pinned", "ADR-0006",
         f"jellyfin image must be a pinned tag, not '{jf_tag or '<none>'}'. "
         "Bump it deliberately with `make pull-jellyfin`.")
else:
    ok("jellyfin-tag-pinned", jf_tag)
```

Prove the parser on the cases that broke the naive version:

```bash
python3 -c "
def _image_tag(image):
    if '@' in image: return '@' + image.rsplit('@',1)[1]
    last = image.rsplit('/',1)[-1]
    return last.rsplit(':',1)[1] if ':' in last else ''
for i in ('lscr.io/linuxserver/jellyfin:10.11.11ubu2604-ls47',
          'lscr.io/linuxserver/jellyfin:latest',
          'registry.local:5000/jellyfin',
          'lscr.io/linuxserver/jellyfin@sha256:abc123'):
    print(f'  {i:55} -> {_image_tag(i)!r}')"
```

Expected: the pinned tag; `'latest'`; `''` (the registry port is not mistaken for a tag); and `'@sha256:abc123'`.

- [ ] **Step 5: Verify, then apply**

```bash
make lint && ./scripts/check-invariants.sh | grep -E "jellyfin-tag|FAIL" || echo "clean"
docker compose up -d jellyfin
make verify-runtime; echo "exit=$? (1 still expected: swag CAP_KILL, Task 9)"
```

Because Step 3 proved the pinned tag is the running image, `up -d` should report jellyfin as already up to date and **not** recreate it. If it does recreate, wait for `healthy` before continuing.

- [ ] **Step 6: Update the docs**

In `README.md`, delete this Known-gaps bullet:

```markdown
- **Jellyfin's tag is not pinned** while qBittorrent's is, so `make
  pull-jellyfin` takes whatever `:latest` is that day. A choice, not an
  oversight. → [ADR-0006](docs/decisions/0006-watchtower-opt-outs.md)
```

In the service reference table, change jellyfin's image cell from `lscr.io/…/jellyfin` to `lscr.io/…/jellyfin **pinned**`. In `docs/decisions/0006-watchtower-opt-outs.md`, replace the paragraph beginning "Note that jellyfin's tag is **not** pinned" with a note that it was pinned on 2026-09-02, that both slow services now behave identically, and that pinning plus the existing label omission is what makes them invisible to Watchtower — cross-referencing ADR-0020's blind-spot section rather than restating it.

- [ ] **Step 7: Commit**

```bash
git add compose/media-serve.yaml scripts/check-invariants.sh README.md docs/decisions/0006-watchtower-opt-outs.md
git commit -m "feat(jellyfin): pin the image tag

An unpinned tag means make pull-jellyfin takes whatever :latest is that day, so
a regression surfaces mid-playback rather than on a Tuesday. Pinned to the
digest already running, verified identical before applying, so this is a no-op
on the live container. Closes the last unpinned slow-to-stop service.

The tag parser splits on the last path segment rather than rpartition(':'),
which mistook a registry port for a tag and passed a digest-pinned image in a
check whose entire subject is pinning.

Accepted consequence, recorded in ADR-0020 and README's gap list: pinned plus
unlabelled means Watchtower will never report a Jellyfin update, and relabelling
would not help because a pinned tag is silent either way."
```

---

### Task 9: Give SWAG's nginx back `CAP_KILL`

**This task was not in rev 1.** It came out of reviewing Task 11's capability set, and it is a live bug rather than a hardening improvement.

SWAG runs `cap_drop: ALL` with five capabilities added, and `KILL` is not among them:

```bash
$ docker compose config --format json | python3 -c "import sys,json;print(json.load(sys.stdin)['services']['swag']['cap_add'])"
['NET_BIND_SERVICE', 'CHOWN', 'SETUID', 'SETGID', 'DAC_OVERRIDE']
$ docker exec swag sh -c 'grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status'
CapPrm: 00000000000004c3      # bits 0,1,6,7,10 -- bit 5 (KILL) clear
```

nginx's master process runs as root and its workers run as a different uid (`user abc;` → uid 1000). Linux permits `kill()` across a uid boundary only for a process holding `CAP_KILL`; being root is not sufficient. Probed directly on the live container, using signal 0 so nothing is actually delivered:

```bash
$ docker exec swag sh -c '... kill -0 <worker pid>'
sh: can't kill pid 873: Operation not permitted
```

So the nginx master **cannot signal its own workers**. The consequences are exactly the two operations this stack does to SWAG:

- **`nginx -s reload`** — the master accepts the SIGHUP (root→root), forks new workers with the new config, then tries to tell the old ones to shut down gracefully. That signal is refused. Old workers keep serving the *old* config indefinitely, and the worker count grows on every reload.
- **`docker compose stop swag`** — the master cannot signal workers to quit, so they are still running when the grace period expires and Docker SIGKILLs the container.

Neither has bitten yet because nothing has reloaded SWAG since it last started (16 workers, `nproc` = 16, no surplus). **Task 10 reloads it.** So this comes first.

This is the same class of bug as ADR-0004: qbittorrent's s6 supervisor runs as root, signals `qbittorrent-nox` running as `abc`, and `cap_drop: ALL` had removed the `CAP_KILL` that made it possible. Same shape, different service, found by reading rather than by an outage.

> **Corollary for Task 12.** "Measure an existing working nginx rather than trusting a blog recipe" was the right instinct, and SWAG is the obvious thing to measure — but SWAG carries this bug, so copying its set would have propagated it. Measure the *mechanism*, not a neighbour's config.

**Files:**
- Create: `docs/decisions/0021-nginx-cap-kill.md`
- Modify: `compose/infra.yaml`
- Modify: `scripts/check-invariants.sh`

- [ ] **Step 1: Reproduce the fault before fixing it**

```bash
cd ~/nas
docker exec swag sh -c '
  for p in /proc/[0-9]*; do
    case "$(tr -d "\0" < $p/cmdline 2>/dev/null)" in
      "nginx: worker process")
        w=$(basename $p)
        echo "master uid: $(awk "/^Uid:/{print \$2}" /proc/$(pgrep -f "nginx: master" | head -1)/status)"
        echo "worker $w uid: $(awk "/^Uid:/{print \$2}" $p/status)"
        kill -0 $w && echo "SIGNAL PERMITTED" || echo "EPERM -- master cannot signal its own workers"
        break;; esac; done'
docker exec swag sh -c 'grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status'
```

Expected: master uid `0`, worker uid `1000`, `EPERM`, and `CapPrm: 00000000000004c3` with bit 5 clear. `make verify-runtime` has been failing on this arm since Task 5 — that failure is this.

- [ ] **Step 2: Write the assertion**

Append as section 17:

```python
# ==========================================================================
# 17. An nginx whose master and workers differ in uid needs CAP_KILL
# ==========================================================================
# nginx's master runs as root and forks workers as another uid (`user abc;` in
# swag, `user www-data;` in playlist-generator). Linux allows kill() across a
# uid boundary only with CAP_KILL -- being root is not enough. Without it:
#   * `nginx -s reload` forks new workers but cannot retire the old ones, so
#     stale-config workers accumulate on every reload;
#   * a graceful stop cannot signal workers, so the container is SIGKILLed at
#     the end of its grace period.
# Same mechanism as ADR-0004 (s6 signalling qbittorrent-nox across a uid
# boundary). Verified by probe, not inference: `kill -0 <worker>` from root
# inside swag returned EPERM on 2026-09-02. ADR-0021.
NGINX_SERVICES = ("swag", "playlist-generator")
for _svc in NGINX_SERVICES:
    _s = services.get(_svc)
    if not _s:
        continue
    _drop = [str(c).upper() for c in (_s.get("cap_drop") or [])]
    _add = [str(c).upper().removeprefix("CAP_") for c in (_s.get("cap_add") or [])]
    if "ALL" not in _drop:
        continue        # cap-drop-all check owns this case
    if "KILL" not in _add:
        fail("nginx-cap-kill", "ADR-0021",
             f"{_svc} drops ALL capabilities and does not add KILL, but its "
             "nginx master runs as root while its workers run as another uid. "
             "kill() across a uid boundary needs CAP_KILL; root alone is "
             "refused with EPERM. `nginx -s reload` will fork new workers and "
             "leak the old ones, and a graceful stop will end in SIGKILL.")
    else:
        ok("nginx-cap-kill", f"{_svc} holds KILL")
```

```bash
./scripts/check-invariants.sh | grep nginx-cap-kill
```

Expected: `FAIL nginx-cap-kill [ADR-0021] swag drops ALL capabilities and does not add KILL …`. `playlist-generator` does not fail yet — it does not drop ALL until Task 12, and the `cap-drop-all` check owns it until then.

- [ ] **Step 3: Write ADR-0021**

Create `docs/decisions/0021-nginx-cap-kill.md` recording: the mechanism (uid boundary + `CAP_KILL`, not root), the probe output above as evidence, the two operations it breaks, why it had not surfaced (SWAG had not been reloaded since start), that Task 10 was about to trigger it, and the cross-reference to ADR-0004 as the same mechanism in a different service. State explicitly that `KILL` is the *minimum* addition — nothing else about SWAG's set changes.

- [ ] **Step 4: Make the change**

In `compose/infra.yaml`, in the `swag` service's `cap_add:` block. The existing comment says the list is declared locally "so the on-disk capability order is unchanged" — so append rather than re-sort:

```yaml
    # NET_BIND_SERVICE first, then the LSIO set -- declared locally rather than
    # inherited from svc-lsio so the on-disk capability order is unchanged.
    cap_add:
      - NET_BIND_SERVICE
      - CHOWN
      - SETUID
      - SETGID
      - DAC_OVERRIDE
      # INVARIANT: nginx's master is root and its workers run as uid 1000
      # (`user abc;`). kill() across a uid boundary needs CAP_KILL -- root is
      # not enough. Without it `nginx -s reload` leaks stale-config workers and
      # a graceful stop ends in SIGKILL. Verified by probe: kill -0 against a
      # worker returned EPERM on 2026-09-02. ADR-0021.
      - KILL
```

- [ ] **Step 5: Verify the model, then recreate — this is the reverse proxy**

Recreating `swag` drops every subdomain for a few seconds. Do it deliberately, and have the rollback ready.

```bash
cd ~/nas
make lint
docker compose config > /tmp/after-t9.yml
diff /tmp/after-t7.yml /tmp/after-t9.yml
```

Expected: one added line, `- KILL` under `swag.cap_add`. Nothing else.

```bash
docker compose up -d swag
for i in $(seq 1 24); do
  s=$(docker inspect -f '{{.State.Health.Status}}' swag 2>/dev/null)
  echo "t=$((i*5))s $s"; [ "$s" = healthy ] && break; sleep 5
done
```

**Rollback if it does not come back healthy:**

```bash
git checkout compose/infra.yaml && docker compose up -d swag
```

- [ ] **Step 6: Prove the fault is gone, and that a reload now behaves**

```bash
# the capability is present
docker exec swag sh -c 'grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status'
# the probe that returned EPERM now succeeds
docker exec swag sh -c '
  for p in /proc/[0-9]*; do
    case "$(tr -d "\0" < $p/cmdline 2>/dev/null)" in
      "nginx: worker process") kill -0 $(basename $p) && echo "SIGNAL PERMITTED"; break;; esac; done'
# and a reload retires its old workers instead of leaking them
before=$(docker exec swag sh -c 'ls /proc | grep -c "^[0-9]"')
docker exec swag nginx -t && docker exec swag nginx -s reload
sleep 5
docker exec swag sh -c 'for p in /proc/[0-9]*; do
  case "$(tr -d "\0" < $p/cmdline 2>/dev/null)" in "nginx: worker process") echo x;; esac; done | wc -l'
nproc
docker exec swag sh -c 'grep -iE "kill\(|operation not permitted" /config/log/nginx/error.log | tail -5' || echo "no kill failures logged"
```

Expected: `CapPrm` gains bit 5 (`…4e3` where it read `…4c3`), `SIGNAL PERMITTED`, a post-reload worker count equal to `nproc` rather than double it, and no `kill(...) failed` lines.

```bash
curl -sk -o /dev/null -w 'apex: %{http_code}\n' https://4eva.me/
curl -sk -o /dev/null -w 'jellyfin: %{http_code}\n' https://jellyfin.4eva.me/
make verify-runtime; echo "exit=$? (0 expected now)"
```

`verify-runtime` should pass every arm for the first time.

- [ ] **Step 7: Commit**

```bash
git add compose/infra.yaml scripts/check-invariants.sh docs/decisions/0021-nginx-cap-kill.md
git commit -m "fix(swag): grant CAP_KILL — nginx could not signal its own workers

swag drops ALL capabilities and adds five, none of them KILL. Its nginx master
runs as root and forks workers as uid 1000 (\`user abc;\`), and Linux permits
kill() across a uid boundary only with CAP_KILL -- root is not sufficient.
Probed on the live container with signal 0: 'can't kill pid 873: Operation not
permitted'.

Two consequences. \`nginx -s reload\` forks new workers and cannot retire the
old ones, so stale-config workers accumulate on every reload. And a graceful
stop cannot signal workers, so the container is SIGKILLed at the end of its
grace period.

It had not bitten because nothing had reloaded swag since it last started --
worker count still equalled nproc. The next task reloads it, which is how this
was found.

Same mechanism as ADR-0004: s6 signalling qbittorrent-nox across a uid boundary
with CAP_KILL dropped. Found by reading rather than by an outage.

Assertion written first and verified failing; the reload test now shows the
worker count returning to nproc instead of doubling."
```

---

### Task 10: Give Lingarr the proxy-conf it never had — and track it

`lingarr` carries `swag=enable` but SWAG ships no lingarr sample, so `lingarr.4eva.me` resolves to nothing. The label has been a lie since the service was added.

Rev 1 wrote the conf into gitignored `.docker-config/` and committed only a README change, leaving the actual fix enforced by nothing but a nightly backup — which is precisely the "remembered rule" this plan exists to eliminate. This version puts the conf **in the repo** and bind-mounts it, following the pattern SWAG already uses for the root site conf:

```yaml
- ../webapps/4eva-rootpage/4eva-rootpage.root.conf:/config/nginx/site-confs/root.conf:ro
```

and adds an assertion that every `swag=enable` service has a conf, so the next one cannot be a lie either.

Do Task 9 first. This task reloads nginx, which is the operation Task 9 repairs.

**Files:**
- Create: `swag/proxy-confs/lingarr.subdomain.conf` (tracked)
- Modify: `compose/infra.yaml` (mount it)
- Modify: `scripts/check-invariants.sh`
- Modify: `README.md`

- [ ] **Step 1: Confirm there is no sample to enable, and no conf in place**

```bash
docker exec swag sh -c 'ls /defaults/proxy-confs/ | grep -i lingarr' || echo "none shipped — hand-write it"
docker exec swag sh -c 'ls /config/nginx/proxy-confs/ | grep -i lingarr' || echo "none enabled"
curl -sk -o /dev/null -w 'lingarr before: %{http_code}\n' https://lingarr.4eva.me/
docker compose config --format json | python3 -c "
import sys,json;d=json.load(sys.stdin)['services']['lingarr']
print('ports:',d.get('ports'));print('container port: 8080 (ASPNETCORE_URLS=http://+:8080)')"
```

Both listings expected to report none. Note lingarr listens on **8080** inside the container and is published to `127.0.0.1:9876` on the host — SWAG reaches it over `nas-network` on 8080, not 9876.

- [ ] **Step 2: Write the conf into the repo**

Model it on an enabled one rather than inventing the shape:

```bash
cd ~/nas
mkdir -p swag/proxy-confs
docker exec swag cat /config/nginx/proxy-confs/qui.subdomain.conf > /tmp/qui-reference.conf
sed -n '1,40p' /tmp/qui-reference.conf
```

Then create `swag/proxy-confs/lingarr.subdomain.conf`:

```nginx
## Hand-written: SWAG ships no lingarr sample. lingarr carried swag=enable with
## no proxy-conf, so lingarr.${PUBLIC_DOMAIN} resolved to nothing.
##
## Tracked in this repo and bind-mounted read-only into SWAG, following the
## pattern already used for 4eva-rootpage.root.conf -- a conf that lives only in
## gitignored .docker-config/ is a rule enforced by a nightly backup.
##
## lingarr listens on 8080 inside the container (ASPNETCORE_URLS=http://+:8080).
## Its host publish is 127.0.0.1:9876; SWAG does not use that, it reaches the
## container over nas-network.

server {
    listen 443 ssl;
    listen [::]:443 ssl;

    server_name lingarr.*;

    include /config/nginx/ssl.conf;

    client_max_body_size 0;

    location / {
        include /config/nginx/proxy.conf;
        include /config/nginx/resolver.conf;
        set $upstream_app lingarr;
        set $upstream_port 8080;
        set $upstream_proto http;
        proxy_pass $upstream_proto://$upstream_app:$upstream_port;
    }
}
```

- [ ] **Step 3: Mount it**

In `compose/infra.yaml`, add to `swag`'s `volumes:` — paths are relative to `compose/`, hence `../`:

```yaml
      # Tracked proxy-conf: SWAG ships no lingarr sample, and a conf that lives
      # only in gitignored .docker-config/ is enforced by nothing but the
      # nightly backup. Same pattern as root.conf above. ADR-0020 companion.
      - ../swag/proxy-confs/lingarr.subdomain.conf:/config/nginx/proxy-confs/lingarr.subdomain.conf:ro
```

- [ ] **Step 4: Assert that a `swag=enable` label is never again a lie**

Append as section 18. This is the general form of the rule, so the next service added with the label gets it for free:

```python
# ==========================================================================
# 18. Every swag=enable service has a proxy-conf
# ==========================================================================
# `swag=enable` is what publishes a service on its subdomain -- but only if a
# matching *.subdomain.conf exists. lingarr carried the label with no conf from
# the day it was added, so lingarr.${PUBLIC_DOMAIN} resolved to nothing and
# nothing said so. Confs are looked for in the tracked swag/proxy-confs/ first,
# then in the SWAG config dir; a conf in only the latter is a warning, because
# it is gitignored and survives only via the nightly backup.
_swag_dir = os.path.join(_conf, "swag", "nginx", "proxy-confs") if _conf else None
_missing, _untracked = [], []
for name, svc in sorted(services.items()):
    labels = svc.get("labels") or {}
    if isinstance(labels, list):
        labels = dict(x.split("=", 1) for x in labels if "=" in x)
    if labels.get("swag") != "enable" or name == "swag":
        continue
    _tracked = os.path.isfile(f"swag/proxy-confs/{name}.subdomain.conf")
    _live = bool(_swag_dir) and os.path.isfile(
        os.path.join(_swag_dir, f"{name}.subdomain.conf"))
    if _tracked:
        continue
    (_untracked if _live else _missing).append(name)
if _missing:
    fail("swag-labels-are-routed", "ADR-0020",
         f"{_missing} carry swag=enable with no proxy-conf anywhere. The label "
         "is what publishes a subdomain, and without a matching "
         "<service>.subdomain.conf it publishes nothing -- silently.")
elif _untracked:
    warn("swag-labels-are-routed", "ADR-0020",
         f"{_untracked} are routed only by a conf in the gitignored SWAG config "
         "dir. That rule is enforced by the nightly backup and nothing else. "
         "Move it to swag/proxy-confs/ and bind-mount it, as lingarr does.")
else:
    ok("swag-labels-are-routed", "every swag=enable service has a tracked conf")
```

Run it **before** creating the conf to see it fail, then after:

```bash
./scripts/check-invariants.sh | grep swag-labels-are-routed
```

Expected before Step 2: `FAIL … ['lingarr'] carry swag=enable with no proxy-conf anywhere.` Expected after: `WARN` naming the services routed only from `.docker-config/` — which is honest, and is its own follow-up rather than something to fix here.

- [ ] **Step 5: Validate the nginx config before reloading**

Never reload SWAG on an unvalidated conf — a syntax error takes down every service behind the proxy:

```bash
cd ~/nas
make lint
docker compose up -d swag     # picks up the new bind mount
for i in $(seq 1 24); do
  s=$(docker inspect -f '{{.State.Health.Status}}' swag 2>/dev/null)
  echo "t=$((i*5))s $s"; [ "$s" = healthy ] && break; sleep 5
done
docker exec swag sh -c 'ls -l /config/nginx/proxy-confs/lingarr.subdomain.conf'
docker exec swag nginx -t
```

Expected: `healthy`, the conf present read-only, and `syntax is ok` / `test is successful`. If `nginx -t` fails, fix the conf and re-test; **do not proceed to the reload.**

- [ ] **Step 6: Reload and verify — including that the reload itself behaved**

```bash
docker exec swag nginx -s reload
sleep 5
docker exec swag nginx -t && echo "config still valid after reload"
# Task 9's fix means old workers actually retire. Prove it here.
docker exec swag sh -c 'for p in /proc/[0-9]*; do
  case "$(tr -d "\0" < $p/cmdline 2>/dev/null)" in "nginx: worker process") echo x;; esac; done | wc -l'
nproc
docker exec swag sh -c 'grep -icE "kill\(.*failed" /config/log/nginx/error.log' || true
```

Expected: worker count equal to `nproc`, and zero `kill(...) failed` lines. A doubled worker count means Task 9 did not take.

```bash
curl -sk -o /dev/null -w 'lingarr: %{http_code}\n' https://lingarr.4eva.me/
curl -sk -o /dev/null -w 'apex still up: %{http_code}\n' https://4eva.me/
curl -sk -o /dev/null -w 'jellyfin still up: %{http_code}\n' https://jellyfin.4eva.me/
```

- [ ] **Step 7: Exercise the app, not just the index**

A `200`/`302` on `/` proves nginx has an upstream, not that Lingarr works through the proxy. Lingarr is an ASP.NET app with a live-progress channel, so exercise the path that a plain reverse-proxy conf most often breaks:

```bash
# the SPA's own API, through SWAG
curl -sk -o /dev/null -w 'api: %{http_code}\n' https://lingarr.4eva.me/api/setting/all
# the live-update channel: expect 101 Switching Protocols, or at minimum not 400/502
curl -sk -o /dev/null -w 'ws upgrade: %{http_code}\n' \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H "Sec-WebSocket-Key: $(head -c16 /dev/urandom | base64)" \
  https://lingarr.4eva.me/signalr
# and confirm it is really lingarr behind it, not a default page
curl -sk https://lingarr.4eva.me/ | grep -io 'lingarr' | head -1
```

If the upgrade returns `400` or `502`, SWAG's stock `proxy.conf` is not passing the `Upgrade`/`Connection` headers for this route — add them to the `location /` block and reload again. **Record whichever result you get in the commit message**; "it returned 200" is not evidence the service works.

- [ ] **Step 8: Update `README.md`**

Delete this Known-gaps bullet:

```markdown
- **`lingarr` carries `swag=enable` but has no proxy-conf**, so
  `lingarr.4eva.me` does not resolve to it. Reach it on `127.0.0.1:9876`.
```

Add `lingarr.` to the URL map's *arr row list, and note in the SWAG section that proxy-confs this repo owns live in `swag/proxy-confs/` and are bind-mounted, so they are version-controlled rather than backup-controlled.

- [ ] **Step 9: Commit**

```bash
git add swag/proxy-confs/lingarr.subdomain.conf compose/infra.yaml scripts/check-invariants.sh README.md
git commit -m "fix(swag): route lingarr, and track the conf instead of backing it up

SWAG ships no lingarr sample, so the swag=enable label had been a lie since the
service was added -- lingarr.4eva.me resolved to nothing.

The conf is committed to swag/proxy-confs/ and bind-mounted read-only, matching
the pattern already used for 4eva-rootpage.root.conf. Writing it into gitignored
.docker-config/ would have left the fix enforced by the nightly backup and
nothing else, which is the exact class of remembered rule this plan exists to
remove.

Also asserts the general rule: every swag=enable service must have a
proxy-conf, tracked ones pass and .docker-config-only ones warn. lingarr was
the instance; the next service with the label gets the check for free.

Verified with nginx -t before reloading, the apex and jellyfin re-checked after,
and the app exercised through its API and live-update channel rather than by a
200 on /."
```

---

**Phase B gate.**

```bash
cd ~/nas
make lint && make check
make verify-runtime; echo "exit=$? (0 expected — swag's arm is green after Task 9)"
docker compose config > /tmp/phase-b.yml
diff /tmp/phase-a.yml /tmp/phase-b.yml
```

Expected diff: exactly five changes and nothing else —

1. `watchtower`: `image:` → `nickfedor/watchtower:1.22.0` (Task 7)
2. `watchtower`: `DOCKER_API_VERSION` removed (Task 7)
3. `watchtower`: `WATCHTOWER_MONITOR_ONLY: "true"` added (Task 7)
4. `jellyfin`: `image:` tag pinned (Task 8)
5. `swag`: `KILL` added to `cap_add`, and the lingarr proxy-conf bind mount added (Tasks 9, 10)

---
# Phase C — Close the capability waivers

ADR-0018's two waivers are the only services in the stack without `cap_drop: ALL`. The reason they were left alone was sound: guessing a database's capability set and finding out at restart is how a database fails to come back. So this phase measures first.

**Measured facts to build on** (gathered 2026-09-02, re-verify in Step 1 of each task):

- `playlist-generator` runs everything as **root** with Docker's full default set (`CapPrm: 00000000a80425fb`). `pid 1` is `tini`; the entrypoint runs `htpasswd -cb` to write `/etc/nginx/.htpasswd`, starts `uvicorn` on `127.0.0.1:8000` (as root, pid 9), then `nginx`. `nginx.conf` declares `user www-data;`, so the master (pid 78, uid 0) spawns **16 workers as uid 33**, and it binds `0.0.0.0:80`.
- `playlist-generator-db` — `pid 1` already runs with **`CapEff: 0000000000000000`**, because the postgres entrypoint drops to uid 999 via `gosu`. Its `PGDATA` is already owned `999:1000`. So the *running* database needs nothing; only the *entrypoint* does.

> **Read Task 9 before Task 12.** `playlist-generator`'s nginx has the same root-master / different-uid-worker shape as SWAG, so it needs `KILL` for the same reason. The temptation is to copy SWAG's five-capability set as a known-good reference — do not: SWAG was missing `KILL` too, and copying it would have propagated the bug into a second service. Measure the mechanism.

---

### Task 11: Harden `playlist-generator-db`

Do the database first only because its need is the narrowest and already measured; take a dump anyway.

**Files:**
- Modify: `webapps/playlist-generator/compose.yaml`

**Interfaces:**
- Consumes: the `svc-base` fragment it already extends.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Re-measure, and take a dump**

```bash
cd ~/nas
docker exec playlist-generator-db sh -c 'grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status; ls -ldn /var/lib/postgresql/data; id'
docker exec playlist-generator-db pg_dump -U playlist -d playlist_generator \
  | gzip > /tmp/plgen-db-$(date +%Y%m%d-%H%M).sql.gz
ls -lh /tmp/plgen-db-*.sql.gz
```

Expected: `CapEff: 0000000000000000` (the running server needs no capabilities) and a non-empty dump. **Do not continue without the dump.**

- [ ] **Step 2: Apply the narrow set**

In `webapps/playlist-generator/compose.yaml`, under `playlist-generator-db`, replace the `# KNOWN GAP` comment with a real capability declaration, directly after the `extends:` block:

```yaml
    # Measured 2026-09-02, not guessed: pid 1 already runs with
    # CapEff 0000000000000000 because the entrypoint gosu's down to uid 999, so
    # the running server needs nothing. These four are what the ENTRYPOINT needs
    # before that hand-off: chown PGDATA (CHOWN), fix modes on files it does not
    # own (FOWNER), and gosu's setuid/setgid pair. No KILL: postgres's
    # postmaster and its backends all run as the SAME uid (999), so signalling
    # them crosses no uid boundary -- unlike nginx (ADR-0021). ADR-0018.
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - FOWNER
      - SETUID
      - SETGID
```

- [ ] **Step 3: Verify the no-KILL claim before trusting it**

The whole point of Task 9 is that "root can signal anything" is false. Check that postgres does not have the uid boundary that would make `KILL` necessary:

```bash
docker exec playlist-generator-db sh -c '
  for p in /proc/[0-9]*; do
    c=$(tr -d "\0" < $p/cmdline 2>/dev/null)
    case "$c" in *postgres*) echo "$(basename $p) uid=$(awk "/^Uid:/{print \$2}" $p/status) $c";; esac
  done' | head
```

Expected: every postgres process on the same uid (999). If any run as a *different* uid from the postmaster, add `KILL` and say why in the comment.

- [ ] **Step 4: Verify the model, then recreate**

```bash
make lint
docker compose config --format json | python3 -c "
import sys,json;d=json.load(sys.stdin)['services']['playlist-generator-db']
print('cap_drop:',d.get('cap_drop'),'cap_add:',d.get('cap_add'))"
docker compose up -d playlist-generator-db
```

- [ ] **Step 5: Prove the database actually came back**

```bash
for i in $(seq 1 24); do
  s=$(docker inspect -f '{{.State.Health.Status}}' playlist-generator-db 2>/dev/null)
  echo "t=$((i*5))s $s"; [ "$s" = healthy ] && break; sleep 5
done
docker exec playlist-generator-db psql -U playlist -d playlist_generator -c 'select count(*) from pg_tables;'
docker logs playlist-generator-db --tail 20 | grep -iE "ready to accept|error|fatal|permission denied"
```

Expected: `healthy`, a row count back, and `database system is ready to accept connections` with no `permission denied`.

**If it does not come back:** revert immediately, then investigate with the dump in hand.

```bash
git checkout webapps/playlist-generator/compose.yaml
docker compose up -d playlist-generator-db
```

- [ ] **Step 6: Prove the app still reaches it, and that stopping is still graceful**

```bash
for i in $(seq 1 24); do
  s=$(docker inspect -f '{{.State.Health.Status}}' playlist-generator 2>/dev/null)
  echo "t=$((i*5))s app=$s"; [ "$s" = healthy ] && break; sleep 5
done
docker exec playlist-generator curl -sf -o /dev/null -w 'health: %{http_code}\n' http://localhost/health
# a capability drop that breaks shutdown shows up as a stop that takes the full
# grace period rather than seconds
time docker compose stop playlist-generator-db
docker compose up -d playlist-generator-db
```

Expected: the app returns to `healthy`, `/health` answers `200`, and the stop completes in a couple of seconds rather than timing out. The app depends on the db with `condition: service_healthy`, so a db restart can leave the app sulking — if it does, `docker compose up -d playlist-generator`.

- [ ] **Step 7: Commit**

```bash
git add webapps/playlist-generator/compose.yaml
git commit -m "feat(playlist-generator-db): cap_drop ALL with a measured capability set

Closes half of ADR-0018. Measured rather than guessed: pid 1 already runs with
CapEff 0000000000000000 because the entrypoint gosu's down to uid 999, so the
running server needs nothing. The four granted are what the entrypoint needs
before that hand-off -- chown PGDATA, FOWNER for modes on files it does not own,
and gosu's setuid/setgid pair.

No KILL, and checked rather than assumed: every postgres process runs as the
same uid, so signalling crosses no uid boundary. That is the distinction
ADR-0021 turns on, and it is why nginx needs KILL and postgres does not.

pg_dump taken before the change; verified healthy, queryable, reachable from
the app, and still stopping in seconds rather than timing out."
```

---

### Task 12: Harden `playlist-generator`

**Files:**
- Modify: `webapps/playlist-generator/compose.yaml`
- Modify: `docs/decisions/0018-capability-gaps.md`
- Modify: `scripts/check-invariants.sh`
- Modify: `README.md`

- [ ] **Step 1: Re-measure what it needs, including the uid boundary**

```bash
cd ~/nas
docker exec playlist-generator sh -c '
  echo "--- pid1 ---"; cat /proc/1/comm
  echo "--- nginx user ---"; grep -E "^\s*user\s" /etc/nginx/nginx.conf
  echo "--- caps ---"; grep -E "^Cap(Prm|Eff|Bnd)" /proc/1/status
  echo "--- procs and uids ---"
  for p in /proc/[0-9]*; do
    c=$(tr -d "\0" < $p/cmdline 2>/dev/null)
    case "$c" in *nginx*|*uvicorn*) echo "  $(basename $p) uid=$(awk "/^Uid:/{print \$2}" $p/status) ${c:0:60}";; esac
  done | head -6
  echo "--- htpasswd owner ---"; ls -ln /etc/nginx/.htpasswd 2>/dev/null'
```

Expected: `tini`; `user www-data;`; full default caps; nginx master `uid=0` with workers `uid=33`; uvicorn `uid=0`.

That maps to exactly five needs:

| Capability | Why | Evidence |
|---|---|---|
| `NET_BIND_SERVICE` | nginx binds `0.0.0.0:80` | port < 1024 |
| `SETUID` + `SETGID` | master spawns `user www-data;` workers | master uid 0 → workers uid 33 |
| `CHOWN` | nginx's cache/log dirs at startup | entrypoint |
| **`KILL`** | master signals uid-33 workers on reload and on graceful stop | **the uid boundary above — ADR-0021** |

`DAC_OVERRIDE` is deliberately *not* granted: everything this container touches is root-owned, so root can already write it without bypassing permission checks.

> `KILL` is the one rev 1 missed, and it is not hypothetical: the identical shape in SWAG was probed and returned `EPERM` (Task 9). Without it this container starts healthy, passes every check in Steps 3 and 4, and then fails to stop gracefully — a 10 s wait ending in SIGKILL. That is why Step 5 times a stop instead of only checking a healthcheck.

- [ ] **Step 2: Apply the set**

In `webapps/playlist-generator/compose.yaml`, under `playlist-generator`, add directly after the `extends:` block, and delete the trailing `# KNOWN GAP` comment:

```yaml
    # Measured 2026-09-02, not guessed. nginx binds :80 (NET_BIND_SERVICE), its
    # master spawns `user www-data;` workers (SETUID/SETGID), and CHOWN covers
    # nginx's cache and log dirs at startup. uvicorn stays on 127.0.0.1:8000 and
    # needs nothing.
    #
    # KILL: the nginx master is uid 0 and its workers are uid 33. kill() across
    # a uid boundary needs CAP_KILL -- root is not enough. Without it the
    # container starts healthy and then cannot reload or stop gracefully, ending
    # every stop in a SIGKILL. Same mechanism as swag (ADR-0021) and as
    # qbittorrent's s6 supervisor (ADR-0004).
    #
    # DAC_OVERRIDE deliberately NOT granted: everything here is root-owned, so
    # there are no permission checks to bypass. ADR-0018.
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - SETUID
      - SETGID
      - KILL
      - NET_BIND_SERVICE
```

- [ ] **Step 3: Recreate and verify it starts**

```bash
make lint
docker compose up -d playlist-generator
for i in $(seq 1 24); do
  s=$(docker inspect -f '{{.State.Health.Status}}' playlist-generator 2>/dev/null)
  echo "t=$((i*5))s $s"; [ "$s" = healthy ] && break; sleep 5
done
docker logs playlist-generator --tail 30 | grep -iE "permission denied|operation not permitted|bind|error"
```

Expected: `healthy` within ~90 s (its `start_period`), and **no** `permission denied` / `operation not permitted`. If nginx cannot bind, the log says `bind() to 0.0.0.0:80 failed (13: Permission denied)` — that means `NET_BIND_SERVICE` did not apply; revert and re-measure.

- [ ] **Step 4: Prove the whole request path works, not just the healthcheck**

`/health` is the only unauthenticated path, so also exercise the authenticated one and the proxy:

```bash
docker exec playlist-generator curl -sf -o /dev/null -w 'health: %{http_code}\n' http://localhost/health
curl -sk -o /dev/null -w 'via swag: %{http_code}\n' https://playlist-generator.4eva.me/   # expect 401 (basic auth), not 502
docker exec playlist-generator python -c "
from app.db import engine
with engine.connect() as c: print('db reachable:', c.exec_driver_sql('select 1').scalar())" 2>/dev/null \
  || echo "(app-internal db check unavailable; the healthcheck covers it)"
```

Expected: `health: 200`, `via swag: 401` (an auth challenge proves nginx is serving — a `502` would mean the container is not reachable).

- [ ] **Step 5: Prove the two things a capability drop silently breaks**

Neither of these shows up in a healthcheck. Both are the failure Task 9 documents.

```bash
# (a) reload must retire old workers, not leak them
before=$(docker exec playlist-generator sh -c 'for p in /proc/[0-9]*; do
  case "$(tr -d "\0" < $p/cmdline 2>/dev/null)" in "nginx: worker process") echo x;; esac; done | wc -l')
docker exec playlist-generator nginx -s reload
sleep 5
after=$(docker exec playlist-generator sh -c 'for p in /proc/[0-9]*; do
  case "$(tr -d "\0" < $p/cmdline 2>/dev/null)" in "nginx: worker process") echo x;; esac; done | wc -l')
echo "workers before=$before after=$after (must be equal)"
docker logs playlist-generator --tail 20 | grep -iE "kill\(.*failed|operation not permitted" \
  || echo "no kill failures"

# (b) a graceful stop must finish in seconds, not at the grace-period timeout
time docker compose stop playlist-generator
docker compose up -d playlist-generator
```

Expected: `before == after`, no `kill(...) failed`, and a stop measured in single-digit seconds. A stop that takes the full grace period means `KILL` did not apply — that is the exact ADR-0004 signature.

**Rollback if anything fails:**

```bash
git checkout webapps/playlist-generator/compose.yaml
docker compose up -d playlist-generator
```

- [ ] **Step 6: Turn the waiver into an assertion**

In `scripts/check-invariants.sh`, empty the waiver dict so the generic `cap-drop-all` check now enforces both services:

```python
# KNOWN GAP, not an exemption: these do not drop capabilities. ADR-0018.
# Warned about on every run so it cannot quietly become the convention.
# 2026-09-02: both closed with measured capability sets -- the dict is
# deliberately left in place (empty) so the next gap has an obvious home.
CAP_DROP_WAIVER = {}
```

```bash
./scripts/check-invariants.sh
./scripts/check-invariants.sh | grep -E "nginx-cap-kill|cap-drop-all"
```

Expected: no `cap-drop-all` warnings, and `nginx-cap-kill` now reporting `ok` for **both** `swag` and `playlist-generator` — Task 9's assertion was written to cover this service too, and this is where its second arm goes green.

- [ ] **Step 7: Update ADR-0018 and `README.md`**

In `docs/decisions/0018-capability-gaps.md`, change `**Status:** **open — documented, not fixed**` to `**Status:** **closed 2026-09-02** (both sets measured, not guessed)`, and replace the "How to close it" section with the two measured sets and the evidence for each, including the three facts that made it safe: the db's pid 1 already ran at `CapEff 0`, the app's only privileged bind is nginx on `:80`, and the uid boundary that makes `KILL` necessary for one and unnecessary for the other. Cross-reference ADR-0021 rather than restating the mechanism.

In `README.md`, delete the `playlist-generator` bullet from Known gaps, and in the invariants table change the hardening-baseline row's note from mentioning waivers to `No exceptions`.

- [ ] **Step 8: Commit**

```bash
git add webapps/playlist-generator/compose.yaml scripts/check-invariants.sh docs/decisions/0018-capability-gaps.md README.md
git commit -m "feat(playlist-generator): cap_drop ALL, closing ADR-0018

Every service in the stack now drops ALL capabilities. The set was measured,
not guessed: nginx binds :80 (NET_BIND_SERVICE), its master spawns
'user www-data;' workers (SETUID/SETGID), and CHOWN covers nginx's cache and
log dirs. DAC_OVERRIDE deliberately withheld -- everything here is root-owned,
so there are no permission checks to bypass.

KILL is in the set because the nginx master is uid 0 and its workers are uid 33,
and kill() across a uid boundary needs CAP_KILL. Without it this container
starts healthy, passes every functional check, and then cannot reload or stop
gracefully -- which is how the identical gap in swag went unnoticed until it was
probed (ADR-0021). Verified here by timing a stop and counting workers across a
reload, not by a healthcheck.

CAP_DROP_WAIVER is now empty and the generic check enforces both services."
```

---

**Phase C gate.**

```bash
cd ~/nas
make lint && make check && make verify-runtime
./scripts/check-invariants.sh -v | grep -c '  ok'
docker compose config > /tmp/phase-c.yml
diff /tmp/phase-b.yml /tmp/phase-c.yml
```

Expected: no failures, no `cap-drop-all` warnings, and a diff containing only the two capability blocks. Assertion count should now be ~30.

---
# Phase D — Finish the hardlink migration

> **This is the only phase that can lose data.** ADR-0003 records what happened last time: `PUT /api/v1/artist/editor` with `moveFiles: false` emptied `TrackFiles` — **150,187 rows → 0** — and cost ~45 minutes of Lidarr activity to roll back. Read ADR-0003 in full before starting. If you are not prepared to restore a database, **stop after Phase C**; Lidarr copying instead of hardlinking is a disk-space inefficiency, not an outage.

## Why rev 1's method was abandoned

Rev 1 proposed doing this through the API: add `/data/music` as a second root folder, then `PUT /api/v1/artist/{id}?moveFiles=false` for each artist, verifying `trackFileCount` was unchanged after each, then delete the `/music` root folder. Review took that apart, and the database confirms it:

```
sqlite> select sql from sqlite_master where name='TrackFiles';
CREATE TABLE "TrackFiles" (... "Path" TEXT NOT NULL UNIQUE, ...)
sqlite> select count(*) from TrackFiles;                       150300
sqlite> select substr(Path,1,7), count(*) from TrackFiles group by 1;
/music/ | 150300
```

`TrackFiles` has no inode, no hash, no artist-relative path — **the absolute path is the only handle Lidarr has on a file.** `moveFiles=false` updates `Artists.Path` and rewrites nothing else, by design. So:

- **The canary proved nothing.** `plan_repath` sorted ascending by `track_files`, and **748 of the 3,336 artists have zero track files**. The first artist moved was therefore guaranteed to be a 0-file artist verifying `0 == 0`. Not unlikely — certain.
- **The verification was backwards.** An unchanged `trackFileCount` after a `moveFiles=false` repath is evidence the file rows were *not* rewritten, which is the failure, not the success.
- **The last step was the incident.** With 150,300 rows still pointing at `/music`, deleting the `/music` root folder makes the next refresh find no files under either root. That is the documented route to the same `150,187 → 0` outcome, reached by a different path.

Two safer options existed. The first — Artist Editor → "Yes, move the files", which renames on disk and updates the database in one operation — is **wrong here specifically**, because `/data` is a bind mount of `/mnt/drive` and `/music` is a bind mount of `/mnt/drive/music`. `/data/music/X` and `/music/X` are literally the same directory. A "move" would be a rename onto itself.

So this phase does the second: **an offline SQLite prefix rewrite.** It is a pure metadata change, because the bytes are already exactly where they need to be. Nothing on disk moves at all. That also disposes of two other rev-1 problems for free — there is no root-folder `POST` (so its five-field schema never comes up), and there is no root-folder `DELETE` (so the `#4446` path is never walked); the single existing `RootFolders` row is rewritten in place.

## The premise, verified before anything else

There is no point migrating if `/data/music` and `/data/downloads` cannot hardlink. Probed inside the live lidarr container:

```
$ docker exec lidarr sh -c 'ln /downloads/.probe /music/.probe'
ln: failed to create hard link: Cross-device link      # today's problem, ADR-0002
$ docker exec lidarr sh -c 'ln /data/downloads/.probe /data/music/.probe'
LINK OK                                                # the premise holds
```

Review anticipated `EXDEV` between `/data/music` and `/data/downloads`. That does not apply: `/data` is a **single** bind mount of `/mnt/drive`, so both are subdirectories inside one mount. `/music` and `/downloads` are two separate bind mounts, which is exactly why linking fails today.

## What gets rewritten, and what deliberately does not

Every column in the database holding a value that starts with `/music`, enumerated rather than assumed:

| Table.column | Rows | Rewrite? | Why |
|---|---:|---|---|
| `RootFolders.Path` | 1 | **yes** | the root itself |
| `Artists.Path` | 3,336 | **yes** | artist folders |
| `TrackFiles.Path` | 150,300 | **yes** | the only handle Lidarr has on a file |
| `MetadataFiles.RelativePath` | 14,958 | **yes** | see below |
| `History.SourceTitle` | 605,171 | no | historical audit text |
| `History.Data` (JSON `importedPath`) | 189,396 | no | historical |
| `DownloadHistory.Data` (JSON `destinationPath`) | 125,117 | no | historical |
| `Commands.Body` (JSON `folders`) | 3 | no | transient queue, regenerated |

**`MetadataFiles.RelativePath` is the trap.** Despite the name, 14,958 of its 43,299 rows hold **absolute** paths:

```
sqlite> select RelativePath from MetadataFiles where RelativePath like '/%' limit 1;
/music/Burzum/2001 - Once Emperor/album.nfo
sqlite> select case when RelativePath like '/%' then 'ABSOLUTE' else 'relative' end,
   ...>        count(*) from MetadataFiles group by 1;
ABSOLUTE | 14958
relative | 28341
```

A rewrite covering only `Artists` and `TrackFiles` — the obvious two — orphans every one of those `.nfo` files. The script must handle the mixed shape: rewrite the absolute rows, leave the relative ones alone.

The four "no" rows are left because they are audit history, not state Lidarr resolves against; rewriting 900k+ rows of historical text to make old log entries read prettier is risk with no return. **Record that choice in ADR-0003** so the leftover `/music` strings in History are not mistaken later for an incomplete migration.

> **The counts drift.** `lidarr_backlog_drip` and `lidarr_monitor_sweep` add artists every 15 minutes — `Artists` read 3,336 at 16:20 on 2026-09-02 and 3,345 an hour later. Treat every figure in this phase as the shape of the answer, not the answer; Task 14's rehearsal reports the live numbers and those are the ones to check against. Only two are structural: `RootFolders` is exactly 1, and `MetadataFiles` splits 14,958 absolute / 28,341 relative.

> **This rehearsal has been run once already**, on 2026-09-02, against a copy of the live database, using the module and tests exactly as written below. Result: 14/14 unit tests pass; `apply_rewrite` changed `1 / 3345 / 150300 / 14958` rows; row counts preserved across all four tables; zero rows left on `/music`; the 28,341 relative `MetadataFiles` rows untouched; the root folder read back `/data/music`; a second pass reported `0` eligible; and a random 1,000-path sample of rewritten `TrackFiles.Path` values was **1000/1000** present on disk. Re-run it anyway — the point is the state on the day, not the state on 2026-09-02.

---

### Task 13: Build the offline repath tool, with its logic under test

**Files:**
- Create: `scripts/lidarr_repath_db.py`
- Create: `scripts/tests/test_lidarr_repath_db.py`

**Interfaces:**
- Consumes: a path to a Lidarr SQLite database. **No API, no network, no Lidarr running.**
- Produces: `rewrite_path(old, old_root, new_root) -> str`, `REWRITE_TARGETS` (the table/column list above), `plan_rewrite(conn, old_root, new_root) -> list[dict]`, and `apply_rewrite(conn, plan, new_root) -> dict`. Task 14 calls the CLI, not these functions.

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_lidarr_repath_db.py`. The path-rewriting logic is pure and it is the part that corrupts data when wrong, so it gets covered first; the SQL is then tested against a synthetic database rather than the real one.

```python
"""Unit tests for the offline Lidarr repath.

The repath is destructive (ADR-0003: PUT /api/v1/artist/editor emptied 150,187
TrackFiles rows). These tests cover the part that decides what a path becomes,
and the part that decides which rows are eligible -- the two places a bug
writes a wrong path into 168,595 rows.
"""
import sqlite3

import pytest

from scripts.lidarr_repath_db import (
    REWRITE_TARGETS,
    apply_rewrite,
    plan_rewrite,
    rewrite_path,
)

# --- pure path logic ------------------------------------------------------

def test_rewrite_path_swaps_only_the_root_prefix():
    assert rewrite_path("/music/Aphex Twin", "/music", "/data/music") == "/data/music/Aphex Twin"


def test_rewrite_path_preserves_nested_structure():
    assert (
        rewrite_path("/music/Boards of Canada/Geogaddi/01.flac", "/music", "/data/music")
        == "/data/music/Boards of Canada/Geogaddi/01.flac"
    )


def test_rewrite_path_rewrites_the_bare_root_itself():
    """RootFolders.Path is exactly '/music', with no trailing segment."""
    assert rewrite_path("/music", "/music", "/data/music") == "/data/music"


def test_rewrite_path_is_idempotent_on_already_migrated_paths():
    """Running the tool twice must not produce /data/music/data/music/..."""
    assert rewrite_path("/data/music/Aphex Twin", "/music", "/data/music") == "/data/music/Aphex Twin"


def test_rewrite_path_refuses_a_path_outside_the_old_root():
    with pytest.raises(ValueError, match="not under"):
        rewrite_path("/downloads/thing", "/music", "/data/music")


def test_rewrite_path_does_not_match_a_sibling_with_a_shared_prefix():
    """/musicvideos must not be treated as living under /music."""
    with pytest.raises(ValueError, match="not under"):
        rewrite_path("/musicvideos/thing", "/music", "/data/music")


def test_rewrite_path_rejects_a_traversal_segment():
    with pytest.raises(ValueError, match="traversal"):
        rewrite_path("/music/../etc/passwd", "/music", "/data/music")


def test_rewrite_path_leaves_a_relative_path_alone():
    """MetadataFiles.RelativePath is 28,341 relative rows and 14,958 absolute
    ones in the live DB. The relative ones are not ours to touch."""
    with pytest.raises(ValueError, match="not under"):
        rewrite_path("artist.nfo", "/music", "/data/music")


# --- SQL, against a synthetic database ------------------------------------

def _db():
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE RootFolders (Id INTEGER PRIMARY KEY, Path TEXT NOT NULL);
        CREATE TABLE Artists (Id INTEGER PRIMARY KEY, Path TEXT NOT NULL);
        CREATE TABLE TrackFiles (Id INTEGER PRIMARY KEY, Path TEXT NOT NULL UNIQUE);
        CREATE TABLE MetadataFiles (Id INTEGER PRIMARY KEY, RelativePath TEXT NOT NULL);
        INSERT INTO RootFolders VALUES (1, '/music');
        INSERT INTO Artists VALUES (1, '/music/Burzum'), (2, '/music/Autechre');
        INSERT INTO TrackFiles VALUES (1, '/music/Burzum/01.mp3'), (2, '/music/Autechre/02.flac');
        INSERT INTO MetadataFiles VALUES
            (1, '/music/Burzum/album.nfo'), (2, 'artist.nfo'), (3, '/music/Autechre/album.nfo');
    """)
    return c


def test_rewrite_targets_covers_every_path_column_including_metadatafiles():
    """MetadataFiles.RelativePath holds 14,958 ABSOLUTE paths in the live DB
    despite its name. Omitting it orphans every .nfo."""
    assert ("MetadataFiles", "RelativePath") in REWRITE_TARGETS
    assert ("TrackFiles", "Path") in REWRITE_TARGETS
    assert ("Artists", "Path") in REWRITE_TARGETS
    assert ("RootFolders", "Path") in REWRITE_TARGETS


def test_plan_rewrite_counts_only_eligible_rows():
    plan = {p["table"]: p for p in plan_rewrite(_db(), "/music", "/data/music")}
    assert plan["RootFolders"]["eligible"] == 1
    assert plan["Artists"]["eligible"] == 2
    assert plan["TrackFiles"]["eligible"] == 2
    # 2 of the 3 MetadataFiles rows are absolute; 'artist.nfo' is skipped
    assert plan["MetadataFiles"]["eligible"] == 2
    assert plan["MetadataFiles"]["skipped_relative"] == 1


def test_apply_rewrite_changes_every_eligible_row_and_nothing_else():
    conn = _db()
    apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
    assert conn.execute("SELECT Path FROM RootFolders").fetchone()[0] == "/data/music"
    assert [r[0] for r in conn.execute("SELECT Path FROM TrackFiles ORDER BY Id")] == [
        "/data/music/Burzum/01.mp3", "/data/music/Autechre/02.flac"]
    assert [r[0] for r in conn.execute("SELECT RelativePath FROM MetadataFiles ORDER BY Id")] == [
        "/data/music/Burzum/album.nfo", "artist.nfo", "/data/music/Autechre/album.nfo"]


def test_apply_rewrite_preserves_row_counts_exactly():
    """The ADR-0003 failure was rows disappearing. Assert the count, always."""
    conn = _db()
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("RootFolders", "Artists", "TrackFiles", "MetadataFiles")}
    apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
    after = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before}
    assert before == after


def test_apply_rewrite_is_idempotent():
    conn = _db()
    apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
    second = plan_rewrite(conn, "/music", "/data/music")
    assert sum(p["eligible"] for p in second) == 0


def test_apply_rewrite_rolls_back_entirely_on_error():
    """A partial rewrite is worse than none: half the rows pointing at a root
    that no longer exists is not a state anything recovers from."""
    conn = _db()
    conn.execute("INSERT INTO TrackFiles VALUES (99, '/data/music/Burzum/01.mp3')")
    with pytest.raises(sqlite3.IntegrityError):
        apply_rewrite(conn, plan_rewrite(conn, "/music", "/data/music"), "/data/music")
    unchanged = conn.execute("SELECT Path FROM TrackFiles WHERE Id=1").fetchone()[0]
    assert unchanged == "/music/Burzum/01.mp3"
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd ~/nas && . .venv/bin/activate
pytest scripts/tests/test_lidarr_repath_db.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'scripts.lidarr_repath_db'`.

- [ ] **Step 3: Write the module**

Create `scripts/lidarr_repath_db.py`. The shape follows `AGENTS.md`: pure logic at module level, side effects in `main()`, exit `0`/`1`/`2`.

```python
#!/usr/bin/env python3
"""Rewrite Lidarr's /music path prefix to /data/music, offline, in one transaction.

Why this script exists
----------------------
Lidarr still copies instead of hardlinking because its root folder is /music
while downloads live under /downloads -- separate bind mounts, and link() cannot
cross a mount point (ADR-0002, verified: `ln /downloads/x /music/x` returns
EXDEV, `ln /data/downloads/x /data/music/x` succeeds). The fix is to put both
under /data.

Why NOT the API
---------------
Sonarr and Radarr were repathed with `PUT /api/v3/.../editor`. The same call
against Lidarr emptied TrackFiles: 150,187 rows -> 0 (ADR-0003). And the
non-editor form, `PUT /api/v1/artist/{id}?moveFiles=false`, updates only
Artists.Path -- TrackFiles.Path keeps an absolute /music/... string, which is
the ONLY handle Lidarr has on a file (no inode, no hash). Deleting the old root
folder afterwards then leaves 150,300 rows resolving to nothing.

/music and /data/music are two views of the SAME directory (/data is a bind
mount of /mnt/drive; /music is a bind mount of /mnt/drive/music). So nothing on
disk needs to move, and this is a pure metadata rewrite.

Safety properties
-----------------
* Dry-run by default. `--apply` is required to write anything.
* Refuses to run against a database Lidarr still has open (`--require-stopped`).
* Refuses to run unless a backup of the .db AND its -wal/-shm exists.
* One transaction: it commits everything or nothing. A half-rewritten database
  is worse than an untouched one.
* Verifies row counts are unchanged, and (with --verify-disk) that every
  rewritten path exists on disk, BEFORE committing.

Exit codes
----------
  0  success (or dry-run completed)
  1  partial -- verification failed and the transaction was rolled back
  2  fatal -- bad arguments, DB unreadable, Lidarr still running, no backup

Usage
-----
  python scripts/lidarr_repath_db.py --db /path/to/lidarr.db
  python scripts/lidarr_repath_db.py --db copy.db --apply --verify-disk --disk-prefix /mnt/drive/music
"""
from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath

# main() additionally needs argparse, os, random, subprocess and sys. They are
# left out of this excerpt so `ruff check scripts` is clean on what is shown --
# add them when you write main().

# Every table.column in Lidarr's schema holding a value that starts with the
# root path. Enumerated from the live database on 2026-09-02, not assumed:
#
#   RootFolders.Path              1        the root itself
#   Artists.Path                  3,336    artist folders
#   TrackFiles.Path               150,300  the only handle Lidarr has on a file
#   MetadataFiles.RelativePath    14,958   ABSOLUTE despite the column name
#
# MetadataFiles is the trap: 14,958 of its 43,299 rows are absolute /music/...
# paths and 28,341 are genuinely relative ('artist.nfo'). Omitting the table
# orphans every .nfo; rewriting it blindly corrupts the relative rows.
#
# Deliberately NOT rewritten -- historical audit text, not state Lidarr
# resolves against: History.SourceTitle (605,171), History.Data,
# DownloadHistory.Data, Commands.Body. See ADR-0003.
REWRITE_TARGETS = (
    ("RootFolders", "Path"),
    ("Artists", "Path"),
    ("TrackFiles", "Path"),
    ("MetadataFiles", "RelativePath"),
)


def rewrite_path(old: str, old_root: str, new_root: str) -> str:
    """Swap old_root for new_root at the front of `old`.

    Idempotent: a path already under new_root is returned unchanged. Raises
    ValueError for a traversal segment, and for anything not under either root
    -- including a relative path, which MetadataFiles is full of. Guessing at a
    path is how you write a wrong one into 168,595 rows.
    """
    if ".." in PurePosixPath(old).parts:
        raise ValueError(f"path traversal segment in {old!r}")
    new_root = new_root.rstrip("/")
    old_root = old_root.rstrip("/")
    if old == new_root or old.startswith(new_root + "/"):
        return old
    if old != old_root and not old.startswith(old_root + "/"):
        raise ValueError(f"{old!r} is not under {old_root!r}")
    suffix = old[len(old_root):].lstrip("/")
    return f"{new_root}/{suffix}" if suffix else new_root


def plan_rewrite(conn: sqlite3.Connection, old_root: str, new_root: str) -> list[dict]:
    """Count what each table would change, without changing anything."""
    old_root = old_root.rstrip("/")
    plan = []
    for table, column in REWRITE_TARGETS:
        try:
            total = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        except sqlite3.OperationalError:
            continue        # table absent in this schema version
        eligible = conn.execute(
            f'SELECT count(*) FROM "{table}" WHERE "{column}" = ? OR "{column}" LIKE ?',
            (old_root, old_root + "/%"),
        ).fetchone()[0]
        already = conn.execute(
            f'SELECT count(*) FROM "{table}" WHERE "{column}" = ? OR "{column}" LIKE ?',
            (new_root.rstrip("/"), new_root.rstrip("/") + "/%"),
        ).fetchone()[0]
        relative = conn.execute(
            f'SELECT count(*) FROM "{table}" WHERE "{column}" NOT LIKE ?', ("/%",)
        ).fetchone()[0]
        plan.append({
            "table": table, "column": column, "old_root": old_root,
            "total": total, "eligible": eligible,
            "already_migrated": already, "skipped_relative": relative,
        })
    return plan


def apply_rewrite(conn: sqlite3.Connection, plan: list[dict], new_root: str) -> dict:
    """Rewrite every eligible row in ONE transaction. Commits or rolls back.

    The LIKE-anchored UPDATE is what keeps /musicvideos out and leaves relative
    paths untouched: both fail `= old_root OR LIKE old_root || '/%'`.
    """
    new_root = new_root.rstrip("/")
    changed = {}
    # sqlite3 opens its own implicit transaction, so a bare BEGIN raises
    # "cannot start a transaction within a transaction". Take manual control
    # for the duration, and hand the connection back as we found it.
    prior = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in plan:
            table, column = item["table"], item["column"]
            old_root = item["old_root"].rstrip("/")
            cur = conn.execute(
                f'UPDATE "{table}" SET "{column}" = ? || substr("{column}", ?) '
                f'WHERE "{column}" = ? OR "{column}" LIKE ?',
                (new_root, len(old_root) + 1, old_root, old_root + "/%"),
            )
            changed[table] = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.isolation_level = prior
    return changed
```

The `main()` half follows the same contract as every other script here — `--db`, `--old-root`, `--new-root`, `--apply`, `--backup-dir`, `--require-stopped`, `--verify-disk`, `--disk-prefix`, `--sample N`. Its required behaviours, each of which should be obvious from the docstring above:

1. **Refuse to touch a live database.** `docker inspect -f '{{.State.Running}}' lidarr` must be `false`, unless `--no-require-stopped` is passed explicitly. Rewriting under a running Lidarr means WAL frames land on top of the rewrite.
2. **Refuse without a backup** of `lidarr.db`, `lidarr.db-wal` **and** `lidarr.db-shm`. WAL mode means a `.db` copied alone reads back stale — the same trap CLAUDE.md documents for the *arr notification toggles.
3. **Print the plan table** (per-table `total / eligible / already_migrated / skipped_relative`) in both dry-run and apply mode.
4. **Verify before committing**, inside the transaction: total row count per table unchanged, zero rows still matching the old root, and — with `--verify-disk` — `os.path.exists()` on a random `--sample` of rewritten `TrackFiles.Path` values translated through `--disk-prefix`. Any failure rolls back and exits `1`.

- [ ] **Step 4: Run the tests until they pass**

```bash
cd ~/nas && . .venv/bin/activate
pytest scripts/tests/test_lidarr_repath_db.py -v
```

Expected: 14 passed.

- [ ] **Step 5: Lint and check the whole suite still passes**

```bash
ruff check scripts
pytest -q scripts/tests
python scripts/test_scripts.py 2>&1 | tail -3
```

Expected: ruff clean, all tests pass (332 + 14), 19/19 smoke.

- [ ] **Step 6: Commit**

```bash
git add scripts/lidarr_repath_db.py scripts/tests/test_lidarr_repath_db.py
git commit -m "feat(scripts): offline SQLite repath for Lidarr's root folder (ADR-0003)

Lidarr still copies instead of hardlinking because its root is /music while
downloads are /downloads -- separate bind mounts, and link() cannot cross one.
Verified: ln /downloads/x /music/x returns EXDEV; ln /data/downloads/x
/data/music/x succeeds.

The API cannot do this migration. TrackFiles has no inode and no hash -- the
absolute path is the only handle Lidarr has on a file -- and
PUT /api/v1/artist/{id}?moveFiles=false updates Artists.Path only, leaving
150,300 rows pointing at /music. Deleting the old root folder afterwards is the
documented route back to the 150,187 -> 0 incident.

/music and /data/music are the same directory, so nothing on disk moves and
this is a pure metadata rewrite: one transaction over RootFolders.Path,
Artists.Path, TrackFiles.Path and MetadataFiles.RelativePath.

MetadataFiles is the trap and is why the table list is enumerated from the live
schema rather than assumed: 14,958 of its 43,299 rows hold ABSOLUTE /music
paths despite the column being named RelativePath, and 28,341 are genuinely
relative. Omitting the table orphans every .nfo; rewriting it blindly corrupts
the relative rows.

History.SourceTitle (605,171 rows), History.Data, DownloadHistory.Data and
Commands.Body are deliberately left alone -- audit text, not resolved state.

14 unit tests cover the path logic and the SQL against a synthetic DB,
including idempotency, the /musicvideos shared-prefix trap, traversal
rejection, the mixed absolute/relative MetadataFiles shape, row-count
preservation, and full rollback on error. No live writes in this commit."
```

---

### Task 14: Rehearse the whole migration on a copy

This replaces rev 1's single-artist canary, which could not have detected the failure it was written to catch. A full rehearsal on a copy of the real database is strictly better: same 168,595 rows, same schema, same data, and **zero** risk, because the live database is never opened.

**Files:** none — this is an operational task, and its output decides whether Task 15 happens at all.

- [ ] **Step 1: Take a consistent copy of the real database**

Lidarr's SQLite is in WAL mode, so all three files must come together:

```bash
cd ~/nas
mkdir -p /tmp/lidarr-rehearsal && rm -f /tmp/lidarr-rehearsal/*
for f in lidarr.db lidarr.db-wal lidarr.db-shm; do
  cp ".docker-config/lidarr/$f" "/tmp/lidarr-rehearsal/$f"
done
ls -lh /tmp/lidarr-rehearsal/
```

> Copying a live WAL database is fine **for a rehearsal** — a slightly torn read changes nothing about whether the rewrite logic is correct. It is not fine for the real thing, which is why Task 15 stops Lidarr first.

- [ ] **Step 2: Record the baseline the rewrite must preserve**

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:/tmp/lidarr-rehearsal/lidarr.db?mode=ro", uri=True)
for t in ("RootFolders", "Artists", "TrackFiles", "MetadataFiles"):
    total = c.execute(f"select count(*) from {t}").fetchone()[0]
    print(f"{t:16} {total:>8}")
print("\nartists with zero track files:",
      c.execute("""select count(*) from Artists a where not exists (
        select 1 from TrackFiles tf join Albums al on tf.AlbumId=al.Id
        where al.ArtistMetadataId=a.ArtistMetadataId)""").fetchone()[0])
PY
```

Expected, close to: `RootFolders 1`, `Artists ~3,340`, `TrackFiles ~150,300`, `MetadataFiles 43,299`, and **748** artists with zero track files. The artist and track-file counts drift upward as the drip cron runs; what matters is that Step 5's post-rewrite counts equal *these* counts, not that they match the numbers printed here.

> That 748 is the number that invalidated rev 1's canary. `plan_repath` sorted ascending by track-file count, so the first artist it moved was certain to be one of these, and its verification was `0 == 0`.

- [ ] **Step 3: Dry-run against the copy**

```bash
cd ~/nas && . .venv/bin/activate
python scripts/lidarr_repath_db.py --db /tmp/lidarr-rehearsal/lidarr.db
```

Expected — a plan table matching the counts above, with `eligible` equal to `1 / <artists> / <trackfiles> / 14958` and `skipped_relative` of `28341` on `MetadataFiles`. **If `MetadataFiles.eligible` is 0, the table is not in `REWRITE_TARGETS` and 14,958 `.nfo` files are about to be orphaned — stop and fix Task 13.**

- [ ] **Step 4: Apply to the copy, with on-disk verification**

```bash
python scripts/lidarr_repath_db.py --db /tmp/lidarr-rehearsal/lidarr.db \
  --apply --no-require-stopped --backup-dir /tmp/lidarr-rehearsal \
  --verify-disk --disk-prefix /mnt/drive/music --sample 500
echo "exit=$?"
```

Expected: exit `0`, per-table changed counts identical to `eligible`, row counts unchanged, and 500/500 sampled paths existing on disk.

- [ ] **Step 5: Audit the copy independently of the tool that wrote it**

Do not accept the script's own report as the verification:

```bash
python3 - <<'PY'
import os, random, sqlite3
c = sqlite3.connect("file:/tmp/lidarr-rehearsal/lidarr.db?mode=ro", uri=True)
q = lambda s, *a: c.execute(s, a).fetchone()[0]
print("row counts    :", {t: q(f"select count(*) from {t}")
      for t in ("RootFolders","Artists","TrackFiles","MetadataFiles")})
print("still /music  :", {t: q(f"select count(*) from {t} where {col} like '/music/%' or {col}='/music'")
      for t, col in (("RootFolders","Path"),("Artists","Path"),
                     ("TrackFiles","Path"),("MetadataFiles","RelativePath"))})
print("now /data/music:", {t: q(f"select count(*) from {t} where {col} like '/data/music%'")
      for t, col in (("RootFolders","Path"),("Artists","Path"),
                     ("TrackFiles","Path"),("MetadataFiles","RelativePath"))})
print("relative kept :", q("select count(*) from MetadataFiles where RelativePath not like '/%'"))
print("root folder   :", q("select Path from RootFolders"))
rows = [r[0] for r in c.execute("select Path from TrackFiles")]
sample = random.sample(rows, 1000)
missing = [p for p in sample if not os.path.exists(p.replace("/data/music", "/mnt/drive/music", 1))]
print(f"on disk       : {len(sample)-len(missing)}/{len(sample)} exist; missing={missing[:3]}")
PY
```

Expected: row counts identical to Step 2; `still /music` all zero; `now /data/music` equal to Step 2's counts for the first three, plus `14958` for `MetadataFiles`; `relative kept` = `28341`; root folder `/data/music`; and **1000/1000** sampled files existing. Anything less than 1000/1000 means the prefix mapping is wrong — stop, and do not run Task 15.

- [ ] **Step 6: Prove idempotency on the copy**

The real run may need repeating after an interruption, so re-running must be a no-op rather than a corruption:

```bash
python scripts/lidarr_repath_db.py --db /tmp/lidarr-rehearsal/lidarr.db
```

Expected: `eligible` zero across every table, `already_migrated` matching the previous `eligible`. No `/data/music/data/music/...` anywhere.

- [ ] **Step 7: Record the rehearsal in ADR-0003**

Whichever way it went, write it down: the method (offline prefix rewrite, not the API), the four tables and their row counts, the `MetadataFiles` finding, the sampled on-disk verification, and the decision to leave History untouched. If it failed, record the failure mode and stop the phase — a second failure is more valuable written down than a second rollback is.

- [ ] **Step 8: Commit the record**

```bash
rm -rf /tmp/lidarr-rehearsal
git add docs/decisions/0003-lidarr-data-mount-staged.md
git commit -m "docs(adr-0003): record the offline repath rehearsal on a DB copy

Full-scale rehearsal on a copy of the live database rather than a single-artist
canary. The canary could not have worked: 748 of 3,336 artists have zero track
files and the plan sorted smallest-first, so it was certain to verify 0 == 0.

Records the four tables rewritten, that MetadataFiles.RelativePath holds 14,958
absolute paths despite its name, that History's 605,171 /music strings are left
alone on purpose, and the 1000/1000 on-disk sample."
```

---

### Task 15: Run it for real

Only if Task 14's rehearsal was clean, including 1000/1000 on disk.

**Files:**
- Modify: `docs/decisions/0003-lidarr-data-mount-staged.md`
- Modify: `docs/decisions/0002-single-mount-data-hardlinks.md`
- Modify: `README.md`
- Uses (does not modify): `scripts/lidarr_repath_db.py` from Task 13

- [ ] **Step 1: Silence the crons that write to Lidarr**

Four jobs touch Lidarr on schedules as tight as every 15 minutes. An import landing mid-migration changes a count and produces a spurious failure; one landing *after* the rewrite but against a stopped Lidarr just fails.

```bash
crontab -l > /tmp/crontab-before-repath.bak
crontab -l | sed -E '/lidarr-monitor-sweep|lidarr-backlog-drip|lidarr-stuck-reaper|process-soulseek-imports/ s/^/#REPATH /' | crontab -
crontab -l | grep -E '^#REPATH' | wc -l      # expect 4
```

Restoring them is Step 9. Set a reminder — a silently disabled drip is its own outage.

- [ ] **Step 2: Stop Lidarr and take the real backup**

```bash
cd ~/nas
docker compose stop lidarr
docker inspect -f '{{.State.Running}}' lidarr     # must be false
BK=/mnt/drive/backups/pre-lidarr-repath/$(date +%Y%m%d-%H%M)
mkdir -p "$BK"
for f in lidarr.db lidarr.db-wal lidarr.db-shm; do
  cp -v ".docker-config/lidarr/$f" "$BK/$f"
done
ls -lh "$BK"; echo "BK=$BK"
```

All three files, because the database is WAL-mode: a `.db` copied without its `-wal` reads back stale, which is the same trap CLAUDE.md documents for the *arr notification toggles. **Do not continue without all three.**

- [ ] **Step 3: Dry-run against the real database, stopped**

```bash
. .venv/bin/activate
python scripts/lidarr_repath_db.py --db .docker-config/lidarr/lidarr.db
```

Expected: the same plan table as the rehearsal. Any difference means the database moved between rehearsal and now — re-read it before proceeding.

- [ ] **Step 4: Apply**

```bash
python scripts/lidarr_repath_db.py --db .docker-config/lidarr/lidarr.db \
  --apply --backup-dir "$BK" --verify-disk --disk-prefix /mnt/drive/music --sample 1000
echo "exit=$?"
```

Expected: exit `0`. On exit `1` the transaction rolled back and the database is untouched — read the reason before doing anything else. On any other outcome, restore:

```bash
docker compose stop lidarr
cp "$BK"/lidarr.db* .docker-config/lidarr/
docker compose up -d lidarr
```

- [ ] **Step 5: Audit before restarting Lidarr**

Run the Step 5 audit block from Task 14 against `.docker-config/lidarr/lidarr.db`. Every expectation is the same. **Do not start Lidarr until it passes.**

- [ ] **Step 6: Start Lidarr and let it settle**

```bash
docker compose up -d lidarr
for i in $(seq 1 36); do
  s=$(docker inspect -f '{{.State.Health.Status}}' lidarr 2>/dev/null)
  echo "t=$((i*5))s $s"; [ "$s" = healthy ] && break; sleep 5
done
docker logs lidarr --tail 40 | grep -iE "error|exception|not found|missing" || echo "no errors"
```

- [ ] **Step 7: Verify through the API, and wait for the rescan rather than sleeping**

Lidarr's rescan is asynchronous. Rev 1's `--settle-seconds 2` raced it; poll the command queue instead:

```bash
K=$(sed -n 's/^API_KEY_LIDARR=//p' .env)
Q() { curl -s -H "X-Api-Key: $K" "http://127.0.0.1:8686/api/v1/$1"; }

Q rootfolder | python3 -c "
import sys,json;[print(' ',x['id'],x['path'],'accessible=',x.get('accessible')) for x in json.load(sys.stdin)]"

# trigger a full rescan and WAIT for it, rather than guessing a settle time
curl -s -X POST -H "X-Api-Key: $K" -H 'Content-Type: application/json' \
  -d '{"name":"RescanFolders"}' http://127.0.0.1:8686/api/v1/command \
  | python3 -c "import sys,json;print('command id',json.load(sys.stdin)['id'])"
while :; do
  st=$(Q command | python3 -c "
import sys,json
c=[x for x in json.load(sys.stdin) if x['name'] in ('RescanFolders','RefreshArtist')]
print(','.join(sorted({x['status'] for x in c})) or 'idle')")
  echo "rescan: $st"; case "$st" in idle|completed) break;; esac; sleep 15
done

Q artist | python3 -c "
import sys,json;d=json.load(sys.stdin)
print('artists:',len(d),'trackfiles:',sum((a.get('statistics') or {}).get('trackFileCount',0) for a in d))
print('still on /music:',sum(1 for a in d if a['path'].startswith('/music')))"
```

Expected: one root folder, `/data/music`, `accessible=True`; artist and track-file totals matching Task 14 Step 2's baseline exactly; and **zero** artists still on `/music`. A track-file total of 0 is the ADR-0003 failure — restore from `$BK` immediately.

- [ ] **Step 8: Confirm the hardlink win is real**

This is the whole point of the phase, and it needs an actual import — not an inference:

```bash
# existing files are still nlink=1; that is expected and is not the test
docker exec lidarr sh -c 'find /data/music -name "*.flac" | head -50 | xargs -r stat -c "%h" | sort | uniq -c'
```

Re-enable the crons (Step 9), let one real import land, then:

```bash
docker exec lidarr sh -c 'find /data/music -newermt "-2 hours" -name "*.flac" -o -newermt "-2 hours" -name "*.mp3" \
  | head -5 | xargs -r stat -c "%h %n"'
df -h /mnt/drive
```

Expected: link count `%h` of **2 or more** on newly imported files. `1` means it still copied — investigate before declaring ADR-0003 resolved. The pre-existing backlog stays at `nlink=1`; that is not a regression, it is the 425 files CLAUDE.md already records.

- [ ] **Step 9: Restore the crons**

```bash
crontab -l | sed -E 's/^#REPATH //' | crontab -
crontab -l | grep -cE 'lidarr-monitor-sweep|lidarr-backlog-drip|lidarr-stuck-reaper|process-soulseek-imports'   # expect 4
crontab -l | grep -c '^#REPATH'   # expect 0
tail -20 logs/lidarr_backlog_drip.log
tail -20 logs/lidarr_monitor_sweep.log
```

Both logs must show a successful run on the next cycle. This is the step most likely to be forgotten, and its failure is silent.

- [ ] **Step 10: Update ADR-0003, ADR-0002 and `README.md`**

Set ADR-0003's status to `resolved <date>`, record the final totals, the four tables, and that the method was an offline prefix rewrite rather than any API call. In ADR-0002, change the line saying Lidarr is the exception. In `README.md`, delete the "Lidarr still copies instead of hardlinking" Known-gaps bullet, change the lidarr row in the service table from "`/data` staged but unused" to "`/data` in use", and update the "Imports are copying instead of hardlinking" troubleshooting block, which currently says Lidarr deliberately still copies.

Leave lidarr's `/music` bind mount in place for now — it costs nothing and it is the fastest rollback if something surfaces weeks later. Removing it is a follow-up once a full cycle has run clean.

- [ ] **Step 11: Commit**

```bash
git add docs/decisions/0003-lidarr-data-mount-staged.md docs/decisions/0002-single-mount-data-hardlinks.md README.md
git commit -m "docs: Lidarr is on /data/music and hardlinking — ADR-0003 resolved

Migrated by an offline SQLite prefix rewrite with Lidarr stopped: one
transaction over RootFolders.Path, Artists.Path, TrackFiles.Path and
MetadataFiles.RelativePath, verified by row counts and a 1000-path on-disk
sample before commit. Rehearsed at full scale on a copy of the database first.

No API call was involved. PUT /api/v1/artist/editor is what emptied TrackFiles
in September, and the non-editor form rewrites Artists.Path only -- leaving
150,300 TrackFiles rows on /music, which the old root folder's deletion would
then have resolved to nothing.

Nothing moved on disk: /music and /data/music are two bind-mount views of the
same directory, so this was metadata only.

Hardlinking confirmed by link count on newly imported files. The pre-existing
backlog stays at nlink=1 as documented."
```

---
# Phase E — Off-box backup

`config_backup.py` writes 1.5 GB of config to `/mnt/drive/backups/` — the same host, the same box, and for `${CONFIG_DIRECTORY}` (which lives on the OS NVMe) not even a different disk failure domain. A single drive loss takes the config and its backups together.

---

### Task 16: Add a restic wrapper with an explicit, user-supplied destination

> **Decision required from the operator before this task.** The destination and its credentials cannot be invented. Pick one and set the matching `.env` values in Step 2:
> - **B2 / S3 / any rclone remote** — cheapest for ~2 GB, genuinely off-site.
> - **SFTP to another machine you own** — free, off-box, but not off-site.
> - **A USB disk you rotate** — off-box only while unplugged; needs a human.
>
> Steps below are written for restic, which handles all three via its backend URL, and which is chosen over `rsync` because it deduplicates, encrypts client-side, and — most importantly — can *verify* the stored data with `restic check --read-data`. An unverified backup is a hope.

Four corrections to rev 1, all from review:

1. **The retention policy would never have pruned anything.** `restic forget` groups snapshots with `--group-by host,paths` by default and applies the keep policy per group. Rev 1 backed up `$newest` — a new timestamped directory every night — so every snapshot was its own group of one and `--keep-daily 7` kept all of them forever. The same changing path also defeats parent-snapshot selection, making every run a full rescan.
2. **`restic check` is not `restic check --read-data`.** Rev 1's commit message claimed the latter as the reason for choosing restic and the script ran the former, which verifies structure only.
3. **Use B2's S3-compatible API, not the native `b2:` backend.** restic's own documentation recommends generating S3-compatible keys, and notes the S3 backend only *hides* obsolete files — so a bucket lifecycle rule is needed too, or hidden versions accumulate forever.
4. **`RESTIC_PASSWORD` in `.env` is the wrong place** for the one secret whose loss makes every snapshot unreadable. Use `RESTIC_PASSWORD_FILE` pointing at a `0600` file.

**Files:**
- Create: `scripts/offsite_backup.sh`
- Modify: `.env.example`, `AGENTS.md`, `Makefile`

- [ ] **Step 1: Install restic**

```bash
sudo apt-get update && sudo apt-get install -y restic
restic version
```

- [ ] **Step 2: Configure the destination**

Add to `.env` (gitignored — real values here), and document them redacted in `.env.example`:

```bash
# --- off-box config backup (scripts/offsite_backup.sh) ---
# restic repository URL. For Backblaze B2, use the S3-COMPATIBLE endpoint with
# S3 credentials rather than the native b2: backend -- restic's docs recommend
# it, and it is better tested:
#   s3:s3.<region>.backblazeb2.com/<bucket>   (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
#   sftp:user@otherbox:/backups/nas           (needs working key-based ssh)
RESTIC_REPOSITORY=
# Path to a 0600 file holding the passphrase. NOT the passphrase itself:
# losing it makes every snapshot unreadable, so it does not belong in the same
# file as everything else, and it must be stored off this machine as well.
RESTIC_PASSWORD_FILE=/home/tom/.config/restic/nas.pass
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

Generate the passphrase, store it off this machine, then write the file:

```bash
mkdir -p ~/.config/restic
openssl rand -base64 48 > ~/.config/restic/nas.pass
chmod 600 ~/.config/restic/nas.pass
ls -l ~/.config/restic/nas.pass      # must be -rw-------
cat ~/.config/restic/nas.pass        # record this somewhere that is NOT this box
```

If the destination is B2 via S3, add the bucket lifecycle rule now — "keep only the last version" — because the S3 backend hides rather than deletes, and `forget --prune` will otherwise leave hidden objects accruing storage cost indefinitely.

- [ ] **Step 3: Give the backup a stable path**

This is what makes retention work. Point a symlink at the newest local backup and back up **the symlink target's stable path**, so every snapshot shares one `paths` value:

```bash
cd ~/nas
LOCAL=/mnt/drive/backups/nas-configs
ln -sfn "$(ls -dt $LOCAL/*/ | head -1)" "$LOCAL/latest"
ls -l "$LOCAL/latest"
```

`config_backup.py` should update this symlink at the end of each run; until it does, `offsite_backup.sh` refreshes it itself (Step 4). With a stable `/mnt/drive/backups/nas-configs/latest` path, `--group-by host,paths` puts every snapshot in one group, `--keep-daily 7` means what it says, and restic can pick a parent snapshot instead of rescanning 1.5 GB from scratch every night.

- [ ] **Step 4: Write the wrapper**

Create `scripts/offsite_backup.sh`:

```bash
#!/usr/bin/env bash
#
# offsite_backup.sh -- push the config backup off this box.
#
# config_backup.py writes to /mnt/drive/backups/, which is the same host and,
# for ${CONFIG_DIRECTORY} on the OS NVMe, not even a different failure domain.
# One drive loss takes the config and its backups together. This closes that.
#
# Backs up the LATEST local config-backup directory rather than
# ${CONFIG_DIRECTORY} directly, so what leaves the box is a consistent snapshot
# that config_backup.py already took with the services quiesced -- not live
# WAL-mode SQLite being written underneath us.
#
# It backs up through a STABLE symlink path, not the timestamped directory.
# restic's `forget` groups by --group-by host,paths and applies the keep policy
# per group; a new path every night makes every snapshot its own group of one,
# so --keep-daily 7 keeps everything forever and nothing is ever pruned. The
# stable path also lets restic pick a parent snapshot instead of rescanning.
#
# Exit codes:  0 ok  |  1 partial (backup ok, verify or prune failed)  |  2 fatal
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

# Read .env WITHOUT sourcing it: an unquoted value executes as a command in any
# shell (WATCHTOWER_SCHEDULE='0 0 4 * * *' was doing exactly that, silenced
# here by 2>/dev/null in the previous version of this script).
env_get() { sed -n "s/^$1=//p" .env 2>/dev/null | tail -1 | sed "s/^['\"]//;s/['\"]$//"; }
for v in RESTIC_REPOSITORY RESTIC_PASSWORD_FILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
  val=$(env_get "$v"); [ -n "$val" ] && export "$v=$val"
done

LOCAL_BACKUPS="${LOCAL_BACKUP_DIR:-/mnt/drive/backups/nas-configs}"
STABLE="$LOCAL_BACKUPS/latest"
DRY_RUN=1
[ "${1:-}" = "--apply" ] && DRY_RUN=0

for v in RESTIC_REPOSITORY RESTIC_PASSWORD_FILE; do
  if [ -z "${!v:-}" ]; then echo "FATAL: $v is not set in .env" >&2; exit 2; fi
done
if [ ! -r "$RESTIC_PASSWORD_FILE" ]; then
  echo "FATAL: RESTIC_PASSWORD_FILE ($RESTIC_PASSWORD_FILE) is not readable" >&2; exit 2
fi

newest=$(ls -dt "$LOCAL_BACKUPS"/*/ 2>/dev/null | grep -v '/latest/$' | head -1)
if [ -z "$newest" ]; then
  echo "FATAL: no local config backup under $LOCAL_BACKUPS -- run config_backup.py first" >&2
  exit 2
fi
ln -sfn "$newest" "$STABLE"

age_h=$(( ( $(date +%s) - $(stat -Lc %Y "$STABLE") ) / 3600 ))
echo "newest local backup: $newest"
echo "  via stable path:   $STABLE (${age_h}h old, $(du -sLh "$STABLE" | cut -f1))"
if [ "$age_h" -gt 48 ]; then
  echo "WARNING: local backup is ${age_h}h old; the nightly config_backup cron may be broken" >&2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: would run: restic backup --tag nas-config $STABLE"
  restic snapshots --compact 2>/dev/null | tail -5
  echo "pass --apply to actually push"
  exit 0
fi

rc=0
echo "==> backing up"
restic backup --tag nas-config --host "$(hostname)" "$STABLE" || exit 2

# --group-by host,tags: the path is stable now, but tagging is the durable
# guarantee that one changed path cannot silently disable retention again.
echo "==> pruning (keep 7 daily, 5 weekly, 6 monthly)"
restic forget --tag nas-config --group-by host,tags \
  --keep-daily 7 --keep-weekly 5 --keep-monthly 6 --prune || rc=1

# check alone verifies STRUCTURE only. --read-data-subset actually reads the
# stored blobs back; 1/7 per night covers the whole repository each week at a
# seventh of the egress.
week=$(( $(date +%-j) % 7 + 1 ))
echo "==> verifying repository integrity (structure + data subset ${week}/7)"
restic check --read-data-subset=${week}/7 || rc=1

echo "==> latest snapshots"
restic snapshots --compact --tag nas-config | tail -5
exit $rc
```

```bash
chmod +x scripts/offsite_backup.sh
```

- [ ] **Step 5: Initialise the repo, dry-run, then a real run**

```bash
cd ~/nas
export RESTIC_REPOSITORY=$(sed -n 's/^RESTIC_REPOSITORY=//p' .env)
export RESTIC_PASSWORD_FILE=$(sed -n 's/^RESTIC_PASSWORD_FILE=//p' .env)
export AWS_ACCESS_KEY_ID=$(sed -n 's/^AWS_ACCESS_KEY_ID=//p' .env)
export AWS_SECRET_ACCESS_KEY=$(sed -n 's/^AWS_SECRET_ACCESS_KEY=//p' .env)
restic init && restic snapshots
./scripts/offsite_backup.sh
./scripts/offsite_backup.sh --apply; echo "exit=$?"
```

Expected: `created restic repository … at <your repo>`, an empty snapshot list, a dry-run summary, then a completed `backup` / `forget` / `check` with exit `0`.

- [ ] **Step 6: Prove retention actually prunes**

This is the bug rev 1 shipped, so verify the fix rather than assuming it. Take three snapshots and confirm they land in **one** group:

```bash
for i in 1 2 3; do ./scripts/offsite_backup.sh --apply >/dev/null; done
restic snapshots --compact --tag nas-config
restic forget --tag nas-config --group-by host,tags --keep-last 1 --dry-run
```

Expected: the `forget --dry-run` reports **one** group containing all snapshots and lists the older ones as "would remove". If it reports one group per snapshot, the path is still varying — re-check Step 3.

- [ ] **Step 7: Prove a restore works — the only step that makes this a backup**

```bash
cd ~/nas
restic restore latest --target /tmp/restic-restore-test
find /tmp/restic-restore-test -type f | head -10
find /tmp/restic-restore-test -name '*.db' | head -5
du -sh /tmp/restic-restore-test
rm -rf /tmp/restic-restore-test
```

Expected: real config files and at least one `.db` present, total size comparable to the source. **If you cannot restore, you do not have a backup** — fix it before committing.

- [ ] **Step 8: Add the Make target**

Append to `Makefile`, and add `backup-offsite` to `.PHONY`:

```makefile
backup-offsite: ## Push the newest local config backup off this box (restic)
	@scripts/offsite_backup.sh --apply
```

- [ ] **Step 9: Commit**

```bash
git add scripts/offsite_backup.sh Makefile .env.example AGENTS.md
git commit -m "feat(backup): push the config backup off this box with restic

config_backup.py wrote 1.5GB to /mnt/drive/backups/ -- same host, and for
\${CONFIG_DIRECTORY} on the OS NVMe not even a different failure domain. One
drive loss took the config and its backups together.

Backs up the newest config_backup.py output rather than \${CONFIG_DIRECTORY}
directly, so what leaves the box is a quiesced snapshot and not live WAL-mode
SQLite.

Four things the first draft got wrong, all found in review:
- it backed up a new timestamped directory each night, and restic forget groups
  by --group-by host,paths, so every snapshot was its own group of one and
  --keep-daily 7 kept everything forever. Now via a stable 'latest' symlink and
  --group-by host,tags, which also lets restic pick a parent snapshot instead
  of rescanning 1.5GB nightly;
- it ran 'restic check' while the commit message claimed 'check --read-data'.
  Now --read-data-subset=N/7, rotating weekly over the whole repository;
- B2 via the S3-compatible endpoint, not the native b2: backend, per restic's
  docs -- plus the bucket lifecycle rule the S3 backend needs, since it hides
  obsolete files rather than deleting them;
- RESTIC_PASSWORD_FILE (0600) instead of the passphrase in .env.

It also reads .env with sed rather than sourcing it: the old version sourced it
with 2>/dev/null, which silently swallowed the unquoted WATCHTOWER_SCHEDULE
executing as a command.

Dry-run default. Retention proven to prune, and a restore verified into /tmp
before committing."
```

---

### Task 17: Schedule it, and make a silent failure impossible

**Files:**
- Modify: crontab (not version-controlled — record it in `README.md`)
- Modify: `README.md`

- [ ] **Step 1: Add the cron entry**

Wrapped in `cron_job.py` like every other job, so a failure or a stall reports itself to ntfy. Runs at 02:00, an hour after `config_backup.py` at 01:00, so it always has a fresh local snapshot:

```bash
crontab -l > /tmp/crontab.bak
( crontab -l; cat <<'CRON'
0 2 * * * /usr/bin/flock -n /tmp/nas-offsite-backup.lock /usr/bin/env bash -c "cd /home/tom/nas && . .venv/bin/activate && python scripts/cron_job.py --name offsite-backup --max-age-min 2880 -- bash scripts/offsite_backup.sh --apply >> logs/offsite_backup.log 2>&1"
CRON
) | crontab -
crontab -l | grep offsite
```

**`cd /home/tom/nas &&` is load-bearing and was missing in rev 1.** Cron runs with `CWD=$HOME` (`/home/tom`), so without it `. .venv/bin/activate`, `scripts/cron_job.py`, `scripts/offsite_backup.sh` and `logs/` all resolve to the wrong place and the job never runs — while the verification in Step 2, which is executed *after* a manual `cd`, passes. Every one of the 14 existing crontab entries has it; match them.

`--max-age-min 2880` (48 h) means `cron_job.py` raises an alert if the job has not succeeded within two days, which is what turns a silently-broken backup into a notification.

- [ ] **Step 2: Verify the wrapper path works the way cron will run it**

Reproduce cron's environment rather than your shell's — that is the whole point:

```bash
cd /home/tom            # cron's actual working directory
env -i HOME=/home/tom PATH=/usr/bin:/bin /usr/bin/env bash -c \
  "cd /home/tom/nas && . .venv/bin/activate && python scripts/cron_job.py --name offsite-backup-test --max-age-min 2880 -- bash scripts/offsite_backup.sh >> logs/offsite_backup.log 2>&1"
echo "exit=$?"
tail -20 /home/tom/nas/logs/offsite_backup.log
```

Expected: exit `0` and dry-run output in the log. Then confirm the failure mode the `cd` protects against, so the reason it is there is recorded:

```bash
cd /home/tom
env -i HOME=/home/tom PATH=/usr/bin:/bin /usr/bin/env bash -c \
  ". .venv/bin/activate && python scripts/cron_job.py --name x -- true" ; echo "exit=$? (expect non-zero)"
```

- [ ] **Step 3: Confirm the alert path is live**

```bash
cd ~/nas && . .venv/bin/activate && python scripts/stack_watchdog.py
```

The offsite job should appear in the watchdog's cron freshness view (or at minimum not be reported as stale immediately after a successful run).

- [ ] **Step 4: Update `README.md`**

Delete the last Known-gaps bullet:

```markdown
- **No off-box backup of `${CONFIG_DIRECTORY}`.** `config_backup.py` writes to
  `/mnt/drive/backups/`, which is the same host and the same box.
```

Add to the Scheduled jobs table, in the `daily` row: `· offsite_backup 02:00`, and add `verify-runtime 06:15` from Task 5. In the monitoring table, add a row:

```markdown
| `scripts/offsite_backup.sh` (daily 02:00) | Config surviving the loss of this machine | Media — 4.6 T is not backed up anywhere, by choice |
```

That last clause matters: this task backs up **config**, not media. Say so rather than letting the gap list imply otherwise.

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "docs: schedule the off-box config backup, and say what it does not cover

Daily 02:00, an hour after config_backup.py, wrapped in cron_job.py with
--max-age-min 2880 so a silently-broken backup becomes an ntfy alert.

The entry begins with 'cd /home/tom/nas &&', matching all 14 existing ones.
Cron's working directory is \$HOME, so without it the venv, the scripts and
logs/ all resolve elsewhere -- and the obvious verification, run after a manual
cd, passes anyway. Step 2 reproduces cron's environment instead.

States explicitly that this covers config and not the 4.6T of media, so the
closed gap is not read as more than it is."
```

---

## Final gate

```bash
cd ~/nas
make lint
make check                 # expect: no failures
make verify-runtime        # expect: all arms green, exit 0
./scripts/check-invariants.sh -v | grep -c '  ok'

. .venv/bin/activate
ruff check scripts && python scripts/test_scripts.py 2>&1 | tail -3 && pytest -q scripts/tests

docker compose config > /tmp/final.yml
diff /tmp/plan-baseline.yml /tmp/final.yml
```

The final diff should contain **exactly** these changes and nothing else:

1. `watchtower`: image → `nickfedor/watchtower:1.22.0`; `DOCKER_API_VERSION` removed; `WATCHTOWER_MONITOR_ONLY: "true"` added (Task 7)
2. `jellyfin`: `image:` tag pinned (Task 8)
3. `swag`: `KILL` added to `cap_add` (Task 9)
4. `swag`: lingarr proxy-conf bind mount added (Task 10)
5. `playlist-generator-db`: `cap_drop: [ALL]` + 4 `cap_add` (Task 11)
6. `playlist-generator`: `cap_drop: [ALL]` + 5 `cap_add` (Task 12)

Anything else is a regression introduced by this plan. Find it and fix it rather than explaining it.

Remaining warnings at the end should be exactly two, both intentional:

- `qbit-diskio-type` — until Task 4 Step 5's measurement is done and recorded.
- `swag-labels-are-routed` — for services routed only from gitignored `.docker-config/`, which is its own follow-up.

## Scorecard

| README rule / gap | Outcome |
|---|---|
| qbittorrent keeps `CAP_KILL` | Now asserted at **runtime** too (`make verify-runtime`), not just in config |
| qbittorrent tag pinned ≥ 5.2.2 | Unchanged — already structural. Its companion fix is now asserted against the **live session**, and `DiskIOType` is surfaced as the thing `DisableOSCache` only mitigates (Task 4) |
| qbittorrent/jellyfin unlabelled for Watchtower | **Rule retired.** Watchtower can no longer write to any container, and runs a maintained image (Task 7) |
| `memswap_limit == mem_limit` | Unchanged — already asserted |
| slskd healthcheck Soulseek-independent | **Now asserted, generically** — no autoheal-monitored healthcheck may probe a dependency (Task 1) |
| autoheal timeouts | **Now computed from the model**, over the monitored population, honouring per-container overrides (Task 2) |
| No `QBITTORRENT_USER`/`PASS` on the container | Unchanged — already asserted |
| Only dockerproxy has the socket | Now asserted at **runtime** too (Task 5) |
| `/data` mounts | Unchanged — already asserted |
| Jellyfin volume mappings frozen | **Now asserted**, sources included (Task 3) |
| Hardening baseline | **No exceptions left** (Tasks 11–12) |
| *New:* `swag` nginx could not signal its own workers | **Fixed** (Task 9). Found by review, not by an outage |
| *New:* `.env` executed a command when sourced | **Fixed** (Task 6 Step 0) |
| Gap: playlist-generator capabilities | **Closed** (Tasks 11–12) |
| Gap: Lidarr copies instead of hardlinks | **Closed** (Tasks 13–15) by offline SQLite rewrite, or documented as failed-twice |
| Gap: Jellyfin leak not root-caused | **Still open.** See below |
| Gap: Jellyfin tag unpinned | **Closed** (Task 8) |
| Gap: lingarr unrouted | **Closed** (Task 10), with the conf tracked rather than backed up |
| Gap: bazarr 24 vs 37 | **Was never a gap** — removed (Task 6) |
| Gap: ten dead env vars | **Closed** (Task 6), each proven dead by `git grep` rather than by an unchanged compose model |
| Gap: no off-box backup | **Closed for config**; media remains unbacked, by choice (Tasks 16–17) |
| *New:* invariants unenforced in CI | **Closed** (Task 5) — and the runtime half is now cron'd, since CI cannot see the host |
| *New:* no update notification for jellyfin/qbittorrent | **Opened** (Task 7). Both pinned and unlabelled; needs a version-aware watcher |

## The one gap left open on purpose

**Jellyfin's memory behaviour.** Three mitigations contain it and none fixes it, and no amount of Compose editing will.

Before any heap-dump investigation, try the two cheap things, in this order:

1. **`DOTNET_gcServer=0`.** Server GC in a `mem_limit`-ed container is a well-known cause of apparently unbounded RSS: it sizes heaps per core and returns memory to the OS lazily. Jellyfin's maintainers' position on the long-running "memory leak" reports is that there is no leak — memory is GC-managed, the stated requirement is 8 GB, and most reporters are on low-end hardware.
2. **`MALLOC_ARENA_MAX=2`.** `dotnet/runtime#122027` — verified to exist, titled "High Native Memory Usage Observed on Azure Linux 3.0 with .NET 8.0", opened 2025-11-28, **closed as a duplicate** — is *not* a managed-heap leak. It describes glibc creating 60+ malloc arenas of ~64 MB each in containers while the managed heap stays small, because the allocator sizes itself from visible CPU count. That matches "total memory constantly increasing, while .NET managed heap stays small" better than a leak does.

Rev 1 cited `#122027` as though it were the leak issue. It is a real issue and worth acting on, but it says something different from what rev 1 implied — check what an issue actually says before it goes into an ADR. Measure RSS, the managed heap (`dotnet-counters`), and arena count separately before concluding anything; a heap dump is the third step, not the first.
