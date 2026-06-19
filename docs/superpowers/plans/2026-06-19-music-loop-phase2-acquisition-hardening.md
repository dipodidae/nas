# Phase 2 — Acquisition Hardening (Tubifarry 2.1.1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. This is an **operational runbook** (plugin/config + staged verification), not a code-with-unit-tests plan — the "test cycle" is controlled live verification with an explicit rollback at each gate.

**Goal:** Upgrade the Tubifarry Lidarr plugin from 2.1.0 → 2.1.1 to gain its plugin-side slskd flood control (outbound-search semaphore + `MaxGrabsPerUser`), layering defence-in-depth on top of the existing local ban fixes — without regressing the acquisition path or re-triggering slskd search-flood bans.

**Architecture:** Tubifarry is a Lidarr **nightly** plugin (files at `${CONFIG_DIRECTORY}/lidarr/plugins/TypNull/Tubifarry/`); its runtime settings live in the **Lidarr DB** as indexer/download-client config, not in the repo. The upgrade is a plugin-file swap (via Lidarr's plugin UI) + a Lidarr restart + verification that the existing flood-control settings survive and the new caps are set. slskd (v0.25.1.0, shares gluetun's netns) is downstream and is only *observed* here — no slskd change.

**Tech Stack:** Lidarr `:nightly`, Tubifarry plugin v2.1.1 (requires .NET 8 + Lidarr ≥ 3.1.3.0), slskd v0.25.1.0, byparr (FlareSolverr-compatible) fronting CF indexers.

## Global Constraints

- **Verified-already-done — DO NOT touch:** slskd is on **v0.25.1.0** with the migrated `transfers:`/`integrations:` schema. No slskd config migration. No enabling of `integrations.vpn.gluetun` (N/A for AirVPN static port 37020).
- **Preserve the existing flood-control settings (live in Lidarr DB, indexer id 4):** `useFallbackSearch=False` and `useTrackFallback=False` MUST remain False after the upgrade (per the search-flood ban history — these exploded 1 AlbumSearch into 4–15 near-dup queries). Re-verify post-upgrade.
- **Respect the slskd login-timeout / ghost-session gotcha:** do NOT restart slskd as part of this. Only Lidarr restarts here. If slskd ever shows the 5s-login-hang pattern, the cure is leaving it DOWN 15–30 min, never a restart loop.
- **Watch these 2.1.1-era issues (both touch THIS stack's byparr-fronted CF indexer path):** #200 (Lidarr fails to parse JSON from Flaresolverr/Byparr) and #199 (lyrics enhancer triggers an FFmpeg-binary re-download loop in Docker). Verification steps below explicitly check for both.
- **Rollback is mandatory and pre-staged:** a copy of the working 2.1.0 plugin dir is taken in Task 1 before any change; every gate's failure path is "restore 2.1.0 + restart Lidarr".
- **Lidarr port/paths:** Lidarr WebUI `127.0.0.1:8686`, API `/api/v1`, key `API_KEY_LIDARR` in `.env`. Plugin dir: `${CONFIG_DIRECTORY}/lidarr/plugins/TypNull/Tubifarry/` (`CONFIG_DIRECTORY=/home/tom/nas/.docker-config`).

---

### Task 1: Preflight — baseline, backup, and capture rollback point

**Files:** none in repo; operates on the live Lidarr config dir.

- [ ] **Step 1: Confirm current Tubifarry version is 2.1.0**

Run:
```bash
grep -m1 'Lidarr.Plugin.Tubifarry/' \
  "${CONFIG_DIRECTORY:-/home/tom/nas/.docker-config}/lidarr/plugins/TypNull/Tubifarry/Lidarr.Plugin.Tubifarry.deps.json"
```
Expected: `"Lidarr.Plugin.Tubifarry/2.1.0"`.

- [ ] **Step 2: Confirm Lidarr version meets the plugin floor (≥ 3.1.3.0)**

Run:
```bash
source .env
curl -fsS -H "X-Api-Key: ${API_KEY_LIDARR}" http://127.0.0.1:8686/api/v1/system/status \
  | python3 -c 'import sys,json;print("lidarr",json.load(sys.stdin)["version"])'
```
Expected: version `>= 3.1.3.x`. If below, STOP — upgrade Lidarr nightly first.

- [ ] **Step 3: Snapshot the current flood-control indexer settings (rollback reference)**

Run:
```bash
source .env
curl -fsS -H "X-Api-Key: ${API_KEY_LIDARR}" http://127.0.0.1:8686/api/v1/indexer \
  | python3 -c 'import sys,json;
d=json.load(sys.stdin)
for i in d:
    if "tubifarry" in (i.get("implementation","")+i.get("name","")).lower() or i.get("id")==4:
        fields={f["name"]:f.get("value") for f in i.get("fields",[])}
        print("id",i["id"],i.get("name"),"useFallbackSearch=",fields.get("useFallbackSearch"),"useTrackFallback=",fields.get("useTrackFallback"))' \
  | tee /tmp/tubifarry-indexer-baseline.txt
```
Expected: the Tubifarry indexer (id 4) with `useFallbackSearch= False` and `useTrackFallback= False`. Note them — these must be unchanged after upgrade.

- [ ] **Step 4: Back up the working 2.1.0 plugin directory (the rollback artifact)**

⚠️ **CRITICAL: the backup MUST live OUTSIDE the `plugins/` tree.** Lidarr scans
the *entire* `plugins/` directory and loads every assembly it finds — a backup
copy left under `plugins/` produces a SECOND `Lidarr.Plugin.Tubifarry.dll`, two
`SlskdIndexer` types, and `FindTypeByName` throws *"Sequence contains more than
one matching element"* → IndexerFactory/MetadataFactory/DownloadClientFactory all
fail and `/api/v1/indexer` returns HTTP 500. (Learned the hard way 2026-06-19.)

Run:
```bash
CFG="${CONFIG_DIRECTORY:-/home/tom/nas/.docker-config}"
mkdir -p "$CFG/lidarr/.plugin-backups"
cp -a "$CFG/lidarr/plugins/TypNull/Tubifarry" "$CFG/lidarr/.plugin-backups/Tubifarry.2.1.0.bak"
ls -la "$CFG/lidarr/.plugin-backups/Tubifarry.2.1.0.bak/Lidarr.Plugin.Tubifarry.dll"
```
Expected: the `.bak` dir exists with the DLL, **outside** `plugins/`. (Rollback at
any later gate = `rm -rf $CFG/lidarr/plugins/TypNull/Tubifarry && cp -a $CFG/lidarr/.plugin-backups/Tubifarry.2.1.0.bak $CFG/lidarr/plugins/TypNull/Tubifarry && docker restart lidarr`.)

- [ ] **Step 5: Capture a slskd ban/flood baseline from recent logs**

Run:
```bash
docker logs slskd --since 2h 2>&1 | grep -iE 'banned|too many|quickly repeat|rate' | tail -20 || echo "(no recent ban lines — good baseline)"
```
Note whether any flood/ban lines already exist, so post-upgrade comparison is meaningful.

---

### Task 2: Upgrade Tubifarry to 2.1.1

**Files:** none in repo; Lidarr plugin UI + config dir.

**Interfaces:**
- Consumes: 2.1.0 backup (Task 1 Step 4), Lidarr ≥ 3.1.3.0 (Task 1 Step 2).
- Produces: Tubifarry 2.1.1 loaded, indexer settings intact (verified Task 3).

- [ ] **Step 1: Update the plugin (UI or API)**

UI path: System → Plugins → Tubifarry → **Update**.

API path (verified working 2026-06-19) — the install is a **command**, NOT a POST
to `/system/plugins/install` (that returns HTTP 405):
```bash
LK=$(grep -E '^API_KEY_LIDARR=' .env | cut -d= -f2-)
curl -fsS -X POST -H "X-Api-Key: $LK" -H 'Content-Type: application/json' \
  -d '{"name":"InstallPlugin","githubUrl":"https://github.com/TypNull/Tubifarry"}' \
  http://127.0.0.1:8686/api/v1/command          # → HTTP 201
# poll until the InstallPlugin command status == "completed", then restart
```
Expected: command completes, `plugins/TypNull/Tubifarry/Lidarr.Plugin.Tubifarry.deps.json` shows `2.1.1`.

- [ ] **Step 2: Restart Lidarr to load the new plugin assembly**

Run:
```bash
docker restart lidarr
```
(Only Lidarr — never slskd.) Wait ~45s for it to come up.

- [ ] **Step 3: Verify 2.1.1 is the loaded version**

Run:
```bash
grep -m1 'Lidarr.Plugin.Tubifarry/' \
  "${CONFIG_DIRECTORY:-/home/tom/nas/.docker-config}/lidarr/plugins/TypNull/Tubifarry/Lidarr.Plugin.Tubifarry.deps.json"
docker logs lidarr --since 2m 2>&1 | grep -iE 'tubifarry|plugin' | tail -10
```
Expected: `"Lidarr.Plugin.Tubifarry/2.1.1"` and logs show the plugin loading **without an assembly/version error**. If it fails to load → rollback (Task 1 Step 4 note) and STOP.

---

### Task 3: Verify flood-control settings survived + set the new caps

**Files:** none in repo; Lidarr UI/DB.

**Interfaces:**
- Consumes: baseline from Task 1 Step 3.

- [ ] **Step 1: Re-check the indexer flood-control flags are still False**

Run the same command as Task 1 Step 3, comparing to the baseline:
```bash
source .env
curl -fsS -H "X-Api-Key: ${API_KEY_LIDARR}" http://127.0.0.1:8686/api/v1/indexer \
  | python3 -c 'import sys,json;
d=json.load(sys.stdin)
for i in d:
    if "tubifarry" in (i.get("implementation","")+i.get("name","")).lower() or i.get("id")==4:
        f={x["name"]:x.get("value") for x in i.get("fields",[])}
        print("useFallbackSearch=",f.get("useFallbackSearch"),"useTrackFallback=",f.get("useTrackFallback"))'
```
Expected: both `False`, matching `/tmp/tubifarry-indexer-baseline.txt`. If the upgrade reset either to True → set it back to False in the Lidarr indexer UI (id 4) and save. This is the #1 regression risk.

- [ ] **Step 2: (UI) Set the new 2.1.1 flood-control caps**

In the Tubifarry indexer/download-client settings, set the new flood-control fields introduced in 2.1.0/2.1.1:
- `MaxGrabsPerUser` — set to a small bound (e.g. **2**) so a single Soulseek peer can't be hammered.
- The slskd outbound-search **semaphore / concurrency cap** (2.1.1) — leave at its conservative default unless the UI exposes a higher value; the point is it is now *active*. Note the value you chose.

Save.

- [ ] **Step 3: Confirm settings persisted across a config read**

Re-run Step 1's curl (optionally widen the field dump) to confirm `MaxGrabsPerUser` and the semaphore value are stored, not just shown in the UI.

---

### Task 4: Controlled live verification (the loop still acquires, no flood, byparr intact)

**Files:** none.

**Interfaces:**
- Consumes: 2.1.1 configured (Tasks 2–3).

- [ ] **Step 1: Tail slskd + Lidarr logs in one window**

Run (leave running during Step 2):
```bash
docker logs -f --since 1m slskd 2>&1 | grep --line-buffered -iE 'search|banned|too many|quickly repeat' &
docker logs -f --since 1m lidarr 2>&1 | grep --line-buffered -iE 'tubifarry|flaresolverr|byparr|json|ffmpeg' &
```

- [ ] **Step 2: Trigger ONE controlled album search**

In Lidarr, manually search for **one** missing album (Album → Search). Just one — this is a smoke test, not a backlog run. (Do NOT kick `lidarr_backlog_drip` or a monitor sweep here.)

- [ ] **Step 3: Confirm acquisition works and NO flood/ban appears**

Watch the tail for ~2–3 min. Expected:
- slskd issues a **bounded** number of searches (not the 4–15 near-dup fan-out) and shows **no** `banned` / `quickly repeat a search` / `too many` lines.
- Lidarr/Tubifarry shows the search resolving to grabs/imports normally.
PASS = a clean search with no flood. (Kill the backgrounded tails with `kill %1 %2` when done.)

- [ ] **Step 4: Explicitly check for the two known 2.1.1 regressions (#200, #199)**

Run:
```bash
docker logs lidarr --since 10m 2>&1 | grep -iE 'flaresolverr|byparr|could not parse|invalid json' | tail -10   # issue #200
docker logs lidarr --since 10m 2>&1 | grep -iE 'ffmpeg.*download|downloading ffmpeg|ffmpeg.*not found' | tail -10  # issue #199
```
Expected: **no** lines for either. If #200 (byparr JSON parse) appears → the CF-indexer path is broken by the upgrade; if #199 (FFmpeg re-download loop) appears → disable the Tubifarry lyrics enhancer. Either is a rollback-or-mitigate decision; record which.

- [ ] **Step 5: Confirm the ops dashboard still reports slskd healthy**

Run:
```bash
source .env && python scripts/media_ops_status.py 2>/dev/null | sed -n '/slskd:/,/qBittorrent/p'
```
Expected: slskd `✓ up vN ...`. (This reuses the Phase-1-era ops tooling — sanity that nothing downstream broke.)

---

### Task 5: Cleanup + record the outcome

**Files:**
- Modify: `docs/superpowers/specs/2026-06-19-goated-music-stack-design.md` (mark Phase 2 status)

- [ ] **Step 1: If verification fully passed, remove the rollback backup**

Run (ONLY if Task 4 passed cleanly):
```bash
rm -rf "${CONFIG_DIRECTORY:-/home/tom/nas/.docker-config}/lidarr/plugins/TypNull/Tubifarry.2.1.0.bak"
```
(Keep it if any issue was merely mitigated rather than cleanly absent.)

- [ ] **Step 2: Update the roadmap spec's Phase 2 status**

In the design doc, annotate the Phase 2 section: slskd v0.25 migration verified-already-done; gluetun integration N/A (AirVPN static port); Tubifarry upgraded 2.1.0→2.1.1 with `MaxGrabsPerUser` + semaphore active; note the chosen values and whether #199/#200 appeared.

- [ ] **Step 3: Commit the doc update**

```bash
git add docs/superpowers/specs/2026-06-19-goated-music-stack-design.md
git commit -m "docs: Phase 2 done — Tubifarry 2.1.1 flood control; slskd v0.25 already migrated"
```

---

## Self-Review

**Spec coverage (Phase 2 of the design doc):**
- Tubifarry → 2.1.1 (semaphore + `MaxGrabsPerUser`) → Tasks 2, 3. ✓
- Watch #199 / #200 on byparr-fronted path → Task 4 Step 4. ✓
- slskd v0.25 config migration → **verified already complete** (Global Constraints + live evidence v0.25.1.0). ✓ (no task needed — correctly descoped)
- slskd native gluetun port integration → **descoped with reason** (AirVPN static port; dynamic-PF feature N/A). ✓
- Respect login-timeout + search-flood gotchas → Global Constraints + Tasks 1.5, 3.1, 4.3. ✓

**Placeholder scan:** No "TBD"/"add error handling"/"similar to". The two genuinely operator-chosen values (`MaxGrabsPerUser`=2 suggested; semaphore left at conservative default) are explicit recommendations with rationale, not placeholders. UI steps that cannot be scripted (plugin update button, indexer field edits) are marked `(UI)`.

**Consistency:** The Tubifarry indexer is referred to as "id 4" consistently (Tasks 1.3, 3.1) matching the documented Lidarr-DB location of the flood-control flags. Plugin path `${CONFIG_DIRECTORY}/lidarr/plugins/TypNull/Tubifarry/` and `CONFIG_DIRECTORY=/home/tom/nas/.docker-config` are used identically throughout. Rollback artifact name `Tubifarry.2.1.0.bak` matches between create (1.4), restore notes, and cleanup (5.1).
