# Phase 1 — Close the Listening Loop (ListenBrainz ↔ Jellyfin) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Jellyfin music library a behavioral intelligence signal it has never had — scrobble plays to ListenBrainz, sync loved tracks, import ListenBrainz "Created for You" playlists back into Jellyfin, and surface loop freshness on the ops dashboard.

**Architecture:** ListenBrainz (hosted MetaBrainz service) is the recommendation brain. The `lyarenei/jellyfin-plugin-listenbrainz` plugin runs inside Jellyfin and handles scrobbling + loved-sync + playlist import. A new best-effort `gather_listenbrainz()` source in the existing `scripts/media_ops_status.py` reads the user's most-recent listen timestamp from the public ListenBrainz API and emits it into `ops-status.json`; `ops.html` renders a "music loop" freshness tile. ListenBrainz health is **informational only** — a stale scrobble must NOT mark the whole stack degraded.

**Tech Stack:** Jellyfin (LSIO `:latest`), jellyfin-plugin-listenbrainz v6.2.0.3, ListenBrainz HTTP API, Python 3.11+ stdlib (`urllib`), pytest, vanilla JS in `ops.html`.

## Global Constraints

- **Jellyfin version floor:** plugin v6.2.0.3 requires **Jellyfin 10.11+**. Confirm before installing (Task 1). If below, a Jellyfin upgrade is a prerequisite — stop and surface it.
- **No new container.** ListenBrainz is the hosted service; the plugin lives inside the existing Jellyfin container. No `docker-compose.yml` service is added in Phase 1.
- **Secrets contract:** the ListenBrainz user token is a `scripts/`-only concern → store as `API_KEY_LISTENBRAINZ` in `.env`, document in BOTH `.env.example` and `AGENTS.md`'s env list (per the repo's two-concerns `.env` rule).
- **Best-effort, never fatal:** `gather_listenbrainz()` follows the `gather_qbittorrent` pattern — any error returns an unreachable status object, never raises, never changes `derive_overall_status`'s verdict.
- **Python script contract:** exit 0/1/2, side effects in `main()`, pure logic testable (per `AGENTS.md`). Match `ruff` + the existing file style.
- **Reversibility:** every step in Tasks 1–5 is reversible (plugin uninstall, token revoke). Note it where relevant.

---

### Task 1: Preflight — confirm Jellyfin version and capture baseline

**Files:** none modified (read-only verification).

- [ ] **Step 1: Confirm Jellyfin version meets the 10.11+ floor**

Run:
```bash
docker exec jellyfin cat /usr/lib/jellyfin/bin/jellyfin.deps.json 2>/dev/null | head -1 || \
curl -fsS http://127.0.0.1:8096/System/Info/Public | python3 -c 'import sys,json;print(json.load(sys.stdin)["Version"])'
```
Expected: a version string `>= 10.11.x`. If `< 10.11`, STOP — Jellyfin upgrade is a prerequisite; do not proceed.

- [ ] **Step 2: Confirm the plugin catalog is reachable from Jellyfin**

Run:
```bash
docker exec jellyfin sh -c 'wget -qO- http://127.0.0.1:8096/health' ; echo
```
Expected: `Healthy`.

- [ ] **Step 3: Record the current plugin list as a rollback baseline**

Run:
```bash
ls -1 "${CONFIG_DIRECTORY:-/mnt/drive/.docker-config}/jellyfin/plugins" 2>/dev/null || echo "(no plugins dir yet)"
```
Expected: a list (possibly empty). Note it — uninstalling the ListenBrainz plugin later means removing the `ListenBrainz_*` folder and restarting.

---

### Task 2: ListenBrainz account + token, wired into the secrets contract

**Files:**
- Modify: `.env` (add `API_KEY_LISTENBRAINZ`)
- Modify: `.env.example`
- Modify: `AGENTS.md` (env list)

**Interfaces:**
- Produces: env var `API_KEY_LISTENBRAINZ` consumed by `gather_listenbrainz()` in Task 6, and the ListenBrainz **user name** (needed for the listens API path).

- [ ] **Step 1: (USER ACTION) Create the ListenBrainz account + token**

This step requires the user — it is their account/credential. Instruct them:
> Create (or sign in to) a ListenBrainz account at https://listenbrainz.org, then copy the **user token** from https://listenbrainz.org/settings/ and note your **user name**.

If the user prefers, they can run this in-session via `! <command>` for nothing here — this is a browser action. Wait for them to provide the token + user name.

- [ ] **Step 2: Add the token to `.env` (do NOT commit `.env`)**

Append to `.env`:
```bash
# ListenBrainz user token (scrobbling + listens API for ops dashboard)
API_KEY_LISTENBRAINZ=<token-from-user>
LISTENBRAINZ_USER=<username-from-user>
```

- [ ] **Step 3: Document the new keys in `.env.example`**

Add to `.env.example` (placeholder values, safe to commit):
```bash
# ListenBrainz user token + username — used by scripts/media_ops_status.py
# (and configured into the Jellyfin ListenBrainz plugin via its UI)
API_KEY_LISTENBRAINZ=
LISTENBRAINZ_USER=
```

- [ ] **Step 4: Document in `AGENTS.md` env list**

Add `API_KEY_LISTENBRAINZ` and `LISTENBRAINZ_USER` to the `API_KEY_*` / env documentation section, one line each describing purpose.

- [ ] **Step 5: Commit (docs only — `.env` stays gitignored)**

```bash
git add .env.example AGENTS.md
git commit -m "docs: document ListenBrainz token env vars"
```

---

### Task 3: Install the ListenBrainz plugin into Jellyfin

**Files:** none in repo (Jellyfin UI + config dir).

**Interfaces:**
- Consumes: Jellyfin 10.11+ confirmed (Task 1).
- Produces: an installed, loaded `ListenBrainz` plugin (verified in Step 4).

- [ ] **Step 1: Get the authoritative plugin-repo manifest URL**

The manifest URL is published in the plugin's GitHub README and has changed across releases — it is the source of truth. Fetch it:
```bash
curl -fsS https://raw.githubusercontent.com/lyarenei/jellyfin-plugin-listenbrainz/master/README.md | grep -iE 'manifest|repo.*json' | head
```
Note the `manifest.json` URL it lists.

- [ ] **Step 2: (UI) Add the plugin repository**

In Jellyfin: Dashboard → Plugins → Repositories → **+** → paste the manifest URL from Step 1, name it `ListenBrainz`. Save.

- [ ] **Step 3: (UI) Install the plugin**

Dashboard → Plugins → Catalog → find **ListenBrainz** → install **v6.2.0.3** (or the latest 6.2.x targeting 10.11). Then restart Jellyfin:
```bash
docker restart jellyfin
```

- [ ] **Step 4: Verify the plugin loaded**

Run (after restart settles ~30s):
```bash
ls -1 "${CONFIG_DIRECTORY:-/mnt/drive/.docker-config}/jellyfin/plugins" | grep -i listenbrainz
docker logs jellyfin 2>&1 | grep -i listenbrainz | tail -5
```
Expected: a `ListenBrainz_*` plugin folder exists and logs show the plugin loading without error. (Rollback: delete that folder + `docker restart jellyfin`.)

---

### Task 4: Configure scrobbling, loved-sync, and playlist import

**Files:** none in repo (Jellyfin UI).

**Interfaces:**
- Consumes: installed plugin (Task 3), user token (Task 2).

- [ ] **Step 1: (UI) Link the Jellyfin user to ListenBrainz**

Dashboard → Plugins → ListenBrainz → select your Jellyfin user → paste the `API_KEY_LISTENBRAINZ` token → **Save**. The plugin should confirm the token validates against ListenBrainz.

- [ ] **Step 2: (UI) Enable the loop features**

In the same panel enable:
- Scrobbling (submit listens) — ON
- "Favorite/loved" sync (Jellyfin favorites ↔ ListenBrainz loved) — ON
- Playlist sync / import of ListenBrainz "Created for You" playlists (Weekly Jams / Weekly Exploration / Daily Jams) — ON, if exposed in v6.2.x.

Save.

- [ ] **Step 3: Verify configuration persisted**

Run:
```bash
find "${CONFIG_DIRECTORY:-/mnt/drive/.docker-config}/jellyfin" -iname '*listenbrainz*.xml' -exec grep -li 'token\|scrobble' {} \; 2>/dev/null
```
Expected: a plugin config XML containing the user binding (token stored hashed/encoded by the plugin — presence of the config file is the check, not the token value).

---

### Task 5: Verify the round-trip (the loop actually closes)

**Files:** none.

**Interfaces:**
- Consumes: configured plugin (Task 4), `LISTENBRAINZ_USER` (Task 2).

- [ ] **Step 1: Capture the current latest-listen timestamp as a baseline**

Run:
```bash
source .env
curl -fsS "https://api.listenbrainz.org/1/user/${LISTENBRAINZ_USER}/listens?count=1" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin)["payload"]["listens"];print(d[0]["listened_at"] if d else "none")'
```
Note the value (epoch or `none`).

- [ ] **Step 2: (USER ACTION) Play a track in Jellyfin to completion (or past the scrobble threshold ~50%/4min)**

Tell the user: play one full track from the Jellyfin music library on any client.

- [ ] **Step 3: Verify a new scrobble landed (allow ~1–2 min)**

Re-run the Step 1 command. Expected: a **newer** `listened_at` than the baseline. PASS = the scrobble half of the loop works. (If unchanged after 2 min: check `docker logs jellyfin | grep -i listenbrainz` for submission errors — common cause is an invalid token or the play not crossing the scrobble threshold.)

- [ ] **Step 4: Verify playlist import (allow up to the plugin's sync interval, or trigger a manual sync if available)**

In Jellyfin, look under Playlists for ListenBrainz-sourced playlists (e.g. "Weekly Jams"). PASS = at least one appears. (These regenerate on ListenBrainz's weekly/daily cadence; if your account is brand-new, ListenBrainz may need listening history before it generates them — note this as expected, not a failure.)

---

### Task 6: Ops-dashboard "music loop" freshness tile

**Files:**
- Modify: `scripts/media_ops_status.py` (add dataclass + `gather_listenbrainz` + report-dict wiring + summary line)
- Modify: `scripts/tests/test_media_ops_status.py` (new tests)
- Modify: `webapps/4eva-rootpage/src/ops.html` (render the tile)

**Interfaces:**
- Consumes: `API_KEY_LISTENBRAINZ`, `LISTENBRAINZ_USER` env vars (Task 2).
- Produces: a `listenbrainz` key in `ops-status.json` of shape
  `{"reachable": bool, "user": str|null, "last_listen_epoch": int|null, "last_listen_age_s": float|null, "error": str|null}`.

- [ ] **Step 1: Write the failing test for the dataclass + gather function**

Add to `scripts/tests/test_media_ops_status.py`:
```python
def test_gather_listenbrainz_parses_latest_listen(monkeypatch):
    import scripts.media_ops_status as m

    payload = {"payload": {"listens": [{"listened_at": 1_700_000_000}]}}

    def fake_get(url, headers):
        assert "user/tom/listens" in url
        return 200, json.dumps(payload)

    monkeypatch.setattr(m, "_http_get", fake_get)
    st = m.gather_listenbrainz(user="tom", token="tok", now_epoch=1_700_000_060.0)
    assert st.reachable is True
    assert st.last_listen_epoch == 1_700_000_000
    assert st.last_listen_age_s == 60.0


def test_gather_listenbrainz_no_user_is_unreachable(monkeypatch):
    import scripts.media_ops_status as m
    st = m.gather_listenbrainz(user=None, token=None, now_epoch=1.0)
    assert st.reachable is False
    assert "not set" in (st.error or "")


def test_gather_listenbrainz_http_error_never_raises(monkeypatch):
    import scripts.media_ops_status as m

    def boom(url, headers):
        raise OSError("network down")

    monkeypatch.setattr(m, "_http_get", boom)
    st = m.gather_listenbrainz(user="tom", token="tok", now_epoch=1.0)
    assert st.reachable is False
    assert st.error
```
(`json` is already imported at the top of the test module; if not, add `import json`.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `. .venv/bin/activate && pytest scripts/tests/test_media_ops_status.py -k listenbrainz -v`
Expected: FAIL with `AttributeError: module ... has no attribute 'gather_listenbrainz'`.

- [ ] **Step 3: Add the dataclass**

In `scripts/media_ops_status.py`, alongside the other status dataclasses (after `SlskdStatus`):
```python
@dataclass
class ListenBrainzStatus:
    """ListenBrainz loop freshness (informational — never degrades overall)."""

    reachable: bool
    user: str | None = None
    last_listen_epoch: int | None = None
    last_listen_age_s: float | None = None
    error: str | None = None
```

- [ ] **Step 4: Implement `gather_listenbrainz`**

Add near `gather_slskd`:
```python
def gather_listenbrainz(
    user: str | None,
    token: str | None,
    now_epoch: float,
) -> ListenBrainzStatus:
    """Read the user's most-recent listen timestamp from the public LB API.

    Best-effort: any failure returns reachable=False and never raises.
    """
    if not user:
        return ListenBrainzStatus(
            reachable=False, error="LISTENBRAINZ_USER not set"
        )
    url = f"https://api.listenbrainz.org/1/user/{user}/listens?count=1"
    headers = {"Authorization": f"Token {token}"} if token else {}
    try:
        status, body = _http_get(url, headers)
        if status != 200:
            return ListenBrainzStatus(
                reachable=False, user=user, error=f"HTTP {status}"
            )
        listens = json.loads(body)["payload"]["listens"] if body else []
        if not listens:
            return ListenBrainzStatus(reachable=True, user=user)
        epoch = int(listens[0]["listened_at"])
        return ListenBrainzStatus(
            reachable=True,
            user=user,
            last_listen_epoch=epoch,
            last_listen_age_s=max(0.0, now_epoch - epoch),
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return ListenBrainzStatus(reachable=False, user=user, error=str(exc))
    except (ValueError, KeyError) as exc:
        return ListenBrainzStatus(
            reachable=False, user=user, error=f"parse error: {exc}"
        )
```
Note: `_http_get` must reach the public internet here (unlike the localhost sources) — that is expected and fine.

- [ ] **Step 5: Run the gather tests to verify they pass**

Run: `. .venv/bin/activate && pytest scripts/tests/test_media_ops_status.py -k listenbrainz -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Wire it into the report — failing test first**

Add to the test file:
```python
def test_report_dict_includes_listenbrainz():
    import scripts.media_ops_status as m
    report = m.OpsReport(
        generated_at="2026-06-19T00:00:00Z",
        overall="ok",
        containers=[],
        arr_services=[],
        slskd=None,
        qbittorrent=None,
        logs=[],
        listenbrainz=m.ListenBrainzStatus(reachable=True, user="tom", last_listen_age_s=42.0),
    )
    d = m._report_to_dict(report)
    assert d["listenbrainz"]["user"] == "tom"
    assert d["listenbrainz"]["last_listen_age_s"] == 42.0
```

- [ ] **Step 7: Run it to verify it fails**

Run: `. .venv/bin/activate && pytest scripts/tests/test_media_ops_status.py -k report_dict_includes_listenbrainz -v`
Expected: FAIL (`OpsReport.__init__` got unexpected keyword `listenbrainz`, or `KeyError: 'listenbrainz'`).

- [ ] **Step 8: Add the `listenbrainz` field to `OpsReport` and `_report_to_dict`**

In the `OpsReport` dataclass add a field (default `None` so existing call sites stay valid):
```python
    listenbrainz: ListenBrainzStatus | None = None
```
In `_report_to_dict`, add to the returned dict:
```python
        "listenbrainz": asdict(report.listenbrainz) if report.listenbrainz else None,
```

- [ ] **Step 9: Call `gather_listenbrainz` in `main()` and add a summary line**

In `main()`, where the other `gather_*` calls assemble the report, add:
```python
    lb = gather_listenbrainz(
        user=os.environ.get("LISTENBRAINZ_USER"),
        token=os.environ.get("API_KEY_LISTENBRAINZ"),
        now_epoch=now_epoch,
    )
```
and pass `listenbrainz=lb` into the `OpsReport(...)` construction. In `format_summary`, add one informational line (mirroring the slskd line style) showing user + last-listen age (e.g. `music loop: tom · last scrobble 3m ago` / `· no listens yet` / `· unreachable`). Do NOT feed `lb` into `derive_overall_status` — it is informational only.

- [ ] **Step 10: Run the full ops test module**

Run: `. .venv/bin/activate && pytest scripts/tests/test_media_ops_status.py -v`
Expected: all PASS (existing + new). Then `ruff check scripts/media_ops_status.py` → clean.

- [ ] **Step 11: Render the tile in `ops.html`**

In `webapps/4eva-rootpage/src/ops.html`'s `render(d)` function, near the downloaders block, add a "music loop" row driven by `d.listenbrainz`:
```js
        // music loop (ListenBrainz)
        const lb = d.listenbrainz || {}
        const lbAge = lb.last_listen_age_s != null
          ? (lb.last_listen_age_s < 3600
              ? Math.round(lb.last_listen_age_s / 60) + 'm ago'
              : Math.round(lb.last_listen_age_s / 3600) + 'h ago')
          : (lb.reachable ? 'no listens yet' : esc(lb.error || 'unreachable'))
        $('downloaders').insertAdjacentHTML('beforeend',
          `<div class="row">${dot(lb.reachable ? 'ok' : 'warn')}<div class="name">music loop<div class="sub">${lb.user ? esc(lb.user) + ' · ' : ''}last scrobble ${lbAge}</div></div></div>`)
```
(If `ops.html` has a dedicated section id better suited than `downloaders`, use it; match the existing `dot()`/`esc()`/`.row`/`.name`/`.sub` markup conventions already in the file.)

- [ ] **Step 12: Validate the inline JS, then deploy via image rebuild**

`webapps/4eva-rootpage` is a **static nginx image** (no vite/pnpm build) — the
Dockerfile `COPY src/ /usr/share/nginx/html/`. Validate the edited inline
script parses, then deploy by rebuilding the container:
```bash
# syntax-check the inline <script> block
python3 - <<'PY'
import re; h=open('webapps/4eva-rootpage/src/ops.html').read()
open('/tmp/ops_script.js','w').write(re.search(r'<script>(.*?)</script>', h, re.S).group(1))
PY
node --check /tmp/ops_script.js && echo "JS OK"
# deploy (live action — confirm with owner first)
docker compose up -d --build 4eva-rootpage
```
Expected: `JS OK`, then the container rebuilds and serves the updated `ops.html`.

- [ ] **Step 13: End-to-end smoke against the live API**

Run:
```bash
source .env && python scripts/media_ops_status.py --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["listenbrainz"])'
```
Expected: a `listenbrainz` object with `reachable: true` and (if Task 5 succeeded) a recent `last_listen_age_s`.

- [ ] **Step 14: Commit**

```bash
git add scripts/media_ops_status.py scripts/tests/test_media_ops_status.py webapps/4eva-rootpage/src/ops.html
git commit -m "feat(ops): ListenBrainz music-loop freshness tile on ops dashboard"
```

---

## Self-Review

**Spec coverage (Phase 1 of the design doc):**
- ListenBrainz account → Task 2. ✓
- `jellyfin-plugin-listenbrainz` deploy + Jellyfin 10.11+ gate → Tasks 1, 3. ✓
- Scrobble → loved-sync → playlist-import round-trip verified → Tasks 4, 5. ✓
- "Loop freshness" on `ops.html` → Task 6 (success criterion #5). ✓
- Reversibility / no new container (Global Constraints) → honored throughout. ✓

**Placeholder scan:** No "TBD"/"add error handling"/"similar to" placeholders. The one deliberately-late-bound value is the plugin manifest URL (Task 3 Step 1) — fetched from the authoritative README at install time because it changes across releases; this is correct, not a placeholder. User-action steps (Task 2 Step 1, Task 5 Step 2) are explicitly marked because they require human credentials/playback.

**Type consistency:** `ListenBrainzStatus` fields (`reachable`, `user`, `last_listen_epoch`, `last_listen_age_s`, `error`) are used identically in the dataclass (Step 3), `gather_listenbrainz` (Step 4), tests (Steps 1, 6), `_report_to_dict` (Step 8), and the `ops.html` render (Step 11). `gather_listenbrainz(user, token, now_epoch)` signature matches all three test call sites. `OpsReport(..., listenbrainz=...)` matches the new field added in Step 8.
