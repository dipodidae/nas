# Invariant Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn every "rule you must remember" in this stack into either a rule that is *mechanically impossible to violate silently*, or a rule that no longer needs to exist because its root cause is gone.

**Architecture:** Three moves, in order of value. (1) **Remove the capability to fail** — Watchtower loses the ability to delete containers at all, which retires the single worst failure mode in this repo's history rather than defending against it. (2) **Convert remembered rules into executable assertions** — five invariants are currently only prose in an ADR; they become assertions in `scripts/check-invariants.sh`, which then runs in CI so drift cannot merge. (3) **Close the real gaps** — the two capability waivers, Jellyfin's floating tag, Lingarr's missing route, and the absent off-box backup.

Every task follows the same shape, which is TDD applied to infrastructure: **add the assertion first, watch it fail against the live config, then change the config until it passes.** That ordering is what makes "no regression" checkable rather than hoped for.

**Tech Stack:** Docker Compose v5.5.0 (`include:` + `extends`), Bash + Python 3.11+ (stdlib only in `check-invariants.sh`), GitHub Actions, restic (new), pytest/ruff for the existing suites.

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
- **Capability floors, verified empirically not guessed:** `memswap_limit == mem_limit` wherever `mem_limit` is set; `AUTOHEAL_DEFAULT_STOP_TIMEOUT` ≥ max `stop_grace_period` (currently `2m0s`); `CURL_TIMEOUT` > that.
- **Secrets never enter a compose file, a commit, or this plan.** New credentials go in `.env` (gitignored) and are documented, redacted, in `.env.example`.
- **`.env` variable changes must be reflected in BOTH `.env.example` and `AGENTS.md`'s env list.**
- **Exit-code convention for anything under `scripts/`:** `0` success / `1` partial / `2` fatal; side effects in `main()`, pure logic elsewhere (`AGENTS.md`).
- **Baseline for regression proof:** capture `docker compose config > /tmp/plan-baseline.yml` before Task 1 and diff against it at every phase boundary. Only changes a task explicitly declares are permitted.

---

## Phase map

Ordered by blast radius, ascending. Do not reorder — Phase A makes the later phases verifiable.

| Phase | Tasks | Touches a running container? | Risk |
|---|---|---|---|
| **A — Make the rules executable** | 1–6 | No | None. Config-read + docs + CI only |
| **B — Remove the failure capability** | 7–9 | Recreates `watchtower`, `jellyfin`, `swag` reload | Low |
| **C — Close the capability waivers** | 10–11 | Recreates `playlist-generator{,-db}` | Medium |
| **D — Finish the hardlink migration** | 12–14 | Lidarr DB surgery | **High** |
| **E — Off-box backup** | 15–16 | No | Low (new capability) |

---

## File Structure

| File | Responsibility | Phase |
|---|---|---|
| `scripts/check-invariants.sh` | *Modify.* The single executable definition of every stack invariant. Gains 6 assertions and one new config-file reader. | A |
| `.github/workflows/ci.yml` | *Modify.* Gains an `invariants` job so no violation can merge. | A |
| `README.md` | *Modify.* Corrects the bazarr non-gap; tracks gap closure. | A, and each later phase |
| `docs/decisions/0020-watchtower-monitor-only.md` | *Create.* Records retiring Watchtower's write access. | B |
| `compose/infra.yaml` | *Modify.* `watchtower` → monitor-only. | B |
| `compose/media-serve.yaml` | *Modify.* Pin Jellyfin's tag. | B |
| `.docker-config/swag/nginx/proxy-confs/lingarr.subdomain.conf` | *Create.* Hand-written; SWAG ships no lingarr sample. | B |
| `webapps/playlist-generator/compose.yaml` | *Modify.* Real capability sets for both services. | C |
| `docs/decisions/0018-capability-gaps.md` | *Modify.* Closed, with the measured sets. | C |
| `scripts/lidarr_repath_data.py` | *Create.* The safe single-artist-first repath ADR-0003 demands. | D |
| `scripts/tests/test_lidarr_repath_data.py` | *Create.* Unit tests for its pure logic. | D |
| `docs/decisions/0003-lidarr-data-mount-staged.md` | *Modify.* Outcome recorded either way. | D |
| `scripts/offsite_backup.sh` | *Create.* restic wrapper, dry-run default. | E |
| `.env.example`, `AGENTS.md` | *Modify.* New `RESTIC_*` vars; prune 9 dead vars. | A, E |
| `Makefile` | *Modify.* `backup-offsite`, `verify-runtime`. | A, E |

---

# Phase A — Make the rules executable

No container is touched in this entire phase. It is pure gain and fully revertible.

---

### Task 1: Assert slskd's healthcheck stays Soulseek-independent

ADR-0009 is the most dangerous rule in the repo to forget, and it is currently enforced by nothing but a comment. A future edit to a login-aware healthcheck would create a permanent restart spiral.

**Files:**
- Modify: `scripts/check-invariants.sh`

**Interfaces:**
- Consumes: the existing `services`, `fail()`, `ok()` helpers and the `# ====` section layout already in the script.
- Produces: a new check id `slskd-healthcheck-blind`. Later tasks add checks in this same style; none depend on this one.

- [ ] **Step 1: Write the failing assertion**

Insert as a new numbered section immediately before the `# Report` block in `scripts/check-invariants.sh`:

```python
# ==========================================================================
# 11. slskd's healthcheck must stay Soulseek-INDEPENDENT
# ==========================================================================
# A login-aware healthcheck plus autoheal is a permanent restart spiral: the
# login handshake times out after a hardcoded 5000ms while slsknet holds a
# ghost session, and a restart re-collides with it. ADR-0009.
LOGIN_PROBES = ("isloggedin", "/api/v0/server", "/api/v0/session")
hc = (services.get("slskd", {}).get("healthcheck") or {})
probe = " ".join(str(x) for x in (hc.get("test") or [])).lower()
if not probe:
    fail("slskd-healthcheck-blind", "ADR-0009",
         "slskd has no healthcheck. autoheal then cannot restart it when its "
         "web server actually dies, which is the one case a restart does help.")
elif any(p in probe for p in LOGIN_PROBES):
    hit = [p for p in LOGIN_PROBES if p in probe]
    fail("slskd-healthcheck-blind", "ADR-0009",
         f"slskd's healthcheck probes {hit} -- it must stay liveness-only "
         "(web-UI spider). With autoheal=true on this service, a login-aware "
         "check restarts slskd on every transient logout, and each restart "
         "re-collides with slsknet's ghost session (32->64->128s backoff, "
         "never recovers). The cure is staying DOWN 15-30 min, so an "
         "auto-restart is the exact opposite of it. Login state is watched "
         "alert-only by scripts/slskd_login_watch.py.")
else:
    ok("slskd-healthcheck-blind", "liveness-only")
```

- [ ] **Step 2: Prove the assertion can fail**

```bash
cd ~/nas
cat > compose.override.yaml <<'YAML'
services:
  slskd:
    healthcheck:
      test: [CMD, curl, -f, 'http://localhost:5030/api/v0/server']
YAML
./scripts/check-invariants.sh
```

Expected: `FAIL slskd-healthcheck-blind [ADR-0009] slskd's healthcheck probes ['/api/v0/server'] …`, exit 1.

- [ ] **Step 3: Prove it passes against the real config**

```bash
rm -f compose.override.yaml
./scripts/check-invariants.sh -v | grep slskd-healthcheck-blind
```

Expected: `ok   slskd-healthcheck-blind   liveness-only`.

- [ ] **Step 4: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): assert slskd's healthcheck stays Soulseek-independent

ADR-0009 was enforced by a comment only. A login-aware healthcheck plus
autoheal=true is a permanent restart spiral; this makes that unmergeable.
Verified in both directions with a deliberately login-aware override."
```

---

### Task 2: Derive the autoheal timeout floor from the model instead of trusting a comment

ADR-0010's rule is arithmetic over the live model — `AUTOHEAL_DEFAULT_STOP_TIMEOUT ≥ max(stop_grace_period)` and `CURL_TIMEOUT > AUTOHEAL_DEFAULT_STOP_TIMEOUT` — so it should be computed, not remembered. Today raising qbittorrent's `stop_grace_period` to 180 s would silently invalidate autoheal's 150 s and nothing would say so.

**Files:**
- Modify: `scripts/check-invariants.sh`

**Interfaces:**
- Consumes: `services`, `env_of()`, `fail()`, `ok()`.
- Produces: check id `autoheal-timeouts`.

- [ ] **Step 1: Write the failing assertion**

Append as section 12, before the `# Report` block:

```python
# ==========================================================================
# 12. autoheal's own timeouts must exceed the longest graceful stop
# ==========================================================================
# autoheal ignores compose's stop_grace_period and uses its own stop timeout
# (default 10s). Its restart call blocks for that whole timeout, so a shorter
# CURL_TIMEOUT makes it log a failure and re-issue the restart every
# AUTOHEAL_INTERVAL while the first is still in flight -- measured 2026-09-01
# as three overlapping requests against a restart that succeeded at t+150s.
# ADR-0010.
def _secs(v):
    """Parse compose duration ('2m0s', '90s', '1m') or bare seconds."""
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    total, num = 0, ""
    for ch in s:
        if ch.isdigit():
            num += ch
        elif ch == "m":
            total += int(num or 0) * 60; num = ""
        elif ch == "s":
            total += int(num or 0); num = ""
        else:
            return None
    return total + int(num or 0)

graces = {s: _secs(v["stop_grace_period"])
          for s, v in services.items() if v.get("stop_grace_period")}
worst = max(graces.values()) if graces else 0
worst_svc = max(graces, key=graces.get) if graces else "(none)"

ae = env_of("autoheal")
stop_to = _secs(ae.get("AUTOHEAL_DEFAULT_STOP_TIMEOUT", "10"))
curl_to = _secs(ae.get("CURL_TIMEOUT", "30"))

if stop_to is None or curl_to is None:
    fail("autoheal-timeouts", "ADR-0010",
         "could not parse AUTOHEAL_DEFAULT_STOP_TIMEOUT / CURL_TIMEOUT.")
else:
    if stop_to < worst:
        fail("autoheal-timeouts", "ADR-0010",
             f"AUTOHEAL_DEFAULT_STOP_TIMEOUT={stop_to}s < the longest "
             f"stop_grace_period in the stack ({worst}s, {worst_svc}). autoheal "
             "would SIGKILL mid-flush, which is exactly the ungraceful kill "
             "that orphans qbittorrent's lockfile.")
    elif curl_to <= stop_to:
        fail("autoheal-timeouts", "ADR-0010",
             f"CURL_TIMEOUT={curl_to}s must be strictly greater than "
             f"AUTOHEAL_DEFAULT_STOP_TIMEOUT={stop_to}s, or the restart call is "
             "cut off mid-stop, logged as failed, and re-issued on top of the "
             "one still in flight.")
    else:
        ok("autoheal-timeouts",
           f"stop={stop_to}s > worst grace {worst}s ({worst_svc}); curl={curl_to}s")
```

- [ ] **Step 2: Prove it fails when a grace period outgrows autoheal**

```bash
cd ~/nas
cat > compose.override.yaml <<'YAML'
services:
  qbittorrent:
    stop_grace_period: 300s
YAML
./scripts/check-invariants.sh
```

Expected: `FAIL autoheal-timeouts [ADR-0010] AUTOHEAL_DEFAULT_STOP_TIMEOUT=150s < the longest stop_grace_period in the stack (300s, qbittorrent) …`

- [ ] **Step 3: Prove it also catches an inverted curl timeout**

```bash
cat > compose.override.yaml <<'YAML'
services:
  autoheal:
    environment:
      - CURL_TIMEOUT=100
YAML
./scripts/check-invariants.sh
```

Expected: `FAIL autoheal-timeouts … CURL_TIMEOUT=100s must be strictly greater than AUTOHEAL_DEFAULT_STOP_TIMEOUT=150s …`

- [ ] **Step 4: Prove it passes clean**

```bash
rm -f compose.override.yaml
./scripts/check-invariants.sh -v | grep autoheal-timeouts
```

Expected: `ok   autoheal-timeouts   stop=150s > worst grace 120s (qbittorrent); curl=180s`

- [ ] **Step 5: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): compute autoheal's timeout floor from the live model

ADR-0010's rule is arithmetic over max(stop_grace_period), so derive it rather
than trusting a comment: raising qbittorrent's grace period to 180s used to
silently invalidate autoheal's 150s. Verified in three directions."
```

---

### Task 3: Assert Jellyfin's volume mappings are exactly the two intended mounts

The owner's standing instruction (ADR-0016) is the least self-evident rule here — `${SHARE_DIRECTORY}:/data/movies:ro` looks like a bug, so it is the thing a well-meaning future editor is most likely to "fix". Three systems break together if they do.

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
```

Expected exactly two lines: `…/.docker-config/jellyfin -> /config rw` and `/mnt/drive -> /data/movies ro`.

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
jf = services.get("jellyfin", {})
got = {(v.get("target"), bool(v.get("read_only"))) for v in (jf.get("volumes") or [])}
want = {("/config", False), ("/data/movies", True)}
if got != want:
    missing = sorted(t for t, r in want - got)
    added = sorted(t for t, r in got - want)
    fail("jellyfin-mounts-frozen", "ADR-0016",
         "Jellyfin's volume mappings changed and must not. "
         + (f"missing/altered: {missing}. " if missing else "")
         + (f"unexpected: {added}. " if added else "")
         + "/data/movies must stay a READ-ONLY mount of the whole share. Three "
           "systems are calibrated to it: Jellyfin's library paths, the *arr "
           "mapFrom/mapTo mappings, and playlist-generator's "
           "LOCAL_PATH_PREFIX/JELLYFIN_PATH_PREFIX pair.")
else:
    ok("jellyfin-mounts-frozen", "/config rw + /data/movies ro")
```

- [ ] **Step 3: Prove it fails on the most tempting "fix"**

```bash
cat > compose.override.yaml <<'YAML'
services:
  jellyfin:
    volumes:
      - ${SHARE_DIRECTORY}:/data/media:ro
YAML
./scripts/check-invariants.sh
```

Expected: `FAIL jellyfin-mounts-frozen … unexpected: ['/data/media'] …`

- [ ] **Step 4: Prove it passes clean**

```bash
rm -f compose.override.yaml
./scripts/check-invariants.sh -v | grep jellyfin-mounts
```

Expected: `ok   jellyfin-mounts-frozen   /config rw + /data/movies ro`

- [ ] **Step 5: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): freeze Jellyfin's volume mappings

The owner instruction in ADR-0016 protects the least self-evident config in the
stack -- /data/movies looks misnamed, so it is what someone will 'fix'. Three
systems are calibrated to it and break together."
```

---

### Task 4: Assert the real fix behind ADR-0007, not just its backstop

`mem_limit: 4g` is the *backstop*. The actual fix for qBittorrent's 21.1 GB cgroup peak is `DiskIOReadMode`/`DiskIOWriteMode = DisableOSCache` in `qBittorrent.conf` — and that lives in a file a person can revert through the WebUI, entirely outside the compose model. Confirmed still set today; nothing watches it.

**Files:**
- Modify: `scripts/check-invariants.sh`

**Interfaces:**
- Consumes: `os`, `re`, `fail()`, `ok()`, `warn()`; reads `${CONFIG_DIRECTORY}` from `.env`.
- Produces: check id `qbit-oscache-disabled`.

- [ ] **Step 1: Confirm the current on-disk value**

```bash
cd ~/nas
grep -E "DiskIO(Read|Write)Mode" .docker-config/qbittorrent/qBittorrent/qBittorrent.conf
```

Expected: both `=DisableOSCache`.

- [ ] **Step 2: Write the failing assertion**

Append as section 14. Note this is the first check that reads outside the compose model, so it degrades to a warning when the file is unreachable (as in CI) rather than failing the build:

```python
# ==========================================================================
# 14. qBittorrent's OS-cache bypass is still set (the REAL fix for ADR-0007)
# ==========================================================================
# mem_limit: 4g is only the backstop. libtorrent 2.x mmaps torrent data and the
# kernel keeps those pages as page cache; with OS cache enabled this cgroup
# peaked at 21.1GB (journalctl, 2026-09-01) and contributed to host-wide OOM
# kills. DisableOSCache is the fix -- and it lives in qBittorrent.conf, which a
# person can revert through the WebUI with no trace in this repo. ADR-0007.
def _config_directory():
    for line in open(".env", encoding="utf-8", errors="replace"):
        if line.startswith("CONFIG_DIRECTORY="):
            return line.split("=", 1)[1].strip()
    return None

_cd = _config_directory()
_qconf = os.path.join(_cd, "qbittorrent", "qBittorrent", "qBittorrent.conf") if _cd else None
if not _qconf or not os.path.isfile(_qconf):
    warn("qbit-oscache-disabled", "ADR-0007",
         "qBittorrent.conf not readable from here (expected in CI); could not "
         "verify DiskIOReadMode/DiskIOWriteMode=DisableOSCache. Check on the host.")
else:
    _txt = open(_qconf, encoding="utf-8", errors="replace").read()
    _bad = [k for k in ("DiskIOReadMode", "DiskIOWriteMode")
            if not re.search(rf"^Session\\{k}=DisableOSCache\s*$", _txt, re.M)]
    if _bad:
        fail("qbit-oscache-disabled", "ADR-0007",
             f"qBittorrent.conf has {_bad} not set to DisableOSCache. This is the "
             "actual fix for the 21.1GB cgroup peak; mem_limit 4g is only the "
             "backstop and will now be doing all the work. Fix in the WebUI "
             "(Tools > Options > Advanced > 'Disk IO read/write mode' = "
             "'Disable OS cache'), which qbittorrent persists itself -- editing "
             "qBittorrent.conf under a running qbittorrent gets overwritten.")
    else:
        ok("qbit-oscache-disabled", "both modes DisableOSCache")
```

- [ ] **Step 3: Prove it fails when the setting is reverted**

Work on a copy so the live file is never at risk:

```bash
cd ~/nas
cp .docker-config/qbittorrent/qBittorrent/qBittorrent.conf /tmp/qbt.conf.bak
sed -i 's/^Session\\DiskIOReadMode=DisableOSCache/Session\\DiskIOReadMode=EnableOSCache/' \
  .docker-config/qbittorrent/qBittorrent/qBittorrent.conf
./scripts/check-invariants.sh | grep qbit-oscache
```

Expected: `FAIL qbit-oscache-disabled [ADR-0007] qBittorrent.conf has ['DiskIOReadMode'] not set to DisableOSCache …`

- [ ] **Step 4: Restore and prove it passes**

```bash
cp /tmp/qbt.conf.bak .docker-config/qbittorrent/qBittorrent/qBittorrent.conf
rm -f /tmp/qbt.conf.bak
grep -E "DiskIO(Read|Write)Mode" .docker-config/qbittorrent/qBittorrent/qBittorrent.conf
./scripts/check-invariants.sh -v | grep qbit-oscache
```

Expected: both lines back to `DisableOSCache`, and `ok   qbit-oscache-disabled   both modes DisableOSCache`.

> The live qBittorrent process was never restarted, so this step cannot have disturbed it — the file is only read at startup.

- [ ] **Step 5: Commit**

```bash
git add scripts/check-invariants.sh
git commit -m "feat(check): assert qBittorrent's DisableOSCache, the real ADR-0007 fix

mem_limit 4g is the backstop; DisableOSCache is the fix, and it lives in
qBittorrent.conf where the WebUI can silently revert it. Warns rather than
fails when the file is unreachable, so CI stays green."
```

---

### Task 5: Run the invariant checker in CI

Every assertion added above only protects the server if it runs without a human choosing to run it. The pre-commit hook is bypassable with `--no-verify`; CI is not.

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `Makefile`

**Interfaces:**
- Consumes: `scripts/check-invariants.sh` (exit `0` pass / `1` violation / `2` fatal), and CI's existing "Create minimal .env" step.
- Produces: a CI job named `invariants`, added to the `needs:` list of the existing summary gate.

- [ ] **Step 1: Confirm the checker passes with only CI's fabricated `.env`**

CI builds `.env` from `.env.example`. Reproduce that exactly, in a temp dir, so the live `.env` is untouched:

```bash
cd ~/nas
git stash list >/dev/null   # sanity: we are in a clean tree
mkdir -p /tmp/ci-sim && git archive HEAD | tar -x -C /tmp/ci-sim
cd /tmp/ci-sim
awk 'NF && $0 !~ /CLOUDFLARE_API_TOKEN/ {print}' .env.example > .env
echo 'CLOUDFLARE_API_TOKEN=dummy_token' >> .env
./scripts/check-invariants.sh; echo "exit=$?"
```

Expected: exit `0`. The `cap-drop-all` and `qbit-oscache-disabled` warnings are expected here (Phase C closes the first; the second cannot read the host's `qBittorrent.conf`). If it exits `1`, fix the assertion before wiring CI — a red CI on first run teaches people to ignore CI.

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
      # names the ADR that explains why the rule exists.
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

- [ ] **Step 4: Add a `make verify-runtime` target**

The checker validates *config*. This target validates *reality* — that the running containers match the rules, which config alone cannot prove. Append to `Makefile`, and add `verify-runtime` to the `.PHONY` list:

```makefile
verify-runtime: ## Assert the RUNNING containers match the invariants (not just the config)
	@echo "==> every compose service has a container"
	@missing=0; for s in $$(docker compose config --services); do \
	  docker inspect "$$s" >/dev/null 2>&1 || { echo "    !!! $$s: NO CONTAINER (ADR-0006)"; missing=1; }; \
	done; [ $$missing -eq 0 ] && echo "    all present"
	@echo "==> qbittorrent holds CAP_KILL at runtime (ADR-0004)"
	@docker exec qbittorrent sh -c 'grep ^CapPrm /proc/1/status' \
	  | awk '{ v=strtonum("0x" $$2); if (and(v, 32)) print "    ok: KILL present"; \
	           else { print "    !!! KILL MISSING -- every stop will be a 120s SIGKILL"; exit 1 } }'
	@echo "==> nothing but dockerproxy has the Docker socket (ADR-0013)"
	@bad=$$(docker ps -q | xargs -r docker inspect \
	  --format '{{.Name}} {{range .Mounts}}{{.Source}} {{end}}' \
	  | grep docker.sock | grep -v '^/dockerproxy ' || true); \
	  [ -z "$$bad" ] && echo "    ok: dockerproxy only" || { echo "    !!! $$bad"; exit 1; }
	@echo "==> unhealthy containers"
	@docker compose ps --format '{{.Name}}\t{{.Status}}' | grep -iv healthy | grep -i unhealthy \
	  && exit 1 || echo "    none"
```

- [ ] **Step 5: Run it**

```bash
cd ~/nas && make verify-runtime
```

Expected: `all present`, `ok: KILL present`, `ok: dockerproxy only`, `none`.

- [ ] **Step 6: Commit**

```bash
git add .github/workflows/ci.yml Makefile
git commit -m "ci: gate on the invariant checker, and add make verify-runtime

The pre-commit hook is bypassable with --no-verify; CI is not. Also adds a
runtime counterpart, because config validation cannot prove a container exists
or that qbittorrent actually holds CAP_KILL -- the two failures that hurt most."
```

---

### Task 6: Correct the bazarr non-gap and prune the dead env vars

Two Known-gaps entries are wrong, and a wrong gap list is worse than none — it trains people to skim it.

**Files:**
- Modify: `README.md`
- Modify: `.env.example`
- Modify: `AGENTS.md`

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

- [ ] **Step 3: Delete the nine dead variables from `.env`**

They are referenced by no compose file and no script. Back up first, since `.env` is gitignored and unrecoverable:

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

Expected: exactly those ten lines removed (nine dead plus `API_KEY_LAZYLIBRARIAN`, which was only ever in the example). `PLEX_TOKEN` is **kept** — `scripts/enable_bazarr_plex.py` still reads it.

- [ ] **Step 4: Prove nothing broke**

```bash
make lint && make check
docker compose config > /tmp/after-prune.yml
diff /tmp/plan-baseline.yml /tmp/after-prune.yml && echo "MODEL UNCHANGED"
. .venv/bin/activate && python scripts/test_scripts.py 2>&1 | tail -3
```

Expected: model unchanged, 19/19 smoke tests pass. If the model changed, one of those variables was live after all — restore from `/tmp/env-before-prune.bak` and investigate.

- [ ] **Step 5: Update `.env.example`**

In the "NOT USED ANYWHERE" comment block, change the framing from "were present in `.env`" to "have been removed from `.env`", and remove the now-unnecessary `RYM_SCRAPE_*` / `PLEX_*` template entries higher up the file if any remain. Then reconcile `AGENTS.md`'s env list by deleting the same nine names.

- [ ] **Step 6: Commit**

```bash
git add README.md .env.example AGENTS.md
git commit -m "docs: drop the bazarr non-gap, prune nine dead env vars

Radarr has 37 movies but only 24 with files, and bazarr only tracks movies that
have a file -- so 24 was always correct. Listing it as a gap trained people to
skim the gap list. Explanation moved to the troubleshooting block instead.

Also removes nine variables referenced by no compose file and no script.
PLEX_TOKEN stays: scripts/enable_bazarr_plex.py still reads it. Compose model
verified byte-identical after the prune."
```

---

**Phase A gate.** Before Phase B:

```bash
cd ~/nas
make lint && make check && make verify-runtime
docker compose config > /tmp/phase-a.yml
diff /tmp/plan-baseline.yml /tmp/phase-a.yml && echo "PHASE A WAS A PURE NO-OP ON THE MODEL"
```

The diff **must** be empty. Phase A adds assertions and corrects docs; it changes no service.

---

# Phase B — Remove the capability to fail

---

### Task 7: Take away Watchtower's ability to delete containers

This is the highest-value task in the plan. Rules 3 and the `WATCHTOWER_TIMEOUT` rule, one ADR (0006), one 13-hour outage, one 7-day outage, and six "deliberately unlabeled" annotations all exist to defend against **one** thing: Watchtower's non-atomic stop→remove→create abandoning a container. Watchtower supports `--monitor-only`, which removes that capability entirely.

After this, Watchtower still tells you an update exists — the useful half — and `docker compose up -d` performs the recreate, which is what Compose is actually for and which does not abandon containers.

> **Behaviour change, stated plainly:** 16 services stop auto-updating. That is the point. Updates become deliberate (`make pull` + `make up`, or the weekly target in Task 9). If unattended patching matters more than never losing a container, stop after Task 8 and skip this task — but the incident record in ADR-0006 argues hard against that.

**Files:**
- Create: `docs/decisions/0020-watchtower-monitor-only.md`
- Modify: `compose/infra.yaml`
- Modify: `scripts/check-invariants.sh`
- Modify: `README.md`

**Interfaces:**
- Consumes: `env_of()`, `fail()`, `ok()` in the checker.
- Produces: check id `watchtower-monitor-only`. Task 8's Jellyfin pin is independent of this.

- [ ] **Step 1: Confirm the flag exists in the pinned image**

```bash
docker run --rm --entrypoint /watchtower containrrr/watchtower:latest --help 2>&1 \
  | grep -E "monitor-only|WATCHTOWER_MONITOR_ONLY"
```

Expected: `-m, --monitor-only   Will only monitor for new images, not update the containers`.

- [ ] **Step 2: Write the assertion first — it must fail now**

Append to `scripts/check-invariants.sh` as section 15:

```python
# ==========================================================================
# 15. Watchtower is monitor-only -- it may never write
# ==========================================================================
# Its stop->remove->create is not atomic: when the remove fails it logs
# Failed=1 and moves on WITHOUT creating a replacement, leaving no container
# at all. qbittorrent, 2026-09-01, 13h. monitor-only removes the capability
# rather than defending against it. ADR-0020.
wt_env = env_of("watchtower")
if str(wt_env.get("WATCHTOWER_MONITOR_ONLY", "")).lower() != "true":
    fail("watchtower-monitor-only", "ADR-0020",
         "WATCHTOWER_MONITOR_ONLY is not 'true'. Watchtower can then stop and "
         "remove containers, and its recreate is not atomic -- a failed remove "
         "leaves NO container, which restart: unless-stopped cannot fix and "
         "autoheal cannot heal. It cost 13h of qbittorrent on 2026-09-01 and "
         "7 days on 2026-08-19. Updates belong to `docker compose up -d`.")
else:
    ok("watchtower-monitor-only", "notify-only; compose does the recreating")
```

```bash
./scripts/check-invariants.sh | grep watchtower-monitor-only
```

Expected: `FAIL watchtower-monitor-only [ADR-0020] WATCHTOWER_MONITOR_ONLY is not 'true' …`

- [ ] **Step 3: Write ADR-0020**

Create `docs/decisions/0020-watchtower-monitor-only.md`:

```markdown
# ADR-0020 — Watchtower is monitor-only; Compose does the recreating

**Date:** 2026-09-02
**Status:** accepted
**Amends:** ADR-0006, which defended against this failure per-service

## Decision

`WATCHTOWER_MONITOR_ONLY=true`. Watchtower detects and reports new images. It
never stops, removes or creates a container again.

## Why this supersedes the per-service defence

ADR-0006 handled the non-atomic recreate by opting six services out, one at a
time, each with a comment explaining itself. That defence has three problems:

1. It is opt-*out*, so the dangerous default applies to every new service until
   someone remembers.
2. It only protects the services someone thought to protect. The 16 still
   labelled were all exposed to the same failure — qbittorrent was simply the
   one that drew the short straw.
3. It required a second rule (`WATCHTOWER_TIMEOUT` ≥ the longest
   `stop_grace_period`) that exists solely because Watchtower does the stopping.

Monitor-only removes the capability instead of the exposure. The failure mode
cannot occur, for any service, ever.

## What is lost

Unattended patching. 16 services no longer update themselves at 04:00.

That is an acceptable trade at this scale: one host, one operator, a stack where
losing qBittorrent for 13 hours or 7 days both actually happened, and where
`docker compose up -d` is a one-command recreate that does not abandon
containers.

## What replaces it

- Watchtower still sends its ntfy digest — now "these images have updates"
  rather than "these images were updated".
- `make pull` then `make up` applies everything.
- `make pull-jellyfin` / `make update-qbittorrent` remain for the two services
  that want a watched, one-at-a-time update.
- `scripts/stack_update.sh` (ADR-0020 companion, `make update-all`) does the
  Compose-native update with health verification, for those who want it on a
  schedule.

## What stays true from ADR-0006

The per-service labels stay as they are. They now express "do not even tell me
about updates to this" rather than "do not touch this", which is still
meaningful for the pinned qbittorrent tag and the four locally-built images
Watchtower cannot pull anyway. `scripts/stack_watchdog.py` remains the detector
for a missing container, because Watchtower is no longer the only thing that
could cause one.

`WATCHTOWER_TIMEOUT` also stays. It is inert under monitor-only, but leaving it
costs nothing and it must be correct again the moment anyone reverts this.
```

- [ ] **Step 4: Make the change**

In `compose/infra.yaml`, in the `watchtower` service's `environment:` list, replace the `WATCHTOWER_ROLLING_RESTART` line and its neighbours so the block reads:

```yaml
      - WATCHTOWER_LABEL_ENABLE=true
      - WATCHTOWER_CLEANUP=true
      - WATCHTOWER_INCLUDE_RESTARTING=true
      - WATCHTOWER_ROLLING_RESTART=false
      - WATCHTOWER_REMOVE_VOLUMES=false
      # INVARIANT: monitor-only. Watchtower detects updates and reports them;
      # it must never stop, remove or create a container. Its recreate is not
      # atomic and a failed remove leaves NO container at all -- 13h of
      # qbittorrent on 2026-09-01, 7 days on 2026-08-19. Recreating is
      # `docker compose up -d`'s job. ADR-0020.
      - WATCHTOWER_MONITOR_ONLY=true
```

- [ ] **Step 5: Verify the model changed in exactly one way**

```bash
cd ~/nas
make lint
docker compose config > /tmp/after-t7.yml
diff /tmp/phase-a.yml /tmp/after-t7.yml
```

Expected: exactly one added line, `WATCHTOWER_MONITOR_ONLY: "true"`, under `watchtower`'s environment. Nothing else.

- [ ] **Step 6: Apply and confirm it took effect**

```bash
docker compose up -d watchtower
sleep 20
docker logs watchtower --tail 30 | grep -iE "monitor|only|Watchtower [0-9]"
./scripts/check-invariants.sh | grep -E "watchtower|FAIL" || echo "no failures"
make verify-runtime
```

Expected: the log announces monitor-only mode (Watchtower reports its mode at
startup), the checker's `watchtower-monitor-only` assertion now passes, and
`verify-runtime` still finds every service present.

- [ ] **Step 7: Update `README.md`**

In "Updating services", retitle "Scheduled (16 services)" to "Detected, not applied (16 services)" and replace its body:

```markdown
Watchtower runs on `WATCHTOWER_SCHEDULE` (default `0 0 4 * * *`), checks the 16
labelled containers for newer images, and **reports** what it finds to ntfy. It
is `WATCHTOWER_MONITOR_ONLY=true` and never stops, removes or creates anything:
its recreate is not atomic and a failed remove leaves no container at all. That
capability is gone rather than defended against. → [ADR-0020](docs/decisions/0020-watchtower-monitor-only.md)

Applying an update is `make pull && make up`, or one of the watched
single-service targets below.
```

In the invariants table, replace the `qbittorrent`/`jellyfin` Watchtower-label row with:

```markdown
| Watchtower is `MONITOR_ONLY` | Its recreate is not atomic; a failed remove leaves **no container at all** (13 h, then 7 days). The capability is removed, not defended | [0020](docs/decisions/0020-watchtower-monitor-only.md) |
```

- [ ] **Step 8: Commit**

```bash
git add compose/infra.yaml scripts/check-invariants.sh docs/decisions/0020-watchtower-monitor-only.md README.md
git commit -m "feat(watchtower): monitor-only — remove the capability, not the exposure

Watchtower's non-atomic stop->remove->create is the single worst failure mode in
this repo's history: 13h of qbittorrent on 2026-09-01, 7 days on 2026-08-19.
ADR-0006 defended against it by opting six services out one at a time, which is
opt-OUT (dangerous by default for anything new) and left the other 16 exposed to
the identical failure.

WATCHTOWER_MONITOR_ONLY=true removes the capability instead. Watchtower keeps
the useful half -- it still reports available updates to ntfy -- and
`docker compose up -d` does the recreating, which is what Compose is for and
which does not abandon containers.

Cost: 16 services no longer self-update at 04:00. Stated plainly in ADR-0020.

Assertion written before the change and verified failing, then passing."
```

---

### Task 8: Pin Jellyfin's image tag

With Watchtower now unable to write, an unpinned tag is a smaller risk — but `make pull-jellyfin` still takes whatever `:latest` is that day, which is how you find out about a regression during a film rather than on a Tuesday morning.

**Files:**
- Modify: `compose/media-serve.yaml`
- Modify: `scripts/check-invariants.sh`
- Modify: `README.md`, `docs/decisions/0006-watchtower-opt-outs.md`

- [ ] **Step 1: Find the exact tag currently running**

```bash
docker image inspect lscr.io/linuxserver/jellyfin:latest \
  --format '{{(index .Config.Labels "org.opencontainers.image.version")}}'
docker inspect jellyfin --format 'running: {{.Config.Image}}'
```

Expected at time of writing: `10.11.11ubu2604-ls47`. **Use whatever this command actually prints**, not the value quoted here.

- [ ] **Step 2: Pin it**

In `compose/media-serve.yaml`, replace the jellyfin `image:` line, keeping the comment style used by qbittorrent:

```yaml
    # INVARIANT: tag is PINNED. An update must be chosen, never taken by
    # surprise -- a Jellyfin regression surfaces mid-playback. Bump this
    # deliberately with `make pull-jellyfin`, which waits for healthy.
    # ADR-0006.
    image: lscr.io/linuxserver/jellyfin:10.11.11ubu2604-ls47
```

- [ ] **Step 3: Verify the pin resolves to the same image already running**

This is the no-regression check that matters — the pinned tag must be the digest that is running right now, so applying it is a no-op:

```bash
cd ~/nas
docker compose config | grep -A1 "image: lscr.io/linuxserver/jellyfin"
RUNNING=$(docker inspect jellyfin --format '{{.Image}}')
PINNED=$(docker image inspect "$(docker compose config --format json \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['services']['jellyfin']['image'])")" \
  --format '{{.Id}}')
[ "$RUNNING" = "$PINNED" ] && echo "SAME IMAGE — applying is a no-op" || echo "DIFFERENT — investigate before up -d"
```

Expected: `SAME IMAGE — applying is a no-op`.

- [ ] **Step 4: Add the assertion**

Extend the existing `qbit-tag-pinned` section in `scripts/check-invariants.sh` with a sibling check. Append as section 16:

```python
# ==========================================================================
# 16. Jellyfin's tag is pinned too
# ==========================================================================
# Not for watchtower's sake any more (ADR-0020), but because a Jellyfin
# regression is discovered mid-playback. An update must be chosen. ADR-0006.
jf_img = services.get("jellyfin", {}).get("image", "")
jf_tag = jf_img.rpartition(":")[2] if ":" in jf_img else ""
if not jf_tag or jf_tag == "latest":
    fail("jellyfin-tag-pinned", "ADR-0006",
         f"jellyfin image must be a pinned tag, not '{jf_tag or '<none>'}'. "
         "Bump it deliberately with `make pull-jellyfin`.")
else:
    ok("jellyfin-tag-pinned", jf_tag)
```

- [ ] **Step 5: Verify, then apply**

```bash
make lint && ./scripts/check-invariants.sh | grep -E "jellyfin-tag|FAIL" || echo "clean"
docker compose up -d jellyfin
make verify-runtime
```

Because Step 3 proved the pinned tag is the running image, `up -d` should report jellyfin as already up to date and **not** recreate it. If it does recreate, wait for `healthy` before continuing.

- [ ] **Step 6: Update the docs**

In `README.md`, delete this Known-gaps bullet:

```markdown
- **Jellyfin's tag is not pinned** while qBittorrent's is, so `make
  pull-jellyfin` takes whatever `:latest` is that day. A choice, not an
  oversight. → [ADR-0006](docs/decisions/0006-watchtower-opt-outs.md)
```

In the service reference table, change jellyfin's image cell from `lscr.io/…/jellyfin` to `lscr.io/…/jellyfin **pinned**`. In `docs/decisions/0006-watchtower-opt-outs.md`, replace the paragraph beginning "Note that jellyfin's tag is **not** pinned" with a note that it was pinned on 2026-09-02 and that both slow services now behave identically.

- [ ] **Step 7: Commit**

```bash
git add compose/media-serve.yaml scripts/check-invariants.sh README.md docs/decisions/0006-watchtower-opt-outs.md
git commit -m "feat(jellyfin): pin the image tag

An unpinned tag means make pull-jellyfin takes whatever :latest is that day,
so a regression surfaces mid-playback rather than on a Tuesday. Pinned to the
digest already running, verified identical before applying, so this is a no-op
on the live container. Closes the last unpinned slow-to-stop service."
```

---

### Task 9: Give Lingarr the proxy-conf it never had

`lingarr` carries `swag=enable` but SWAG ships no lingarr sample, so `lingarr.4eva.me` resolves to nothing. The label has been a lie since the service was added.

**Files:**
- Create: `.docker-config/swag/nginx/proxy-confs/lingarr.subdomain.conf`
- Modify: `README.md`

- [ ] **Step 1: Confirm there is no sample to enable**

```bash
docker exec swag sh -c 'ls /defaults/proxy-confs/ | grep -i lingarr' || echo "none shipped — hand-write it"
docker exec swag sh -c 'ls /config/nginx/proxy-confs/ | grep -i lingarr' || echo "none enabled"
```

Both expected to report none.

- [ ] **Step 2: Write the conf, modelled on the enabled `qui` one**

Copy the shape of an existing working conf rather than inventing one:

```bash
cd ~/nas
docker exec swag cat /config/nginx/proxy-confs/qui.subdomain.conf > /tmp/qui-reference.conf
sed -n '1,40p' /tmp/qui-reference.conf
```

Then create `.docker-config/swag/nginx/proxy-confs/lingarr.subdomain.conf`:

```nginx
## Version 2024/07/16
# Hand-written: SWAG ships no lingarr sample. lingarr carried swag=enable with
# no proxy-conf, so lingarr.${PUBLIC_DOMAIN} resolved to nothing.
# Container listens on 8080 internally (published to 127.0.0.1:9876 on the host).

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

- [ ] **Step 3: Validate the nginx config before reloading**

Never reload SWAG on an unvalidated conf — a syntax error takes down every service behind the proxy:

```bash
docker exec swag nginx -t
```

Expected: `syntax is ok` / `test is successful`. If it fails, fix the conf and re-test; **do not proceed to the reload.**

- [ ] **Step 4: Reload and verify**

```bash
docker exec swag nginx -s reload
sleep 3
docker exec swag nginx -t && echo "config still valid after reload"
curl -sk -o /dev/null -w '%{http_code}\n' https://lingarr.4eva.me/
curl -sk -o /dev/null -w 'apex still up: %{http_code}\n' https://4eva.me/
curl -sk -o /dev/null -w 'jellyfin still up: %{http_code}\n' https://jellyfin.4eva.me/
```

Expected: lingarr returns a non-`502` status (`200`, or `302` to its UI), and the two control URLs are unchanged. A reload rather than a restart means no other service dropped a connection.

- [ ] **Step 5: Update `README.md`**

Delete this Known-gaps bullet:

```markdown
- **`lingarr` carries `swag=enable` but has no proxy-conf**, so
  `lingarr.4eva.me` does not resolve to it. Reach it on `127.0.0.1:9876`.
```

Add `lingarr.` to the URL map's *arr row list.

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "fix(swag): route lingarr, which carried swag=enable with no proxy-conf

SWAG ships no lingarr sample, so the label had been a lie since the service was
added -- lingarr.4eva.me resolved to nothing. Hand-written conf modelled on the
enabled qui one, validated with nginx -t before reloading, and the apex plus
jellyfin re-checked after."
```

> The conf itself lives under `.docker-config/`, which is gitignored, so only the doc change is committed. Record the conf's content in the commit message body if you want it recoverable from git; it is otherwise part of the SWAG config backup that `config_backup.py` takes nightly.

---

**Phase B gate.**

```bash
cd ~/nas
make lint && make check && make verify-runtime
docker compose config > /tmp/phase-b.yml
diff /tmp/phase-a.yml /tmp/phase-b.yml
```

Expected diff: exactly two changes — `WATCHTOWER_MONITOR_ONLY: "true"` added, and jellyfin's `image:` tag pinned. Nothing else.

---

# Phase C — Close the capability waivers

ADR-0018's two waivers are the only services in the stack without `cap_drop: ALL`. The reason they were left alone was sound: guessing a database's capability set and finding out at restart is how a database fails to come back. So this phase measures first.

**Measured facts to build on** (gathered 2026-09-02, re-verify in Step 1 of each task):

- `playlist-generator` runs everything as **root** with Docker's full default capability set. `pid 1` is `tini`; the entrypoint runs `htpasswd -cb` to write `/etc/nginx/.htpasswd`, starts `uvicorn` on `127.0.0.1:8000`, then `nginx`. `nginx.conf` declares `user www-data;`, so the master spawns workers as a different uid, and it binds `0.0.0.0:80`.
- `playlist-generator-db` — `pid 1` already runs with **`CapEff: 0000000000000000`**, because the postgres entrypoint drops to uid 999 via `gosu`. Its `PGDATA` is already owned `999:1000`. So the *running* database needs nothing; only the *entrypoint* does.

---

### Task 10: Harden `playlist-generator-db`

Do the database first only because its need is the narrowest and already measured; take a dump anyway.

**Files:**
- Modify: `webapps/playlist-generator/compose.yaml`

**Interfaces:**
- Consumes: the `svc-base` fragment it already extends.
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Re-measure, and take a dump**

```bash
cd ~/nas
docker exec playlist-generator-db sh -c 'grep -E "^Cap(Prm|Eff)" /proc/1/status; ls -ldn /var/lib/postgresql/data'
docker exec playlist-generator-db pg_dump -U playlist -d playlist_generator \
  | gzip > /tmp/plgen-db-$(date +%Y%m%d-%H%M).sql.gz
ls -lh /tmp/plgen-db-*.sql.gz
```

Expected: `CapEff: 0000000000000000` (the running server needs no capabilities) and a non-empty dump. **Do not continue without the dump.**

- [ ] **Step 2: Apply the narrow set**

In `webapps/playlist-generator/compose.yaml`, under `playlist-generator-db`, replace the `# KNOWN GAP` comment with a real capability declaration. Place it directly after the `extends:` block:

```yaml
    # Measured 2026-09-02, not guessed: pid 1 already runs with
    # CapEff 0000000000000000 because the entrypoint gosu's down to uid 999, so
    # the running server needs nothing. These four are what the ENTRYPOINT needs
    # before that hand-off: chown PGDATA (CHOWN), fix modes on files it does not
    # own (FOWNER), and gosu's setuid/setgid pair. ADR-0018.
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - FOWNER
      - SETUID
      - SETGID
```

- [ ] **Step 3: Verify the model, then recreate**

```bash
make lint
docker compose config --format json | python3 -c "
import sys,json;d=json.load(sys.stdin)['services']['playlist-generator-db']
print('cap_drop:',d.get('cap_drop'),'cap_add:',d.get('cap_add'))"
docker compose up -d playlist-generator-db
```

- [ ] **Step 4: Prove the database actually came back**

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

- [ ] **Step 5: Prove the app still reaches it**

```bash
for i in $(seq 1 24); do
  s=$(docker inspect -f '{{.State.Health.Status}}' playlist-generator 2>/dev/null)
  echo "t=$((i*5))s app=$s"; [ "$s" = healthy ] && break; sleep 5
done
curl -s -o /dev/null -w 'health: %{http_code}\n' http://127.0.0.1/health 2>/dev/null \
  || docker exec playlist-generator curl -sf -o /dev/null -w 'health: %{http_code}\n' http://localhost/health
```

Expected: the app returns to `healthy` and `/health` answers `200`. The app depends on the db with `condition: service_healthy`, so a db restart can leave the app sulking — if it does, `docker compose up -d playlist-generator`.

- [ ] **Step 6: Commit**

```bash
git add webapps/playlist-generator/compose.yaml
git commit -m "feat(playlist-generator-db): cap_drop ALL with a measured capability set

Closes half of ADR-0018. Measured rather than guessed: pid 1 already runs with
CapEff 0000000000000000 because the entrypoint gosu's down to uid 999, so the
running server needs nothing. The four granted are what the entrypoint needs
before that hand-off -- chown PGDATA, FOWNER for modes on files it does not own,
and gosu's setuid/setgid pair.

pg_dump taken before the change; verified healthy, queryable, and reachable from
the app afterwards."
```

---

### Task 11: Harden `playlist-generator`

**Files:**
- Modify: `webapps/playlist-generator/compose.yaml`
- Modify: `docs/decisions/0018-capability-gaps.md`
- Modify: `scripts/check-invariants.sh`
- Modify: `README.md`

- [ ] **Step 1: Re-measure what it needs**

```bash
cd ~/nas
docker exec playlist-generator sh -c '
  echo "--- pid1 ---"; cat /proc/1/comm
  echo "--- nginx user ---"; grep -E "^\s*user\s" /etc/nginx/nginx.conf
  echo "--- listeners ---"; ss -tlnp 2>/dev/null | grep -E ":80 |:8000 "
  echo "--- htpasswd owner ---"; ls -ln /etc/nginx/.htpasswd 2>/dev/null'
```

Expected: `tini`; `user www-data;`; nginx on `0.0.0.0:80` and uvicorn on `127.0.0.1:8000`.

That maps to exactly four needs: **`NET_BIND_SERVICE`** (bind :80), **`SETUID` + `SETGID`** (nginx master spawns `www-data` workers), and **`CHOWN`** (nginx's cache/log dirs at startup). `DAC_OVERRIDE` is deliberately *not* granted — everything it touches is root-owned, so root can already write it without bypassing permission checks.

- [ ] **Step 2: Apply the set**

In `webapps/playlist-generator/compose.yaml`, under `playlist-generator`, add directly after the `extends:` block, and delete the trailing `# KNOWN GAP` comment:

```yaml
    # Measured 2026-09-02, not guessed. nginx binds :80 (NET_BIND_SERVICE) and
    # its master spawns `user www-data;` workers (SETUID/SETGID), plus CHOWN for
    # nginx's cache and log dirs at startup. uvicorn stays on 127.0.0.1:8000 and
    # needs nothing. DAC_OVERRIDE deliberately NOT granted: everything this
    # container touches is root-owned, so there are no permission checks to
    # bypass. ADR-0018.
    cap_drop:
      - ALL
    cap_add:
      - CHOWN
      - SETUID
      - SETGID
      - NET_BIND_SERVICE
```

- [ ] **Step 3: Recreate and verify**

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

- [ ] **Step 4: Prove the whole path works, not just the healthcheck**

`/health` is the only unauthenticated path, so also exercise the authenticated one and the proxy:

```bash
docker exec playlist-generator curl -sf -o /dev/null -w 'health: %{http_code}\n' http://localhost/health
curl -sk -o /dev/null -w 'via swag: %{http_code}\n' https://playlist-generator.4eva.me/   # expect 401 (basic auth), not 502
docker exec playlist-generator python -c "
from app.db import engine
with engine.connect() as c: print('db reachable:', c.exec_driver_sql('select 1').scalar())" 2>/dev/null \
  || echo "(app-internal db check unavailable; the healthcheck covers it)"
```

Expected: `health: 200`, `via swag: 401` (auth challenge proves nginx is serving — a `502` would mean the container is not reachable).

**Rollback if anything fails:**

```bash
git checkout webapps/playlist-generator/compose.yaml
docker compose up -d playlist-generator
```

- [ ] **Step 5: Turn the waiver into an assertion**

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
```

Expected: `invariants hold: … 0 warning(s)` for `cap-drop-all` — the only remaining warning should be `qbit-oscache-disabled` if run somewhere that cannot read the host config, and none on the host.

- [ ] **Step 6: Update ADR-0018 and `README.md`**

In `docs/decisions/0018-capability-gaps.md`, change `**Status:** **open — documented, not fixed**` to `**Status:** **closed 2026-09-02** (both sets measured, not guessed)`, and replace the "How to close it" section with the two measured sets and the evidence for each, including the two facts that made it safe: the db's pid 1 already ran at `CapEff 0`, and the app's only privileged need is nginx binding `:80`.

In `README.md`, delete the `playlist-generator` bullet from Known gaps, and in the invariants table change the hardening-baseline row's note from mentioning waivers to `No exceptions`.

- [ ] **Step 7: Commit**

```bash
git add webapps/playlist-generator/compose.yaml scripts/check-invariants.sh docs/decisions/0018-capability-gaps.md README.md
git commit -m "feat(playlist-generator): cap_drop ALL, closing ADR-0018

Every service in the stack now drops ALL capabilities. The set was measured,
not guessed: nginx binds :80 (NET_BIND_SERVICE) and its master spawns
'user www-data;' workers (SETUID/SETGID), plus CHOWN for nginx's cache and log
dirs. DAC_OVERRIDE deliberately withheld -- everything here is root-owned, so
there are no permission checks to bypass.

CAP_DROP_WAIVER is now empty and the generic check enforces both services.
Verified healthy, /health 200, and 401-through-SWAG (not 502) after recreate."
```

---

**Phase C gate.**

```bash
cd ~/nas
make lint && make check && make verify-runtime
./scripts/check-invariants.sh -v | grep -c '  ok'
```

Expected: no failures, and no `cap-drop-all` warnings. Assertion count should now be ~28.

---

# Phase D — Finish the hardlink migration

> **This is the only phase that can lose data.** ADR-0003 records what happened last time: `PUT /api/v1/artist/editor` with `moveFiles: false` emptied `TrackFiles` — **150,187 rows → 0** — and cost ~45 minutes of Lidarr activity to roll back. Read ADR-0003 in full before starting. If you are not prepared to restore a database, **stop after Phase C**; Lidarr copying instead of hardlinking is a disk-space inefficiency, not an outage.

---

### Task 12: Build the repath tool, with its logic under test

The safe method ADR-0003 prescribes is: add the root folder, move **one** artist, verify `TrackFiles` survived for that artist, and only then proceed. That is too fiddly to do by hand 3334 times, so it becomes a script — and the path-rewriting logic gets unit tests, because it is pure and it is the part that corrupts data when wrong.

**Files:**
- Create: `scripts/lidarr_repath_data.py`
- Create: `scripts/tests/test_lidarr_repath_data.py`

**Interfaces:**
- Consumes: `API_KEY_LIDARR` and `CONFIG_DIRECTORY` from `.env`; Lidarr's v1 API at `http://localhost:8686`.
- Produces: `rewrite_path(old: str, old_root: str, new_root: str) -> str`, `plan_repath(artists: list[dict], old_root: str, new_root: str) -> list[dict]`, and `verify_artist_trackfiles(s: requests.Session, host: str, artist_id: int, expected: int) -> bool`. Task 13 calls the CLI, not these functions directly.

- [ ] **Step 1: Write the failing tests**

Create `scripts/tests/test_lidarr_repath_data.py`:

```python
"""Unit tests for the pure path logic in lidarr_repath_data.

The repath itself is destructive (ADR-0003: PUT /api/v1/artist/editor emptied
150,187 TrackFiles rows). These tests cover the part that decides what a path
becomes, because that is the part that corrupts data when it is wrong.
"""
import pytest

from scripts.lidarr_repath_data import plan_repath, rewrite_path


def test_rewrite_path_swaps_only_the_root_prefix():
    assert rewrite_path("/music/Aphex Twin", "/music", "/data/music") == "/data/music/Aphex Twin"


def test_rewrite_path_preserves_nested_structure():
    assert (
        rewrite_path("/music/Boards of Canada/Geogaddi/01.flac", "/music", "/data/music")
        == "/data/music/Boards of Canada/Geogaddi/01.flac"
    )


def test_rewrite_path_is_idempotent_on_already_migrated_paths():
    """Running the tool twice must not produce /data/music/data/music/..."""
    assert rewrite_path("/data/music/Aphex Twin", "/music", "/data/music") == "/data/music/Aphex Twin"


def test_rewrite_path_refuses_a_path_outside_the_old_root():
    """A path that is not under old_root is a bug, not something to guess at."""
    with pytest.raises(ValueError, match="not under"):
        rewrite_path("/downloads/thing", "/music", "/data/music")


def test_rewrite_path_does_not_match_a_sibling_with_a_shared_prefix():
    """/musicvideos must not be treated as living under /music."""
    with pytest.raises(ValueError, match="not under"):
        rewrite_path("/musicvideos/thing", "/music", "/data/music")


def test_rewrite_path_rejects_a_traversal_segment():
    with pytest.raises(ValueError, match="traversal"):
        rewrite_path("/music/../etc/passwd", "/music", "/data/music")


def test_plan_repath_returns_one_entry_per_artist_with_before_and_after():
    artists = [
        {"id": 7, "path": "/music/Aphex Twin", "statistics": {"trackFileCount": 412}},
        {"id": 9, "path": "/music/Autechre", "statistics": {"trackFileCount": 288}},
    ]
    plan = plan_repath(artists, "/music", "/data/music")
    assert plan == [
        {"id": 7, "old": "/music/Aphex Twin", "new": "/data/music/Aphex Twin", "track_files": 412},
        {"id": 9, "old": "/music/Autechre", "new": "/data/music/Autechre", "track_files": 288},
    ]


def test_plan_repath_skips_artists_already_on_the_new_root():
    artists = [
        {"id": 7, "path": "/data/music/Aphex Twin", "statistics": {"trackFileCount": 412}},
        {"id": 9, "path": "/music/Autechre", "statistics": {"trackFileCount": 288}},
    ]
    plan = plan_repath(artists, "/music", "/data/music")
    assert [p["id"] for p in plan] == [9]


def test_plan_repath_orders_smallest_first_so_the_canary_is_cheap():
    """The first artist moved is the canary; it should be the least costly to lose."""
    artists = [
        {"id": 1, "path": "/music/Big", "statistics": {"trackFileCount": 900}},
        {"id": 2, "path": "/music/Small", "statistics": {"trackFileCount": 3}},
    ]
    assert [p["id"] for p in plan_repath(artists, "/music", "/data/music")] == [2, 1]
```

- [ ] **Step 2: Run them and watch them fail**

```bash
cd ~/nas && . .venv/bin/activate
pytest scripts/tests/test_lidarr_repath_data.py -v
```

Expected: collection error, `ModuleNotFoundError: No module named 'scripts.lidarr_repath_data'`.

- [ ] **Step 3: Write the module**

Create `scripts/lidarr_repath_data.py`:

```python
#!/usr/bin/env python3
"""Move Lidarr's root folder from /music to /data/music, one artist at a time.

Why this script exists
----------------------
Lidarr still copies instead of hardlinking because its root folder is /music
while downloads live under /downloads -- separate bind mounts, and hardlinks
cannot cross a mount point (ADR-0002). The fix is to put both under /data.

Sonarr and Radarr were repathed with `PUT /api/v3/.../editor`. The same call
against Lidarr **emptied TrackFiles: 150,187 rows -> 0** (ADR-0003). So this
script does NOT use the editor endpoint. It moves ONE artist, verifies that
artist's TrackFiles survived, and refuses to continue if the count changed.

Safety properties
-----------------
* Dry-run by default. `--apply` is required to write anything.
* Refuses to run unless a fresh lidarr.db backup exists (--require-backup).
* Moves the SMALLEST artist first, so the canary is the cheapest to lose.
* After every artist, re-reads that artist's trackFileCount and aborts on any
  change. `--stop-after N` bounds a first run.
* Never touches files on disk: moveFiles is false throughout and the data is
  already reachable at both paths (both are views of the same share).

Exit codes
----------
  0  success (or dry-run completed)
  1  partial -- some artists moved, then a verification failed and it stopped
  2  fatal -- config missing, API unreachable, or no backup

Usage
-----
  python scripts/lidarr_repath_data.py                        # dry-run plan
  python scripts/lidarr_repath_data.py --apply --stop-after 1  # canary only
  python scripts/lidarr_repath_data.py --apply                 # the rest
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import PurePosixPath

import requests

DEFAULT_HOST = "http://localhost:8686"
OLD_ROOT = "/music"
NEW_ROOT = "/data/music"


def rewrite_path(old: str, old_root: str, new_root: str) -> str:
    """Swap old_root for new_root at the front of `old`.

    Idempotent: a path already under new_root is returned unchanged. Raises
    ValueError for anything not under either root, and for traversal segments --
    guessing at a path is how you write a wrong one into a database.
    """
    if ".." in PurePosixPath(old).parts:
        raise ValueError(f"path traversal segment in {old!r}")
    if old == new_root or old.startswith(new_root.rstrip("/") + "/"):
        return old
    if old != old_root and not old.startswith(old_root.rstrip("/") + "/"):
        raise ValueError(f"{old!r} is not under {old_root!r}")
    suffix = old[len(old_root.rstrip("/")):].lstrip("/")
    return str(PurePosixPath(new_root) / suffix) if suffix else new_root


def plan_repath(artists: list[dict], old_root: str, new_root: str) -> list[dict]:
    """Build the move plan, smallest artist first so the canary is cheap."""
    plan = []
    for a in artists:
        try:
            new = rewrite_path(a["path"], old_root, new_root)
        except ValueError:
            continue
        if new == a["path"]:
            continue
        plan.append({
            "id": a["id"],
            "old": a["path"],
            "new": new,
            "track_files": (a.get("statistics") or {}).get("trackFileCount", 0),
        })
    plan.sort(key=lambda p: (p["track_files"], p["id"]))
    return plan


def _session(api_key: str) -> requests.Session:
    s = requests.Session()
    s.headers["X-Api-Key"] = api_key
    return s


def fetch_artists(s: requests.Session, host: str) -> list[dict]:
    r = s.get(f"{host}/api/v1/artist", timeout=60)
    r.raise_for_status()
    return r.json()


def artist_track_files(s: requests.Session, host: str, artist_id: int) -> int:
    r = s.get(f"{host}/api/v1/artist/{artist_id}", timeout=30)
    r.raise_for_status()
    return (r.json().get("statistics") or {}).get("trackFileCount", 0)


def verify_artist_trackfiles(s: requests.Session, host: str, artist_id: int, expected: int) -> bool:
    """True only if this artist still reports `expected` track files."""
    return artist_track_files(s, host, artist_id) == expected


def move_one_artist(s: requests.Session, host: str, artist_id: int, new_path: str) -> None:
    """Repath a SINGLE artist via PUT /api/v1/artist/{id}, not the editor.

    The editor endpoint (PUT /api/v1/artist/editor) is what emptied TrackFiles
    on 2026-09-01. Do not switch to it. moveFiles is false: both paths are views
    of the same share, so nothing needs to move on disk.
    """
    r = s.get(f"{host}/api/v1/artist/{artist_id}", timeout=30)
    r.raise_for_status()
    body = r.json()
    body["path"] = new_path
    r = s.put(f"{host}/api/v1/artist/{artist_id}?moveFiles=false", json=body, timeout=120)
    r.raise_for_status()


def newest_backup_age_hours(config_directory: str) -> float | None:
    """Age of the newest lidarr.db backup, or None if there is none."""
    candidates = []
    for root, _dirs, files in os.walk(config_directory):
        for f in files:
            if f.startswith("lidarr") and f.endswith((".db", ".db.gz", ".zip")):
                candidates.append(os.path.join(root, f))
    if not candidates:
        return None
    newest = max(candidates, key=os.path.getmtime)
    return (time.time() - os.path.getmtime(newest)) / 3600.0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--host", default=os.getenv("LIDARR_HOST", DEFAULT_HOST))
    p.add_argument("--old-root", default=OLD_ROOT)
    p.add_argument("--new-root", default=NEW_ROOT)
    p.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    p.add_argument("--stop-after", type=int, default=0, help="move at most N artists (0 = all)")
    p.add_argument("--backup-dir", default=os.getenv("LIDARR_BACKUP_DIR", ""),
                   help="directory to check for a fresh lidarr.db backup")
    p.add_argument("--max-backup-age-h", type=float, default=6.0)
    p.add_argument("--settle-seconds", type=float, default=2.0,
                   help="pause after each move before verifying")
    args = p.parse_args(argv)

    api_key = os.getenv("API_KEY_LIDARR")
    if not api_key:
        print("FATAL: API_KEY_LIDARR is not set", file=sys.stderr)
        return 2

    if args.apply:
        if not args.backup_dir:
            print("FATAL: --apply requires --backup-dir so a fresh lidarr.db "
                  "backup can be verified. ADR-0003.", file=sys.stderr)
            return 2
        age = newest_backup_age_hours(args.backup_dir)
        if age is None:
            print(f"FATAL: no lidarr.db backup found under {args.backup_dir}", file=sys.stderr)
            return 2
        if age > args.max_backup_age_h:
            print(f"FATAL: newest lidarr backup is {age:.1f}h old (limit "
                  f"{args.max_backup_age_h}h). Take a fresh one first.", file=sys.stderr)
            return 2
        print(f"backup check ok: newest lidarr.db backup is {age:.1f}h old")

    s = _session(api_key)
    try:
        artists = fetch_artists(s, args.host)
    except requests.RequestException as exc:
        print(f"FATAL: could not read artists from {args.host}: {exc}", file=sys.stderr)
        return 2

    plan = plan_repath(artists, args.old_root, args.new_root)
    total_before = sum((a.get("statistics") or {}).get("trackFileCount", 0) for a in artists)
    print(f"{len(artists)} artists, {total_before} track files total")
    print(f"{len(plan)} need repathing {args.old_root} -> {args.new_root}")

    if args.stop_after:
        plan = plan[: args.stop_after]
        print(f"limited to the first {len(plan)} (smallest first)")

    if not args.apply:
        for item in plan[:10]:
            print(f"  DRY-RUN {item['old']} -> {item['new']} ({item['track_files']} files)")
        if len(plan) > 10:
            print(f"  ... and {len(plan) - 10} more")
        print("dry-run only; pass --apply to write")
        return 0

    moved = 0
    for item in plan:
        print(f"  moving artist {item['id']}: {item['old']} -> {item['new']} "
              f"({item['track_files']} files)")
        try:
            move_one_artist(s, args.host, item["id"], item["new"])
        except requests.RequestException as exc:
            print(f"  ERROR: move failed for artist {item['id']}: {exc}", file=sys.stderr)
            return 1 if moved else 2
        time.sleep(args.settle_seconds)
        if not verify_artist_trackfiles(s, args.host, item["id"], item["track_files"]):
            now = artist_track_files(s, args.host, item["id"])
            print(f"  !!! ABORT: artist {item['id']} trackFileCount changed "
                  f"{item['track_files']} -> {now}. This is the ADR-0003 failure. "
                  "STOP and restore lidarr.db from backup.", file=sys.stderr)
            return 1
        moved += 1
        print(f"  ok: {item['track_files']} track files intact")

    total_after = sum(
        (a.get("statistics") or {}).get("trackFileCount", 0)
        for a in fetch_artists(s, args.host)
    )
    print(f"moved {moved} artists; track files {total_before} -> {total_after}")
    if total_after != total_before:
        print("!!! total track file count CHANGED -- restore from backup", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests until they pass**

```bash
cd ~/nas && . .venv/bin/activate
pytest scripts/tests/test_lidarr_repath_data.py -v
```

Expected: 9 passed.

- [ ] **Step 5: Lint and check the whole suite still passes**

```bash
ruff check scripts
pytest -q scripts/tests
python scripts/test_scripts.py 2>&1 | tail -3
```

Expected: ruff clean, all tests pass (341 = 332 + 9), 19/19 smoke.

- [ ] **Step 6: Confirm dry-run against the live Lidarr is read-only and sane**

```bash
cd ~/nas && . .venv/bin/activate
set -a; . ./.env; set +a
python scripts/lidarr_repath_data.py
```

Expected: reports ~3334 artists and ~147,528 track files, ~3334 needing repath, and a dry-run list starting with the smallest artist. **No writes.** Confirm Lidarr is still healthy: `docker inspect -f '{{.State.Health.Status}}' lidarr`.

- [ ] **Step 7: Commit**

```bash
git add scripts/lidarr_repath_data.py scripts/tests/test_lidarr_repath_data.py
git commit -m "feat(scripts): safe per-artist Lidarr repath tool for ADR-0003

Lidarr still copies instead of hardlinking because its root is /music while
downloads are /downloads -- separate mounts, and link() cannot cross one.

The obvious fix, PUT /api/v1/artist/editor, is what emptied TrackFiles
(150,187 rows -> 0) on 2026-09-01. This tool does the thing ADR-0003 actually
prescribes: PUT /api/v1/artist/{id} one artist at a time, smallest first so the
canary is cheapest, re-reading that artist's trackFileCount after every move and
aborting the moment it changes.

Dry-run by default; --apply refuses to run without a lidarr.db backup less than
6h old. 9 unit tests cover the path logic, including idempotency, the
/musicvideos shared-prefix trap, and traversal rejection -- that is the part
that writes a wrong path into a database.

No live writes in this commit; dry-run verified against the running Lidarr."
```

---

### Task 13: Move one artist, and stop

**Files:** none — this is an operational task, and its output decides whether Task 14 happens at all.

- [ ] **Step 1: Take a dedicated backup and record the baseline**

```bash
cd ~/nas && . .venv/bin/activate
python scripts/config_backup.py --backup-dir /mnt/drive/backups/pre-lidarr-repath --fast --no-checksum
BK=$(ls -dt /mnt/drive/backups/pre-lidarr-repath/* | head -1); echo "backup: $BK"
find "$BK" -name 'lidarr*.db*' -o -name 'lidarr*' -type d | head
set -a; . ./.env; set +a
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://127.0.0.1:8686/api/v1/artist \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('artists:',len(d),'trackfiles:',sum((a.get('statistics') or {}).get('trackFileCount',0) for a in d))" \
  | tee /tmp/lidarr-baseline.txt
```

Record both numbers. Expected roughly `artists: 3334 trackfiles: 147528`.

- [ ] **Step 2: Verify a hardlink across the new paths actually works first**

There is no point repathing if `/data/music` and `/data/downloads` cannot link. Prove the premise before acting on it:

```bash
docker exec lidarr sh -c '
  set -e
  probe=/data/downloads/.lidarr-link-probe
  target=/data/music/.lidarr-link-probe
  echo probe > "$probe"
  ln "$probe" "$target" && echo "HARDLINK OK across /data/downloads -> /data/music"
  rm -f "$probe" "$target"'
```

Expected: `HARDLINK OK`. If it reports `EXDEV`, **stop the whole phase** — the premise is wrong and repathing gains nothing.

- [ ] **Step 3: Add `/data/music` as a Lidarr root folder**

```bash
set -a; . ./.env; set +a
curl -s -X POST -H "X-Api-Key: $API_KEY_LIDARR" -H 'Content-Type: application/json' \
  -d '{"path":"/data/music"}' http://127.0.0.1:8686/api/v1/rootfolder | python3 -m json.tool | head -12
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://127.0.0.1:8686/api/v1/rootfolder \
  | python3 -c "import sys,json;[print(' ',x['path'],'accessible=',x.get('accessible')) for x in json.load(sys.stdin)]"
```

Expected: both `/music` and `/data/music` listed, both `accessible=True`.

- [ ] **Step 4: Move exactly one artist**

```bash
cd ~/nas && . .venv/bin/activate
set -a; . ./.env; set +a
python scripts/lidarr_repath_data.py --apply --stop-after 1 \
  --backup-dir /mnt/drive/backups/pre-lidarr-repath
echo "exit=$?"
```

Expected: `backup check ok`, one `moving artist …`, then `ok: N track files intact`, exit `0`.

**If it prints `ABORT … trackFileCount changed`:** that is the ADR-0003 failure reproducing. Stop immediately and restore:

```bash
docker compose stop lidarr
# restore lidarr.db (+ -wal/-shm) from $BK into ${CONFIG_DIRECTORY}/lidarr/
docker compose up -d lidarr
```

- [ ] **Step 5: Verify the totals did not move**

```bash
set -a; . ./.env; set +a
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://127.0.0.1:8686/api/v1/artist \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('artists:',len(d),'trackfiles:',sum((a.get('statistics') or {}).get('trackFileCount',0) for a in d))"
cat /tmp/lidarr-baseline.txt
```

The `trackfiles` number **must be identical** to the baseline. Also confirm Lidarr's own health and that the moved artist's tracks still resolve on disk:

```bash
docker inspect -f '{{.State.Health.Status}}' lidarr
docker logs lidarr --tail 30 | grep -iE "error|exception" || echo "no errors"
```

- [ ] **Step 6: Let it sit for 24 hours**

Do not proceed to Task 14 in the same sitting. Give a real import cycle a chance to run against the moved artist, then check it hardlinked rather than copied:

```bash
# after at least one import into the moved artist
docker exec lidarr sh -c 'find /data/music/<MovedArtist> -name "*.flac" -o -name "*.mp3" | head -3 | xargs -r stat -c "%h %n"'
```

Expected: link count `%h` of `2` or more on newly imported files. `1` means it still copied — investigate before migrating 3333 more artists.

- [ ] **Step 7: Record the outcome in ADR-0003**

Whichever way it went, write it down. Update `docs/decisions/0003-lidarr-data-mount-staged.md` with a dated "Retry, 2026-09-xx" section: the method used (`PUT /api/v1/artist/{id}`, not the editor), the canary artist and its track count, the before/after totals, and whether hardlinking was confirmed. If it failed, record the failure mode and set the status back to open — a second failure is more valuable written down than a second rollback is.

- [ ] **Step 8: Commit the record**

```bash
git add docs/decisions/0003-lidarr-data-mount-staged.md
git commit -m "docs(adr-0003): record the single-artist Lidarr repath canary

Method, canary artist, before/after track file totals, and whether the import
actually hardlinked afterwards."
```

---

### Task 14: Migrate the rest, in bounded batches

Only if Task 13's canary held for 24 h **and** a real import hardlinked.

**Files:**
- Modify: `docs/decisions/0003-lidarr-data-mount-staged.md`
- Modify: `docs/decisions/0002-single-mount-data-hardlinks.md`
- Modify: `README.md`
- Uses (does not modify): `scripts/lidarr_repath_data.py` from Task 12

**Interfaces:**
- Consumes: the `lidarr_repath_data.py` CLI from Task 12 — `--apply`,
  `--stop-after N`, `--backup-dir DIR`; exit `0` success / `1` partial (moved
  some, then a verification failed) / `2` fatal.
- Produces: nothing other tasks depend on. This is the last task in Phase D.

- [ ] **Step 1: Fresh backup, then a bounded batch**

```bash
cd ~/nas && . .venv/bin/activate
python scripts/config_backup.py --backup-dir /mnt/drive/backups/pre-lidarr-repath --fast --no-checksum
set -a; . ./.env; set +a
python scripts/lidarr_repath_data.py --apply --stop-after 25 \
  --backup-dir /mnt/drive/backups/pre-lidarr-repath
```

Expected: 25 `ok: N track files intact` lines, exit `0`. The script aborts itself on the first count change, so a bad batch stops at one artist.

- [ ] **Step 2: Verify totals and health after the batch**

```bash
set -a; . ./.env; set +a
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://127.0.0.1:8686/api/v1/artist \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('trackfiles:',sum((a.get('statistics') or {}).get('trackFileCount',0) for a in d))"
cat /tmp/lidarr-baseline.txt
docker inspect -f '{{.State.Health.Status}}' lidarr
```

Totals identical, `healthy`. **Stop here for the day.**

- [ ] **Step 3: Repeat in batches until done**

Re-run Steps 1–2, raising `--stop-after` only once several batches have been clean. Between batches, confirm the drip crons are still working — they are the thing most likely to notice a broken Lidarr before you do:

```bash
tail -20 logs/lidarr_backlog_drip.log
tail -20 logs/lidarr_monitor_sweep.log
```

- [ ] **Step 4: Switch the default root folder and remove the old one**

Once `lidarr_repath_data.py` reports `0 need repathing`:

```bash
set -a; . ./.env; set +a
curl -s -H "X-Api-Key: $API_KEY_LIDARR" http://127.0.0.1:8686/api/v1/rootfolder \
  | python3 -c "import sys,json;[print(x['id'],x['path']) for x in json.load(sys.stdin)]"
# delete the now-empty /music root by its id
curl -s -X DELETE -H "X-Api-Key: $API_KEY_LIDARR" http://127.0.0.1:8686/api/v1/rootfolder/<ID_OF_/music>
```

- [ ] **Step 5: Confirm the hardlink win is real**

```bash
docker exec lidarr sh -c 'find /data/music -name "*.flac" | head -50 | xargs -r stat -c "%h" | sort | uniq -c'
df -h /mnt/drive
```

Expected: a meaningful share of link counts ≥ 2, and free space no worse than before.

- [ ] **Step 6: Update ADR-0003, ADR-0002 and `README.md`**

Set ADR-0003's status to `resolved <date>`, record the final totals and the method that worked. In ADR-0002, change the line saying Lidarr is the exception. In `README.md`, delete the "Lidarr still copies instead of hardlinking" Known-gaps bullet, and change the lidarr row in the service table from "`/data` staged but unused" to "`/data` in use". Also update the "Imports are copying instead of hardlinking" troubleshooting block, which currently says Lidarr deliberately still copies.

- [ ] **Step 7: Commit**

```bash
git add docs/decisions/0003-lidarr-data-mount-staged.md docs/decisions/0002-single-mount-data-hardlinks.md README.md
git commit -m "docs: Lidarr is on /data/music and hardlinking — ADR-0003 resolved

Migrated per-artist with scripts/lidarr_repath_data.py in bounded batches, track
file totals verified identical after every batch, hardlinking confirmed by link
count on newly imported files. The editor endpoint that broke this in September
was never used."
```

---

# Phase E — Off-box backup

`config_backup.py` writes 1.5 GB of config to `/mnt/drive/backups/` — the same host, the same box, and for `${CONFIG_DIRECTORY}` (which lives on the OS NVMe) not even a different disk failure domain. A single drive loss takes the config and its backups together.

---

### Task 15: Add a restic wrapper with an explicit, user-supplied destination

> **Decision required from the operator before this task.** The destination and its credentials cannot be invented. Pick one and set the matching `.env` values in Step 2:
> - **B2 / S3 / any rclone remote** — cheapest for ~2 GB, genuinely off-site.
> - **SFTP to another machine you own** — free, off-box, but not off-site.
> - **A USB disk you rotate** — off-box only while unplugged; needs a human.
>
> Steps below are written for restic, which handles all three via its backend URL, and which is chosen over `rsync` because it deduplicates, encrypts client-side, and — most importantly — can *verify* a restore with `restic check --read-data`. An unverified backup is a hope.

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
# restic repository URL. Examples:
#   b2:my-bucket:nas-config          (needs B2_ACCOUNT_ID / B2_ACCOUNT_KEY)
#   sftp:user@otherbox:/backups/nas  (needs working key-based ssh)
RESTIC_REPOSITORY=
# Long random passphrase. LOSING THIS MAKES EVERY SNAPSHOT UNREADABLE --
# store it somewhere that is not this machine.
RESTIC_PASSWORD=
B2_ACCOUNT_ID=
B2_ACCOUNT_KEY=
```

Generate the passphrase and **record it off this machine before continuing**:

```bash
openssl rand -base64 48
```

- [ ] **Step 3: Initialise the repo**

```bash
cd ~/nas && set -a; . ./.env; set +a
restic init
restic snapshots
```

Expected: `created restic repository … at <your repo>`, then an empty snapshot list.

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
# Exit codes:  0 ok  |  1 partial (backup ok, verify or prune failed)  |  2 fatal
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2
set -a; . ./.env 2>/dev/null; set +a

LOCAL_BACKUPS="${LOCAL_BACKUP_DIR:-/mnt/drive/backups/nas-configs}"
DRY_RUN=1
[ "${1:-}" = "--apply" ] && DRY_RUN=0

for v in RESTIC_REPOSITORY RESTIC_PASSWORD; do
  if [ -z "${!v:-}" ]; then
    echo "FATAL: $v is not set in .env" >&2; exit 2
  fi
done

newest=$(ls -dt "$LOCAL_BACKUPS"/*/ 2>/dev/null | head -1)
if [ -z "$newest" ]; then
  echo "FATAL: no local config backup under $LOCAL_BACKUPS -- run config_backup.py first" >&2
  exit 2
fi

age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 3600 ))
echo "newest local backup: $newest (${age_h}h old, $(du -sh "$newest" | cut -f1))"
if [ "$age_h" -gt 48 ]; then
  echo "WARNING: local backup is ${age_h}h old; the nightly config_backup cron may be broken" >&2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: would run: restic backup --tag nas-config $newest"
  restic snapshots --compact 2>/dev/null | tail -5
  echo "pass --apply to actually push"
  exit 0
fi

rc=0
echo "==> backing up"
restic backup --tag nas-config --host "$(hostname)" "$newest" || exit 2

echo "==> pruning (keep 7 daily, 5 weekly, 6 monthly)"
restic forget --tag nas-config --keep-daily 7 --keep-weekly 5 --keep-monthly 6 --prune || rc=1

echo "==> verifying repository integrity"
restic check || rc=1

echo "==> latest snapshots"
restic snapshots --compact --tag nas-config | tail -5
exit $rc
```

```bash
chmod +x scripts/offsite_backup.sh
```

- [ ] **Step 5: Dry-run, then a real run**

```bash
cd ~/nas
./scripts/offsite_backup.sh
./scripts/offsite_backup.sh --apply
echo "exit=$?"
```

Expected: dry-run prints what it would do; `--apply` completes `backup`, `forget`, and `check` with exit `0`.

- [ ] **Step 6: Prove a restore works — the only step that makes this a backup**

```bash
cd ~/nas && set -a; . ./.env; set +a
restic restore latest --target /tmp/restic-restore-test
find /tmp/restic-restore-test -type f | head -10
find /tmp/restic-restore-test -name '*.db' | head -5
du -sh /tmp/restic-restore-test
rm -rf /tmp/restic-restore-test
```

Expected: real config files and at least one `.db` present, total size comparable to the source. **If you cannot restore, you do not have a backup** — fix it before committing.

- [ ] **Step 7: Add the Make target**

Append to `Makefile`, and add `backup-offsite` to `.PHONY`:

```makefile
backup-offsite: ## Push the newest local config backup off this box (restic)
	@scripts/offsite_backup.sh --apply
```

- [ ] **Step 8: Commit**

```bash
git add scripts/offsite_backup.sh Makefile .env.example AGENTS.md
git commit -m "feat(backup): push the config backup off this box with restic

config_backup.py wrote 1.5GB to /mnt/drive/backups/ -- same host, and for
\${CONFIG_DIRECTORY} on the OS NVMe not even a different failure domain. One
drive loss took the config and its backups together.

Backs up the newest config_backup.py output rather than \${CONFIG_DIRECTORY}
directly, so what leaves the box is a quiesced snapshot and not live WAL-mode
SQLite. restic over rsync for dedup, client-side encryption, and restic check
--read-data -- an unverified backup is a hope.

Dry-run default. Restore verified into /tmp before committing."
```

---

### Task 16: Schedule it, and make a silent failure impossible

**Files:**
- Modify: crontab (not version-controlled — record it in `README.md`)
- Modify: `README.md`

- [ ] **Step 1: Add the cron entry**

Wrapped in `cron_job.py` like every other job, so a failure or a stall reports itself to ntfy. Runs at 02:00, an hour after `config_backup.py` at 01:00, so it always has a fresh local snapshot:

```bash
crontab -l > /tmp/crontab.bak
( crontab -l; cat <<'CRON'
0 2 * * * /usr/bin/flock -n /tmp/nas-offsite-backup.lock /usr/bin/env bash -c ". .venv/bin/activate && python scripts/cron_job.py --name offsite-backup --max-age-min 2880 -- bash scripts/offsite_backup.sh --apply >> logs/offsite_backup.log 2>&1"
CRON
) | crontab -
crontab -l | grep offsite
```

Note `--max-age-min 2880` (48 h): `cron_job.py` then raises an alert if the job has not succeeded within two days, which is what turns a silently-broken backup into a notification.

- [ ] **Step 2: Verify the wrapper path works end to end**

```bash
cd /home/tom/nas && . .venv/bin/activate
python scripts/cron_job.py --name offsite-backup-test --max-age-min 2880 -- bash scripts/offsite_backup.sh
echo "exit=$?"
tail -20 logs/offsite_backup.log 2>/dev/null
```

Expected: exit `0`, dry-run output in the log.

- [ ] **Step 3: Confirm the alert path is live**

```bash
. .venv/bin/activate && python scripts/stack_watchdog.py
```

The offsite job should appear in the watchdog's cron freshness view (or at minimum not be reported as stale immediately after a successful run).

- [ ] **Step 4: Update `README.md`**

Delete the last Known-gaps bullet:

```markdown
- **No off-box backup of `${CONFIG_DIRECTORY}`.** `config_backup.py` writes to
  `/mnt/drive/backups/`, which is the same host and the same box.
```

Add to the Scheduled jobs table, in the `daily` row: `· offsite_backup 02:00`. In the monitoring table, add a row:

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

States explicitly that this covers config and not the 4.6T of media, so the
closed gap is not read as more than it is."
```

---

## Final gate

```bash
cd ~/nas
make lint
make check                 # expect: no failures, no warnings
make verify-runtime        # expect: all present, KILL present, dockerproxy only, none unhealthy
./scripts/check-invariants.sh -v | grep -c '  ok'

. .venv/bin/activate
ruff check scripts && python scripts/test_scripts.py 2>&1 | tail -3 && pytest -q scripts/tests

docker compose config > /tmp/final.yml
diff /tmp/plan-baseline.yml /tmp/final.yml
```

The final diff should contain **exactly** these changes and nothing else:

1. `watchtower`: `WATCHTOWER_MONITOR_ONLY: "true"` added (Task 7)
2. `jellyfin`: `image:` tag pinned (Task 8)
3. `bazarr`: nothing — it was already fixed before this plan
4. `playlist-generator-db`: `cap_drop: [ALL]` + 4 `cap_add` (Task 10)
5. `playlist-generator`: `cap_drop: [ALL]` + 4 `cap_add` (Task 11)

Anything else is a regression introduced by this plan. Find it and fix it rather than explaining it.

## Scorecard

| README rule / gap | Outcome |
|---|---|
| qbittorrent keeps `CAP_KILL` | Now asserted at **runtime** too (`make verify-runtime`), not just in config |
| qbittorrent tag pinned ≥ 5.2.2 | Unchanged — already structural. Its *real* companion fix (`DisableOSCache`) is now asserted (Task 4) |
| qbittorrent/jellyfin unlabelled for Watchtower | **Rule retired.** Watchtower can no longer write to any container (Task 7) |
| `memswap_limit == mem_limit` | Unchanged — already asserted |
| slskd healthcheck Soulseek-independent | **Now asserted** (Task 1) |
| autoheal timeouts | **Now computed from the model** (Task 2) |
| No `QBITTORRENT_USER`/`PASS` on the container | Unchanged — already asserted |
| Only dockerproxy has the socket | Now asserted at **runtime** too (Task 5) |
| `/data` mounts | Unchanged — already asserted |
| Jellyfin volume mappings frozen | **Now asserted** (Task 3) |
| Hardening baseline | **No exceptions left** (Tasks 10–11) |
| Gap: playlist-generator capabilities | **Closed** (Tasks 10–11) |
| Gap: Lidarr copies instead of hardlinks | **Closed** (Tasks 12–14), or documented as failed-twice |
| Gap: Jellyfin leak not root-caused | **Still open.** Not fixable by config; mitigations and monitoring stay |
| Gap: Jellyfin tag unpinned | **Closed** (Task 8) |
| Gap: lingarr unrouted | **Closed** (Task 9) |
| Gap: bazarr 24 vs 37 | **Was never a gap** — removed (Task 6) |
| Gap: nine dead env vars | **Closed** (Task 6) |
| Gap: no off-box backup | **Closed for config**; media remains unbacked, by choice (Tasks 15–16) |
| *New:* invariants unenforced in CI | **Closed** (Task 5) |

One gap is deliberately left open: **Jellyfin's memory leak**. Three mitigations
contain it and none fixes it, and no amount of Compose editing will. Closing it
means a heap-dump investigation against `dotnet/runtime#122027` and
`#89776`/`#121455`, which is a different kind of work and deserves its own plan.
