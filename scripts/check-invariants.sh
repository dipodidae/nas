#!/usr/bin/env bash
#
# check-invariants.sh -- assert the things this stack has learned the hard way.
#
# Every check here corresponds to an incident. Each failure prints the ADR that
# explains why the invariant exists, so a future change can read the reasoning
# instead of rediscovering it. Run via `make check`; also wired as a
# pre-commit hook.
#
# Exit codes follow the repo convention (AGENTS.md):
#   0  all invariants hold
#   1  one or more invariants violated
#   2  fatal -- could not render the compose model at all
#
# Usage:
#   scripts/check-invariants.sh            # check
#   scripts/check-invariants.sh --verbose  # also list each passing check
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

VERBOSE=0
[ "${1:-}" = "--verbose" ] || [ "${1:-}" = "-v" ] && VERBOSE=1

# Optional test hook: an extra "-f <path>" appended to every compose
# invocation, used to inject deliberate violations when proving an assertion
# can fail. Deliberately NOT compose.override.yaml -- that path is gitignored
# AND auto-loaded, so a forgotten one is invisible to `git status` and silently
# active.
#
# Passing -f to compose replaces the default file discovery, so the base file
# has to be named explicitly alongside the injected one -- otherwise the
# override renders on its own and every service loses its image.
# shellcheck disable=SC2206  # deliberate word splitting: COMPOSE_EXTRA is "-f path"
EXTRA=()
if [ -n "${COMPOSE_EXTRA:-}" ]; then
  EXTRA=(-f compose.yaml ${COMPOSE_EXTRA})
  COMPOSE_EXTRA="-f compose.yaml ${COMPOSE_EXTRA}"
fi

if ! docker compose "${EXTRA[@]}" config -q 2>/dev/null; then
  echo "FATAL: 'docker compose config' failed -- the compose model does not render." >&2
  docker compose "${EXTRA[@]}" config -q
  exit 2
fi

COMPOSE_EXTRA="${COMPOSE_EXTRA:-}" VERBOSE=$VERBOSE python3 - <<'PY'
import json, os, re, subprocess, sys

try:
    _extra = os.environ.get("COMPOSE_EXTRA", "").split()
    raw = subprocess.run(
        ["docker", "compose", *_extra, "config", "--format", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    model = json.loads(raw)
except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
    print(f"FATAL: could not render the compose model: {exc}", file=sys.stderr)
    sys.exit(2)

services = model["services"]
verbose = os.environ.get("VERBOSE") == "1"

failures = []
warnings = []
passes = []

def fail(check, adr, msg):
    failures.append((check, adr, msg))

def ok(check, msg=""):
    passes.append((check, msg))

def warn(check, adr, msg):
    warnings.append((check, adr, msg))

def env_of(svc):
    return services[svc].get("environment") or {}

def caps(svc):
    return set(services[svc].get("cap_add") or [])

def labels(svc):
    return services[svc].get("labels") or {}

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

# --------------------------------------------------------------------------
# Documented waivers. Every entry needs a reason and an ADR. Shrink this list;
# never grow it without a decision record.
# --------------------------------------------------------------------------

# Services that must not take an unattended image update. There is no
# watchtower any more (ADR-0025), so this list no longer gates a label -- it
# records which services a HUMAN must update deliberately, and `diun` reports
# them like everything else (ADR-0024). Kept because the reasoning is still
# load-bearing every time someone runs `docker compose pull`.
MANUAL_UPDATE_ONLY = {
    "qbittorrent":           "pinned tag, floor >= 5.2.2; `make update-qbittorrent`",
    "jellyfin":              "pinned; a regression surfaces mid-playback; `make pull-jellyfin`",
    "playlist-generator-db": "never bump a database engine under its data",
    "scrutiny":              "omnibus bundles InfluxDB; same rule as playlist-generator-db",
    "diun":                  "the thing that reports updates should not take one by surprise",
    "beszel":                "PocketBase DB under the metric history; bump it deliberately",
    "beszel-agent":          "must stay in lockstep with the hub it reports to",
    "streamystats-db":       "VectorChord/Postgres engine under live data",
    "streamystats":          "must stay in lockstep with its job server and schema",
    "streamystats-jobs":     "must stay in lockstep with the UI and schema",
    "tinyauth":              "a bad auth container closes every protected door at once; chosen, never inherited",
}

# KNOWN GAP, not an exemption: these do not drop capabilities. ADR-0018.
# Warned about on every run so it cannot quietly become the convention.
# 2026-09-02: both closed with measured capability sets -- the dict is
# deliberately left in place (empty) so the next gap has an obvious home.
CAP_DROP_WAIVER = {}

# Ports that are public on purpose. Anything else must bind 127.0.0.1.
# --------------------------------------------------------------------------
# The door: which published routes sit behind tinyauth and which must not.
#
# The rule that decides the column: anything with a native mobile or desktop
# client, a sync protocol, or an API consumed from OUTSIDE this box cannot sit
# behind forward auth -- a 302 to a login page is not something a sync client
# can follow. Verified before any door closed: no *arr-to-*arr, cleanuparr,
# recyclarr, bazarr, jellyseerr or scripts/* integration is configured with a
# https://<service>.${PUBLIC_DOMAIN} URL, in the live app configs and app
# SQLite databases, not just the repo.
#
# `protect` means the auth include is in `location /`. It deliberately does NOT
# mean every location: six of these confs carry an ungated `location ~ /api`,
# because the *arr API's authentication is its API key and gating it would
# break every native client that uses one for no gain. That is a path-scope,
# not an oversight.
#
# BOTH DIRECTIONS ARE ASSERTED. A route silently losing its door answers 200,
# which is exactly the failure ADR-0022's label<->conf reconciliation exists to
# catch, one layer up. ADR-0034.
DOOR = {
    # never -- has real auth of its own AND clients that cannot follow a 302
    "jellyfin":           "never",   # TV/phone/DLNA; LAN ports bypass SWAG entirely
    "nextcloud":          "never",   # desktop + mobile sync over WebDAV
    "ntfy":               "never",   # token auth; a door here makes a broken door SILENT
    "auth":               "never",   # it is the thing that hands out the session
    # protect -- browser-only UIs
    "sonarr":             "protect",
    "radarr":             "protect",
    "lidarr":             "protect",
    "bazarr":             "protect",
    "prowlarr":           "protect",
    "lingarr":            "protect",
    "qui":                "protect",
    "slskd":              "protect",
    "cleanuparr":         "protect",
    "lidarr-bulk":        "protect",
    "playlist-generator": "protect",
    "ongehoord":          "protect",
    "jellyseerr":         "protect",
}

# Routes whose door has not been hung yet. This list SHRINKS to empty as the
# migration lands, one commit per tier, and an empty list is the proof that
# every `protect` route above actually has its door. Never add to it.
DOOR_PENDING = {
    "sonarr", "radarr", "lidarr", "bazarr", "prowlarr", "lingarr",
    "qui", "slskd", "cleanuparr", "lidarr-bulk", "playlist-generator",
    "jellyseerr",
}

# The one service whose public name is not its service name. tinyauth answers
# on auth.${PUBLIC_DOMAIN} because "auth" is what the door is called; the conf
# is therefore auth.subdomain.conf. Recorded here rather than left to the
# filename==subdomain assumption, so both directions of the conf<->label
# reconciliation below stay honest instead of silently agreeing on the wrong
# name. Do not grow this without an ADR. ADR-0034.
ROUTE_ALIASES = {
    "tinyauth": "auth",
}

PUBLIC_PORT_ALLOWLIST = {
    443:  "SWAG HTTPS",
    80:   "SWAG HTTP (ACME + redirect)",
    8096: "Jellyfin HTTP (LAN clients)",
    8920: "Jellyfin HTTPS (LAN clients)",
    7359: "Jellyfin auto-discovery (UDP)",
    1900: "Jellyfin DLNA/SSDP (UDP)",
    6881: "qBittorrent BitTorrent inbound (router-forwarded)",
    50300: "slskd Soulseek inbound (router-forwarded)",
}

# Services whose role legitimately involves holding a credential.
SECRET_OK = {
    "swag", "slskd", "qui", "ntfy", "lidarr-bulk", "ongehoord",
    "playlist-generator", "playlist-generator-db", "watchtower",
    # diun: DIUN_NOTIF_NTFY_TOKEN. Its native ntfy notifier takes a token only,
    # not basic auth, so it cannot use the ?auth= or userinfo tricks the other
    # publishers use. ADR-0024.
    "diun",
    # streamystats: SESSION_SECRET and NEXT_SERVER_ACTIONS_ENCRYPTION_KEY are
    # read by Next.js itself; POSTGRES_PASSWORD by the postgres entrypoint;
    # DATABASE_URL by both app halves. All genuinely consumed. ADR-0030.
    #
    # NOTE on the limits of this check: it matches on NAMES, so
    # `DATABASE_URL` -- which embeds the same password in a connection string --
    # slips past it entirely. The check is a tripwire for forgotten credentials,
    # not a proof that none are present.
    "streamystats", "streamystats-db", "streamystats-jobs",
}

# Env vars that must NOT appear on a given service, whatever else changes.
ENV_DENY = {
    "qbittorrent": (
        {"QBITTORRENT_USER", "QBITTORRENT_PASS"},
        "ADR-0011 -- the LSIO image never reads them (linuxserver#228) and "
        "they only leak into `docker inspect`. They belong in .env for scripts/.",
    ),
}

SECRET_PAT = re.compile(r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|_PASS\b|_KEY\b|CREDENTIAL", re.I)

# ==========================================================================
# 1. qBittorrent image tag is pinned, and >= 5.2.2
# ==========================================================================
img = services.get("qbittorrent", {}).get("image", "")
tag = img.rpartition(":")[2] if ":" in img else ""
if not tag or tag == "latest":
    fail("qbit-tag-pinned", "ADR-0005",
         f"qbittorrent image must be a pinned tag, not '{tag or '<none>'}': {img}")
else:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", tag)
    if not m:
        fail("qbit-tag-pinned", "ADR-0005",
             f"cannot parse a qBittorrent version out of tag '{tag}' -- "
             "the >=5.2.2 floor cannot be verified")
    else:
        ver = tuple(int(x) for x in m.groups())
        if ver < (5, 2, 2):
            fail("qbit-version-floor", "ADR-0005",
                 f"qBittorrent {'.'.join(map(str, ver))} < 5.2.2: upstream #24357 "
                 "makes a recreate unable to prove its lockfile is stale, so qbit "
                 "refuses to start. Fixed by #24363 in 5.2.2.")
        else:
            ok("qbit-tag-pinned", f"{tag} (>= 5.2.2)")

# ==========================================================================
# 2. qBittorrent has KILL, and does NOT have FOWNER/FSETID
# ==========================================================================
qc = caps("qbittorrent")
if "KILL" not in qc:
    fail("qbit-cap-kill", "ADR-0004",
         "qbittorrent is missing cap_add: KILL. s6 runs as root and must signal "
         "qbittorrent-nox (uid 1000); without CAP_KILL every SIGTERM is refused "
         "with EPERM and Docker SIGKILLs after stop_grace_period. Measured "
         "120.3s vs 6.2s.")
else:
    ok("qbit-cap-kill")

widened = qc & {"FOWNER", "FSETID"}
if widened:
    fail("qbit-cap-narrow", "ADR-0004",
         f"qbittorrent has {sorted(widened)}, which was verified NOT needed. "
         "KILL alone is sufficient -- do not widen the grant.")
else:
    ok("qbit-cap-narrow")

# ==========================================================================
# 3. No service carries a watchtower label, and every manual-update service
#    is still pinned
# ==========================================================================
# watchtower is retired (ADR-0025). A leftover
# `com.centurylinklabs.watchtower.enable` label would now control nothing while
# still reading as policy -- which is precisely the class of lie this file
# exists to prevent. All 16 were removed with the service.
#
# The second half is what actually still matters: the services a human must
# update deliberately have to STAY pinned, or `docker compose pull` quietly
# takes the update the pin was protecting against.
WT_PREFIX = "com.centurylinklabs.watchtower"
_stale = sorted(
    svc for svc, sv in services.items()
    if any(str(k).startswith(WT_PREFIX) for k in (labels(svc) or {}))
)
if _stale:
    fail("watchtower-labels-gone", "ADR-0025",
         f"{_stale} still carry a {WT_PREFIX}.* label, but watchtower was "
         "retired. The label controls nothing now and reads as though it does. "
         "Remove it; diun handles notification (ADR-0024).")
else:
    ok("watchtower-labels-gone", "no stale watchtower labels")

def _image_tag_of(image):
    """Tag of an image ref, or '' if untagged. Handles a registry port and a
    digest pin, neither of which a bare rpartition(':') survives."""
    if "@" in image:
        return "@" + image.rsplit("@", 1)[1]
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[1] if ":" in last else ""

_unpinned = []
for svc, why in sorted(MANUAL_UPDATE_ONLY.items()):
    sv = services.get(svc)
    if not sv or "build" in sv:
        continue
    t = _image_tag_of(sv.get("image", ""))
    if not t or t == "latest":
        _unpinned.append(f"{svc} ({why})")
if _unpinned:
    fail("manual-update-pinned", "ADR-0025",
         f"these must be updated by hand and are NOT pinned: {_unpinned}. "
         "Nothing auto-updates any more, but `docker compose pull` follows a "
         "moving tag -- the pin is what makes the update a decision.")
else:
    ok("manual-update-pinned", f"{len(MANUAL_UPDATE_ONLY)} manual-update services pinned")

# ==========================================================================
# 4. mem_limit implies memswap_limit == mem_limit
# ==========================================================================
found_mem = False
for svc, sv in sorted(services.items()):
    ml, msl = sv.get("mem_limit"), sv.get("memswap_limit")
    if ml is None:
        if msl is not None:
            fail("memswap-pairing", "ADR-0001",
                 f"{svc} sets memswap_limit without mem_limit.")
        continue
    found_mem = True
    if str(msl) != str(ml):
        fail("memswap-pairing", "ADR-0007",
             f"{svc}: memswap_limit ({msl}) != mem_limit ({ml}). Without the "
             "pair it can balloon into host swap and thrash everything else "
             "before finally being killed.")
    else:
        ok("memswap-pairing", f"{svc} {int(ml)//2**30}g == {int(ml)//2**30}g")
if not found_mem:
    warn("memswap-pairing", "ADR-0007/0008",
         "no service sets mem_limit -- qbittorrent's 4g and jellyfin's 10g "
         "backstops appear to have been removed. Intentional?")

# ==========================================================================
# 5. Every published port is loopback-bound or explicitly public
# ==========================================================================
for svc, sv in sorted(services.items()):
    for p in sv.get("ports") or []:
        host_ip = p.get("host_ip", "")
        target = p.get("target")
        pub = p.get("published")
        if host_ip in ("127.0.0.1", "::1"):
            continue
        try:
            pubn = int(str(pub).split("-")[0])
        except (TypeError, ValueError):
            pubn = target
        if pubn in PUBLIC_PORT_ALLOWLIST:
            ok("port-exposure", f"{svc} :{pubn} public ({PUBLIC_PORT_ALLOWLIST[pubn]})")
        else:
            fail("port-exposure", "ADR-0001",
                 f"{svc} publishes :{pubn} on all interfaces "
                 f"(host_ip={host_ip or 'unset'}). Internal WebUIs must bind "
                 "127.0.0.1 -- the public surface is SWAG. Add it to "
                 "PUBLIC_PORT_ALLOWLIST only if it is meant to be reachable "
                 "from the LAN or the internet.")

# ==========================================================================
# 6. cap_drop: ALL and no-new-privileges everywhere
# ==========================================================================
for svc, sv in sorted(services.items()):
    sec = sv.get("security_opt") or []
    if "no-new-privileges:true" not in sec:
        fail("no-new-privileges", "ADR-0001",
             f"{svc} is missing security_opt: no-new-privileges:true.")
    dropped = {c.upper() for c in (sv.get("cap_drop") or [])}
    if "ALL" not in dropped:
        if svc in CAP_DROP_WAIVER:
            warn("cap-drop-all", "ADR-0018",
                 f"{svc} does not drop capabilities ({CAP_DROP_WAIVER[svc]})")
        else:
            fail("cap-drop-all", "ADR-0001",
                 f"{svc} is missing cap_drop: ALL. Add it with a selective "
                 "cap_add, or add a waiver with an ADR if it genuinely cannot.")

# ==========================================================================
# 7. Capped json-file logging everywhere
# ==========================================================================
for svc, sv in sorted(services.items()):
    lg = sv.get("logging") or {}
    opts = lg.get("options") or {}
    if lg.get("driver") != "json-file":
        fail("log-capped", "ADR-0001",
             f"{svc} logging driver is {lg.get('driver') or '<unset>'}, "
             "expected json-file.")
    elif not (opts.get("max-size") and opts.get("max-file")):
        fail("log-capped", "ADR-0001",
             f"{svc} json-file logging is missing max-size and/or max-file -- "
             "an uncapped container log will fill the disk.")

# ==========================================================================
# 8. sonarr/radarr/lidarr (and bazarr) mount ${SHARE_DIRECTORY}:/data
# ==========================================================================
DATA_REQUIRED = {
    "sonarr": "ADR-0002", "radarr": "ADR-0002",
    "lidarr": "ADR-0003",   # root folder IS /data/music since 2026-09-02
    "bazarr": "ADR-0015",   # needed for path resolution, not hardlinks
}
for svc, adr in sorted(DATA_REQUIRED.items()):
    targets = {v.get("target") for v in (services.get(svc, {}).get("volumes") or [])}
    if "/data" not in targets:
        fail("data-mount", adr,
             f"{svc} does not mount the share at /data. Hardlinks cannot cross "
             "a mount point (0.96 TiB of duplication), and bazarr additionally "
             "resolves the /data/... paths the *arrs report.")
    else:
        ok("data-mount", f"{svc} -> /data")

# ==========================================================================
# 9. dockerproxy is the only thing touching the Docker socket
# ==========================================================================
holders = sorted(
    svc for svc, sv in services.items()
    if any("docker.sock" in str(v.get("source", "")) for v in (sv.get("volumes") or []))
)
if holders != ["dockerproxy"]:
    extra = [h for h in holders if h != "dockerproxy"]
    if extra:
        fail("docker-sock", "ADR-0013",
             f"{extra} mount /var/run/docker.sock. Only dockerproxy may -- the "
             "socket is root on the host. Route through tcp://dockerproxy:2375.")
    if "dockerproxy" not in holders:
        fail("docker-sock", "ADR-0013",
             "dockerproxy does not mount the Docker socket; watchtower and "
             "autoheal have no route to the Docker API.")
else:
    ok("docker-sock", "dockerproxy only")

# ==========================================================================
# 10. No secret-shaped env on a container that has no business holding one
# ==========================================================================
for svc, (denied, why) in ENV_DENY.items():
    present = denied & set(env_of(svc))
    if present:
        fail("env-secrets", "ADR-0011",
             f"{svc} sets {sorted(present)}. {why}")
    else:
        ok("env-secrets", f"{svc} clean of {sorted(denied)}")

for svc in sorted(services):
    if svc in SECRET_OK:
        continue
    leaked = sorted(k for k in env_of(svc) if SECRET_PAT.search(k))
    if leaked:
        fail("env-secrets", "ADR-0011",
             f"{svc} carries secret-shaped env {leaked} but is not on the "
             "SECRET_OK list. A container gets a credential only if the process "
             "inside it actually reads one; otherwise it is just sitting in "
             "`docker inspect`. Add it to SECRET_OK if this is legitimate.")

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
    lbls = svc.get("labels") or {}
    if isinstance(lbls, list):
        lbls = dict(x.split("=", 1) for x in lbls if "=" in x)
    if str(lbls.get("autoheal", "")).lower() != "true":
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
    lbls = svc.get("labels") or {}
    if isinstance(lbls, list):
        lbls = dict(x.split("=", 1) for x in lbls if "=" in x)
    return lbls

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
    import http.cookiejar, urllib.parse, urllib.request
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

    # DiskIOType decides whether libtorrent mmaps at all. This was a standing
    # warning until it was measured on 2026-09-02; the question is now closed
    # and the default is the accepted position, so it reports rather than nags.
    #
    # The measurement: the cgroup hit its 4g limit 40,277 times and OOM-killed
    # ZERO times, while file_mapped fell 3.06GB -> 2.24GB between two readings
    # and anon stayed at ~40MB. The mmap'd pages are reclaimable page cache, not
    # a leak. Switching to the POSIX backend would remove a mechanism that is
    # not doing harm, at a real throughput cost. Full reasoning in ADR-0007.
    #
    # What would reopen it: oom_kill going non-zero, or memory.current staying
    # pinned while ANON (not file) is what grows.
    _t = _prefs.get("disk_io_type")
    ok("qbit-diskio-type",
       f"{_QBT_IOTYPE.get(_t, _t)} -- accepted position, mmap pages proven "
       "reclaimable (ADR-0007)")

# ==========================================================================
# 15. dockerproxy exposes only what autoheal needs
# ==========================================================================
# The Docker socket is root on the host, so the proxy's job is to be narrow.
# With watchtower retired (ADR-0025), `autoheal` is the only client and it needs
# exactly: list containers (CONTAINERS), POST a restart (POST), and the client
# handshake (PING, VERSION).
#
# IMAGES, NETWORKS and DELETE existed ONLY for watchtower's recreate flow --
# NETWORKS to disconnect/reconnect containers on nas-network, DELETE to remove
# them. That recreate is the thing that left NO container at all for 13h on
# 2026-09-01, so this is not tidying an unused grant: it removes the capability
# that caused the incident.
#
# Verified on 2026-09-02 against this exact set, with a disposable canary
# container and a dedicated autoheal watching its own label (never slskd --
# ADR-0009): the canary was restarted three times, while /images/json,
# /networks, /info and /exec all returned 403.
DOCKERPROXY_ALLOWED = {"CONTAINERS", "PING", "VERSION", "POST"}
DOCKERPROXY_REQUIRED = {"CONTAINERS", "POST"}
_dp = {k: str(v) for k, v in env_of("dockerproxy").items()}
_enabled = {k for k, v in _dp.items() if v == "1"}
_extra = sorted(_enabled - DOCKERPROXY_ALLOWED)
_missing = sorted(DOCKERPROXY_REQUIRED - _enabled)
if _extra:
    fail("dockerproxy-narrow", "ADR-0025",
         f"dockerproxy enables {_extra}, which nothing in this stack needs. "
         "autoheal uses CONTAINERS + POST (+ PING/VERSION for the handshake). "
         "IMAGES/NETWORKS/DELETE were watchtower's recreate flow, and that "
         "recreate is the incident. Do not re-widen without an ADR.")
elif _missing:
    fail("dockerproxy-narrow", "ADR-0025",
         f"dockerproxy is missing {_missing}; autoheal cannot list containers "
         "or restart one, so nothing heals an unhealthy container.")
else:
    ok("dockerproxy-narrow", f"{sorted(_enabled)} only")

# ==========================================================================
# 16. Jellyfin's tag is pinned too
# ==========================================================================
# Never for watchtower's sake -- and now there is no watchtower at all
# (ADR-0025). A Jellyfin regression is discovered mid-playback, so the update
# must be chosen: `make pull-jellyfin`. diun reports when there is one to
# choose (ADR-0024). ADR-0006.
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

# ==========================================================================
# 18. Every swag=enable service has a proxy-conf
# ==========================================================================
# What actually routes a subdomain on this host is the PRESENCE OF THE CONF,
# not the label: no DOCKER_MODS is set, so the linuxserver auto-proxy mod is not
# installed and `swag=enable` is documentation rather than mechanism (verified
# 2026-09-02). Both directions therefore drift silently, and both have:
#   * lingarr carried the label with no conf, so lingarr.${PUBLIC_DOMAIN}
#     answered SWAG's default page -- with a 200, which is why nobody noticed;
#   * slskd had a conf and no label, i.e. a public surface the compose file did
#     not declare.
# Confs are looked for in the tracked swag/proxy-confs/ first, then in the SWAG
# config dir; a conf in only the latter is a warning, because it is gitignored
# and survives only via the nightly backup. ADR-0022.
#
# A service can also be routed by a tracked conf bind-mounted into swag under
# a different name -- 4eva-rootpage is the apex site and arrives as
# site-confs/root.conf, not a subdomain conf. Those count as tracked, because
# the point of the check is "the route lives in this repo", not "the route is
# spelled <name>.subdomain.conf".
_swag_dir = os.path.join(_conf, "swag", "nginx", "proxy-confs") if _conf else None
_swag_mounts = [
    os.path.basename(str(v.get("source", "")))
    for v in (services.get("swag", {}).get("volumes") or [])
    if str(v.get("target", "")).startswith("/config/nginx/")
]
_missing, _untracked = [], []
for name, svc in sorted(services.items()):
    lbls = svc.get("labels") or {}
    if isinstance(lbls, list):
        lbls = dict(x.split("=", 1) for x in lbls if "=" in x)
    if lbls.get("swag") != "enable" or name == "swag":
        continue
    _route = ROUTE_ALIASES.get(name, name)
    _tracked = (os.path.isfile(f"swag/proxy-confs/{_route}.subdomain.conf")
                or any(m.startswith(f"{_route}.") for m in _swag_mounts))
    _live = bool(_swag_dir) and os.path.isfile(
        os.path.join(_swag_dir, f"{_route}.subdomain.conf"))
    if _tracked:
        continue
    (_untracked if _live else _missing).append(name)
_swag_dir_readable = bool(_swag_dir) and os.path.isdir(_swag_dir)
if _missing and not _swag_dir_readable:
    # CI has no .docker-config/, so "not in the tracked dir" cannot be
    # distinguished from "not routed at all". Degrade rather than fail the
    # build on something only the host can answer -- the same treatment
    # jellyfin-mounts-frozen gets.
    warn("swag-labels-are-routed", "ADR-0020",
         f"the SWAG config dir is not readable from here (expected in CI), so "
         f"routing for {_missing} could not be verified. `make check` on the "
         "host is what distinguishes 'routed from .docker-config' from "
         "'not routed at all'.")
elif _missing:
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

# ... and the inverse: a route nobody declared. A conf is what actually serves
# the subdomain, so one without a matching swag=enable label is a public surface
# the compose file does not mention. ADR-0022.
_labelled = {
    n for n, v in services.items()
    if (dict(x.split("=", 1) for x in v["labels"] if "=" in x)
        if isinstance(v.get("labels"), list) else (v.get("labels") or {})
        ).get("swag") == "enable"
}
_labelled_routes = {ROUTE_ALIASES.get(n, n) for n in _labelled}
_undeclared = sorted(
    f[: -len(".subdomain.conf")]
    for f in (os.listdir("swag/proxy-confs") if os.path.isdir("swag/proxy-confs") else [])
    if f.endswith(".subdomain.conf")
    and f[: -len(".subdomain.conf")] not in _labelled_routes
)
if _undeclared:
    warn("swag-routes-are-declared", "ADR-0022",
         f"{_undeclared} have a proxy-conf but no swag=enable label. The conf is "
         "what actually serves the subdomain, so this is a public surface the "
         "compose file does not declare. Add the label, or delete the conf.")
else:
    ok("swag-routes-are-declared", "no undeclared public routes")


# ==========================================================================
# 19. Raw disk access and CAP_SYS_ADMIN belong to scrutiny alone
# ==========================================================================
# The hardening baseline is cap_drop: ALL (ADR-0001). scrutiny is the single
# scoped exception, because reading SMART from an NVMe needs
# NVME_IOCTL_ADMIN_CMD, which the kernel gates on CAP_SYS_ADMIN. Measured
# 2026-09-02, deliberately narrower than the upstream example:
#   SYS_RAWIO alone -> Permission denied;  SYS_ADMIN alone -> works;
#   both            -> no better than SYS_ADMIN alone.
# So SYS_RAWIO is redundant here and must NOT be granted -- same discipline as
# the FOWNER/FSETID refusal on qbittorrent (ADR-0004).
#
# The device passthrough is asserted too, in three ways, because each was a
# real trap:
#   * only scrutiny may hold a raw DISK. jellyfin's /dev/dri (a GPU) is fine
#     and is not a disk, so this is a disk-shaped test, not a devices-shaped one.
#   * it must be the NVMe CONTROLLER char device (/dev/nvme0), not the namespace
#     block device: `smartctl --scan` returns EMPTY with only /dev/nvme0n1, so
#     the collector silently monitors nothing while looking installed.
#   * it must be granted `:r`. Read is measurably sufficient.
# ADR-0023.
_DISK_DEV = re.compile(r"^/dev/(nvme\d+n?\d*|sd[a-z]+\d*|hd[a-z]+|vd[a-z]+)")
_RAW_CAPS = {"SYS_ADMIN", "SYS_RAWIO"}

_disk_holders, _rawcap_holders = [], []
for svc, sv in sorted(services.items()):
    for d in (sv.get("devices") or []):
        if _DISK_DEV.match(str(d.get("source", ""))):
            _disk_holders.append((svc, d))
    if {str(c).upper().removeprefix("CAP_") for c in (sv.get("cap_add") or [])} & _RAW_CAPS:
        _rawcap_holders.append(svc)

_bad_disk = sorted({s for s, _ in _disk_holders if s != "scrutiny"})
if _bad_disk:
    fail("raw-disk-access", "ADR-0023",
         f"{_bad_disk} pass a raw disk device through. Only scrutiny may -- raw "
         "block access to the disks holding this stack's config and 4.6 TB of "
         "un-backed-up media is not something a media app gets for convenience.")
else:
    ok("raw-disk-access", "scrutiny only")

_bad_caps = sorted(s for s in _rawcap_holders if s != "scrutiny")
if _bad_caps:
    fail("raw-cap-access", "ADR-0023",
         f"{_bad_caps} request SYS_ADMIN and/or SYS_RAWIO. Only scrutiny may, and "
         "only because the NVMe SMART ioctl is gated on CAP_SYS_ADMIN. SYS_ADMIN "
         "is close enough to root that it defeats most of ADR-0001.")
else:
    ok("raw-cap-access", "scrutiny only")

if "scrutiny" in services:
    _sc = services["scrutiny"]
    _sc_caps = {str(c).upper().removeprefix("CAP_") for c in (_sc.get("cap_add") or [])}
    if "SYS_RAWIO" in _sc_caps:
        fail("scrutiny-caps-narrow", "ADR-0023",
             "scrutiny has SYS_RAWIO, which was measured NOT needed: SYS_ADMIN "
             "alone reads the NVMe fully, and SYS_RAWIO alone cannot. The "
             "upstream example asks for both; this host measured one.")
    elif "SYS_ADMIN" not in _sc_caps:
        fail("scrutiny-caps-narrow", "ADR-0023",
             "scrutiny is missing SYS_ADMIN, so smartctl gets "
             "'NVME_IOCTL_ADMIN_CMD: Permission denied' and the collector "
             "reports a device with no data -- which looks like a working "
             "install in the UI.")
    else:
        ok("scrutiny-caps-narrow", "SYS_ADMIN only")

    _devs = [d for _s, d in _disk_holders if _s == "scrutiny"]
    _srcs = {str(d.get("source", "")) for d in _devs}
    if not _devs:
        fail("scrutiny-device", "ADR-0023",
             "scrutiny passes through no disk device, so it monitors nothing.")
    elif any(re.match(r"^/dev/nvme\d+n\d", s) for s in _srcs):
        fail("scrutiny-device", "ADR-0023",
             f"scrutiny is given an NVMe NAMESPACE block device ({sorted(_srcs)}). "
             "`smartctl --scan` enumerates controller char devices and returns "
             "EMPTY for a namespace, so the collector finds no disks at all -- "
             "silently. Pass /dev/nvme0, not /dev/nvme0n1.")
    elif any(d.get("permissions", "rwm") != "r" for d in _devs):
        fail("scrutiny-device", "ADR-0023",
             "scrutiny's disk device is not granted read-only (`:r`). Read was "
             "measured sufficient, so anything wider is an unearned grant on a "
             "container that already holds CAP_SYS_ADMIN.")
    else:
        ok("scrutiny-device", f"{sorted(_srcs)} read-only")

# ==========================================================================
# 20. No two services publish the same host port
# ==========================================================================
# `docker compose config` does NOT catch this -- the collision only surfaces at
# `up` time, as a bind failure on whichever service starts second, which then
# looks like that service being broken. It is not hypothetical: scrutiny serves
# on 8080 inside and its upstream example publishes host 8080, which
# qbittorrent already owns on 127.0.0.1 -- and scripts/ reach qbit there via its
# localhost auth-bypass, so losing that publish breaks three scripts silently.
# ADR-0014, ADR-0023.
_claimed = {}
for svc, sv in sorted(services.items()):
    for pt in (sv.get("ports") or []):
        pub = pt.get("published")
        if pub is None:
            continue
        key = (pt.get("host_ip", ""), str(pub), pt.get("protocol", "tcp"))
        _claimed.setdefault(key, []).append(svc)
_dupes = {k: v for k, v in _claimed.items() if len(set(v)) > 1}
if _dupes:
    for (ip, pub, proto), svcs in sorted(_dupes.items()):
        fail("port-collision", "ADR-0023",
             f"{sorted(set(svcs))} all publish {ip or '0.0.0.0'}:{pub}/{proto}. "
             "compose config renders this fine; the second container to start "
             "just fails to bind, and looks broken rather than conflicted.")
else:
    ok("port-collision", f"{len(_claimed)} published host ports, all distinct")


# ==========================================================================
# 21. Diun's manifest is current, and covers every watchable service
# ==========================================================================
# Diun watches what its file-provider manifest lists, and nothing else. So a
# service added to the compose model without regenerating the manifest silently
# loses update notification -- and drift in a NOTIFICATION config is the worst
# kind, because the thing that would have told you is the thing that stopped.
#
# Two separate assertions, because they fail for different reasons:
#   * coverage: every service with an image and no build: section appears. A
#     locally-built service is excluded by derivation, not by a list, exactly
#     like the watchtower opt-out above.
#   * currency: the tracked file still equals what the emitter produces. This
#     catches a hand-edit of a generated file as well as a stale regeneration.
# ADR-0024.
_MANIFEST = "diun/manifest.yml"
if "diun" in services:
    try:
        with open(_MANIFEST, encoding="utf-8") as fh:
            _manifest_text = fh.read()
    except OSError:
        _manifest_text = None

    if _manifest_text is None:
        fail("diun-manifest-present", "ADR-0024",
             f"{_MANIFEST} is missing, but diun mounts it read-only and watches "
             "nothing without it. Regenerate with `make diun-manifest`.")
    else:
        _watchable = {
            n for n, v in services.items()
            if v.get("image") and "build" not in v
        }
        _listed = set(re.findall(r"^\s+service:\s*(\S+)\s*$", _manifest_text, re.M))
        _unwatched = sorted(_watchable - _listed)
        if _unwatched:
            fail("diun-manifest-coverage", "ADR-0024",
                 f"{_unwatched} pull an image but are absent from {_MANIFEST}, so "
                 "nothing will ever report an update for them. Run "
                 "`make diun-manifest` and commit the result.")
        else:
            ok("diun-manifest-coverage", f"{len(_listed)} images watched")

        # Currency. Shelling out to the emitter keeps ONE definition of the
        # manifest format -- duplicating the render here would just create a
        # second thing to drift.
        try:
            _rc = subprocess.run(
                [sys.executable, "scripts/emit_diun_manifest.py", "--check"],
                capture_output=True, text=True, check=False,
            )
        except OSError as _exc:
            warn("diun-manifest-current", "ADR-0024",
                 f"could not run the manifest emitter ({_exc}); currency unverified")
        else:
            if _rc.returncode == 0:
                ok("diun-manifest-current", "matches the compose model")
            elif _rc.returncode == 1:
                fail("diun-manifest-current", "ADR-0024",
                     f"{_MANIFEST} is out of date with the compose model. Run "
                     "`make diun-manifest` and commit the result. "
                     f"({(_rc.stderr or '').strip().splitlines()[:1]})")
            else:
                warn("diun-manifest-current", "ADR-0024",
                     "the manifest emitter could not render the compose model "
                     "(expected in CI without docker); currency unverified")


# ==========================================================================
# 22. An autoheal-monitored service's start_period must cover its worst
#     documented cold start
# ==========================================================================
# autoheal turns a failing healthcheck into a restart. Inside start_period
# Docker counts no failures, so start_period is the ONLY thing standing between
# a slow initialization and an auto-restart of it.
#
# For slskd that restart is not merely unhelpful, it is self-perpetuating:
# slskd does not bind :5030 until its share scan finishes, an interrupted scan
# is recorded as SUSPECT, and the next start then force-rescans from 0% even
# though the on-disk share cache restored fine. So restart -> suspect ->
# full rescan -> restart is a loop in which slskd is never up again.
#
# It has happened twice at two different numbers. The stock 120s+5x60s failed at
# ~9 min. Then 30m failed, because it was extrapolated from the first 39% while
# those directories were still warm in page cache. Then 90m held -- until the
# share grew: on 2026-09-02 a forced rescan of 19,433 directories / 180k files
# was still running at 94% after 1h44m, the 90m window expired, and the
# container flipped to unhealthy mid-scan. Only autoheal being stopped by hand
# prevented the loop.
#
# Hence a DECLARED floor rather than a remembered one, with the measurement that
# produced it. Raise the floor when a measured scan approaches it -- and read it
# off a COMPLETED run, never extrapolate. ADR-0009, ADR-0026.
START_PERIOD_FLOOR = {
    "slskd": (
        4 * 3600,
        "a full forced share rescan measured 2h05m at 180k files on 2026-09-02; "
        "an interrupted scan is marked suspect and forces another, so a restart "
        "mid-scan never recovers",
    ),
}
for _svc, (_floor, _why) in sorted(START_PERIOD_FLOOR.items()):
    _sv = services.get(_svc)
    if not _sv:
        continue
    _hc = _sv.get("healthcheck") or {}
    _sp = _secs(_hc.get("start_period", "0"))
    if _sp is None:
        fail("start-period-floor", "ADR-0026",
             f"{_svc}: unparseable healthcheck start_period "
             f"{_hc.get('start_period')!r}; the floor cannot be verified.")
    elif _sp < _floor:
        fail("start-period-floor", "ADR-0026",
             f"{_svc} start_period is {_sp}s, below the documented floor of "
             f"{_floor}s. {_why}. Inside start_period Docker counts no failures, "
             "so this value is the only thing preventing autoheal from "
             "restarting a slow start -- and for this service a restart makes it "
             "permanently worse.")
    else:
        ok("start-period-floor", f"{_svc} {_sp}s >= {_floor}s")


# ==========================================================================
# 23. qui's data view is path-identical to qBittorrent's, and no wider
# ==========================================================================
# qui's cross-seed creates hardlinks ITSELF -- it does not ask qBittorrent to --
# so it needs a view of the data. Two things must hold or it silently does the
# wrong thing:
#
#   * SAME container path for the SAME host path. qui writes link paths that
#     qBittorrent is then told to seed from. If the two disagree, qui creates
#     links at paths qBittorrent cannot find, and every cross-seed fails in a
#     way that looks like an indexer problem.
#   * ONE mount. Hardlinks cannot cross a mount point: a link target on a
#     different filesystem silently becomes a COPY, which is how this stack
#     paid 0.96 TiB before (ADR-0002). Asserting the host paths share a prefix
#     is the config-time half; `make verify-runtime` compares the actual
#     device numbers, which is the only real proof.
#
# Narrow on purpose: qui gets `downloads`, not the whole share. Cross-seed only
# ever touches what qBittorrent manages, and a torrent UI has no business
# reading /music or /movies. ADR-0027.
def _mounts(svc):
    return {v.get("target"): str(v.get("source", ""))
            for v in (services.get(svc, {}).get("volumes") or [])}

if "qui" in services and "qbittorrent" in services:
    _q, _qb = _mounts("qui"), _mounts("qbittorrent")
    _qb_dl = _qb.get("/downloads")
    _qui_dl = _q.get("/downloads")
    if not _qb_dl:
        fail("qui-data-alignment", "ADR-0027",
             "qbittorrent does not mount /downloads, so nothing can be aligned "
             "to it. Everything below assumes that mount.")
    elif not _qui_dl:
        fail("qui-data-alignment", "ADR-0027",
             "qui does not mount /downloads. Its cross-seed creates hardlinks "
             "itself, so with no view of the data it cannot link at all -- and "
             "the failure surfaces as a cross-seed that never matches.")
    elif _qui_dl != _qb_dl:
        fail("qui-data-alignment", "ADR-0027",
             f"qui's /downloads is host {_qui_dl!r} but qbittorrent's is "
             f"{_qb_dl!r}. qui writes link paths qBittorrent then reads, so a "
             "mismatch means links at paths qBittorrent cannot find.")
    else:
        _extra = sorted(t for t in _q if t not in ("/config", "/downloads"))
        if _extra:
            fail("qui-data-alignment", "ADR-0027",
                 f"qui mounts {_extra} on top of /config and /downloads. Keep "
                 "its filesystem view to what qBittorrent manages -- a torrent "
                 "UI has no business reading the media libraries.")
        else:
            ok("qui-data-alignment", f"/downloads -> {_qui_dl} on both")


# ==========================================================================
# 24. Nothing uses host networking, privileged mode, or host PID/IPC
# ==========================================================================
# AGENTS.md forbids all of these without justification, and there is currently
# no justification anywhere in the stack. Asserted rather than trusted because
# the pressure to add one is real and arrives with an upstream doc attached:
# Beszel's own documentation says its agent MUST use `network_mode: host` to
# collect network-interface stats. It does not, here, and the reasoning is worth
# keeping because it is not just "the rule says no":
#
#   a host-networked container cannot resolve `dockerproxy` by service DNS, so
#   it would force publishing the Docker API proxy on the host -- which ADR-0013
#   refuses outright. Following the upstream layout would have broken TWO rules
#   to gain NIC stats, and the only interesting interface (the WAN link) is
#   already measured properly by scripts/wan_shaper.sh.
#
# ADR-0028.
_bad_net, _bad_priv, _bad_ns = [], [], []
for svc, sv in sorted(services.items()):
    if str(sv.get("network_mode", "")).startswith(("host", "container:")):
        _bad_net.append(f"{svc}={sv['network_mode']}")
    if sv.get("privileged"):
        _bad_priv.append(svc)
    for key in ("pid", "ipc", "userns_mode", "uts"):
        if str(sv.get(key, "")).startswith("host"):
            _bad_ns.append(f"{svc}.{key}={sv[key]}")
if _bad_net:
    fail("no-host-namespaces", "ADR-0028",
         f"{_bad_net} use host/container networking. It removes the network "
         "isolation every other service has, and it breaks service DNS -- so a "
         "host-networked container cannot reach dockerproxy and would force "
         "publishing the Docker API on the host (ADR-0013 refuses that).")
elif _bad_priv:
    fail("no-host-namespaces", "ADR-0001",
         f"{_bad_priv} request privileged mode, which defeats cap_drop: ALL "
         "entirely. Grant measured capabilities instead, as scrutiny does.")
elif _bad_ns:
    fail("no-host-namespaces", "ADR-0001",
         f"{_bad_ns} share a host namespace.")
else:
    ok("no-host-namespaces", f"{len(services)} services, all isolated")

# ==========================================================================
# 25. The notification taxonomy: no topic literals, and no credential leaks
# ==========================================================================
# Six lanes, all prefixed `nas-`, and severity carried by the PRIORITY rather
# than the topic name. The whole scheme only survives contact with a future
# change if the mechanical parts are asserted, because every failure mode here
# is silent: a bare topic literal still publishes, a wrong lane still returns
# 200, and a token in an `environment:` block still works. ADR-0033.

import glob as _glob

REPO = os.getcwd()

def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""

def _code_lines(path, comment="#"):
    """Non-comment, non-blank lines. Prose that DOCUMENTS a topic name is not a
    topic literal -- a scanner that cannot tell the two apart fails on the very
    files that explain the rule."""
    out = []
    in_doc = False
    for line in _read(path).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Crude but sufficient triple-quote tracking for the Python scripts.
        if path.endswith(".py"):
            ticks = stripped.count('"""')
            if in_doc:
                if ticks:
                    in_doc = False
                continue
            if ticks == 1:
                in_doc = True
                continue
        if stripped.startswith(comment):
            continue
        out.append(line)
    return out

LANE_NAMES = ("critical", "attention", "media", "requests", "infra", "updates")
NOTIFY_PY = os.path.join(REPO, "scripts", "notify.py")

# --- 25a. no bare `nas-<lane>` literal outside the router -----------------
# Every publisher names a LANE; the router is the only thing that may know a
# topic string. compose files interpolate ${NTFY_TOPIC_*} instead.
_scan = []
for pattern in ("scripts/*.py", "scripts/*.sh", "compose/*.yaml",
                "webapps/*/compose.yaml", "Makefile"):
    _scan.extend(_glob.glob(os.path.join(REPO, pattern)))

# Matched by SHAPE, not by mere presence: a topic name only matters where it is
# a DESTINATION. `"pending updates go to nas-updates at 04:10"` in a digest
# message is prose; `http://ntfy:8410/nas-updates` is a publish target. The
# five shapes below are every way this stack can spell a destination -- a URL
# path, a shell/compose assignment, a Python keyword or dict value, and a JSON
# list element.
def _destination_shapes(lane):
    literal = "nas-" + lane
    return (f"/{literal}", f"={literal}", f'="{literal}"', f"='{literal}'",
            f': "{literal}"', f"['{literal}']", f'["{literal}"]')

_literals = []
for path in sorted(_scan):
    rel = os.path.relpath(path, REPO)
    if rel in ("scripts/notify.py", "scripts/check-invariants.sh"):
        continue  # the router defines them; this file asserts them
    for line in _code_lines(path):
        # An env-var DEFAULT is the one allowed destination spelling, because a
        # container's own notifier needs a fallback when .env is absent:
        #   TOPIC=${NTFY_TOPIC_MEDIA:-nas-media}
        if "NTFY_TOPIC_" in line:
            continue
        for lane in LANE_NAMES:
            if any(shape in line for shape in _destination_shapes(lane)):
                _literals.append(f"{rel}: {line.strip()[:90]}")
                break

if _literals:
    fail("ntfy-no-topic-literals", "ADR-0033",
         "a bare ntfy topic literal escaped the router -- every publisher must "
         f"name a LANE and let scripts/notify.py resolve the topic: {_literals[:4]}")
else:
    ok("ntfy-no-topic-literals", f"{len(_scan)} files, none holds a topic name")

# --- 25b. every NTFY_* referenced anywhere is documented -----------------
_referenced = set()
for path in sorted(_scan) + [os.path.join(REPO, ".env.example")]:
    for m in re.finditer(r"NTFY_(?:TOPIC|TOKEN)_[A-Z_]+", _read(path)):
        _referenced.add(m.group(0))

_example = _read(os.path.join(REPO, ".env.example"))
_agents = _read(os.path.join(REPO, "AGENTS.md"))
_undocumented = sorted(
    v for v in _referenced if v not in _example or v not in _agents
)
if _undocumented:
    fail("ntfy-env-documented", "ADR-0033",
         "these ntfy variables are referenced but not documented in BOTH "
         f".env.example and AGENTS.md: {_undocumented}")
else:
    ok("ntfy-env-documented", f"{len(_referenced)} NTFY_TOPIC_*/NTFY_TOKEN_* documented")

# --- 25c. every lane has a priority, and critical is never held back ------
_router = _read(NOTIFY_PY)
_missing_lane = [lane for lane in LANE_NAMES if f'{lane.upper()} = "{lane}"' not in _router]
_lane_specs = re.findall(r"Lane\.([A-Z]+): LaneSpec\((\d)", _router)
_spec_lanes = {name.lower() for name, _prio in _lane_specs}
if _missing_lane or _spec_lanes != set(LANE_NAMES):
    fail("ntfy-lane-priorities", "ADR-0033",
         f"scripts/notify.py does not declare all six lanes with a priority: "
         f"missing={_missing_lane} specs={sorted(_spec_lanes)}")
elif any(not (1 <= int(prio) <= 5) for _n, prio in _lane_specs):
    fail("ntfy-lane-priorities", "ADR-0033",
         "a lane declares a priority outside ntfy's 1-5 range")
elif 'if lane is Lane.CRITICAL:\n    return 0.0' not in _router:
    fail("ntfy-critical-never-suppressed", "ADR-0033",
         "cooldown_seconds() no longer pins nas-critical to a zero window. "
         "A suppressed critical alert is the exact failure the lane exists to "
         "prevent -- there is no cooldown value that is correct there.")
elif 'if effective_delay and lane is not Lane.CRITICAL:' not in _router:
    fail("ntfy-critical-never-delayed", "ADR-0033",
         "build_message() no longer strips X-Delay for nas-critical. Quiet "
         "hours must never hold a critical alert until 08:00.")
else:
    ok("ntfy-lane-priorities", "6 lanes, prio 5/4/3/4/2/1, critical unsuppressible")

# --- 25d. the retired topic is gone from every ACTIVE surface -------------
# Scoped to code and configuration on purpose. `nas-alerts` still appears in
# docs/decisions/, docs/jellyfin-playback-audit.md, the archived crontab
# snapshots and in prose explaining this migration -- those are RECORDS of what
# was true, and rewriting history to satisfy a grep would be the wrong fix.
_stale = []
for path in sorted(_scan):
    rel = os.path.relpath(path, REPO)
    if rel == "scripts/check-invariants.sh":
        continue
    for line in _code_lines(path):
        if any(s in line for s in ("/nas-alerts", "=nas-alerts", '="nas-alerts"',
                                   "='nas-alerts'", ': "nas-alerts"',
                                   "['nas-alerts']", '["nas-alerts"]')):
            _stale.append(f"{rel}: {line.strip()[:80]}")
if _stale:
    fail("ntfy-alerts-retired", "ADR-0033",
         f"the retired `nas-alerts` topic is still referenced in code or "
         f"configuration: {_stale[:4]}")
else:
    ok("ntfy-alerts-retired", "no active reference to nas-alerts")

# --- 25e. the arr token is a FILE, never an environment variable ----------
_token_mount = "/ntfy/arr-token"
_bad_env, _bad_ro, _mounted = [], [], []
for svc, sv in sorted(services.items()):
    for key in env_of(svc):
        # Scoped to the *arr publisher token. `diun` DOES carry
        # DIUN_NOTIF_NTFY_TOKEN in its environment, and that is a documented
        # exception (ADR-0024): its native notifier accepts a token only, so it
        # can use neither the ?auth= query trick nor URL userinfo, and it has no
        # file-based option at all. It is also a DIFFERENT token -- minted on
        # nas-scripts, revocable on its own. The rule being asserted here is
        # narrower and absolute: the token that the three *arr containers hold,
        # which is the one stored in their SQLite databases, must reach them as
        # a read-only file and never as an inspectable environment variable.
        if key in ("NTFY_TOKEN_ARR", "ARR_TOKEN", "NTFY_ARR_TOKEN"):
            _bad_env.append(f"{svc}.{key}")
    for vol in sv.get("volumes") or []:
        if not isinstance(vol, dict):
            continue
        if _token_mount in str(vol.get("source", "")):
            _mounted.append(svc)
            if not vol.get("read_only"):
                _bad_ro.append(svc)
if _bad_env:
    fail("ntfy-arr-token-is-a-file", "ADR-0011",
         f"{_bad_env} carry an ntfy token in an `environment:` block. A "
         "credential there leaks into `docker inspect`; the token belongs in "
         "the 0600 file bind-mounted read-only at /run/ntfy-arr-token.")
elif _bad_ro:
    fail("ntfy-arr-token-is-a-file", "ADR-0011",
         f"{_bad_ro} mount the arr-token WITHOUT :ro. A container that can "
         "rewrite its own credential can escalate its own ACL.")
elif not _mounted:
    fail("ntfy-arr-token-is-a-file", "ADR-0033",
         "nothing mounts ${CONFIG_DIRECTORY}/ntfy/arr-token, so no *arr can "
         "publish its imports to nas-media.")
else:
    ok("ntfy-arr-token-is-a-file", f"{sorted(_mounted)}, all :ro, none in env")

# --- 25f. arr_notify.sh is mounted :ro into exactly the three *arr ---------
_expected = {"sonarr", "radarr", "lidarr"}
_have, _rw = set(), []
for svc, sv in sorted(services.items()):
    for vol in sv.get("volumes") or []:
        if not isinstance(vol, dict):
            continue
        if "arr_notify.sh" in str(vol.get("source", "")):
            _have.add(svc)
            if not vol.get("read_only"):
                _rw.append(svc)
if _rw:
    fail("arr-notify-script-mount", "ADR-0033",
         f"{_rw} mount scripts/arr_notify.sh writable. It runs inside the "
         "import pipeline; it must not be modifiable from there.")
elif _have != _expected:
    fail("arr-notify-script-mount", "ADR-0033",
         f"scripts/arr_notify.sh must be mounted into exactly {sorted(_expected)}, "
         f"found {sorted(_have)}. Bazarr, lingarr, recyclarr and whisper "
         "deliberately notify nothing at all.")
else:
    ok("arr-notify-script-mount", "sonarr, radarr, lidarr -- all :ro")

# ==========================================================================
# Report
# ==========================================================================
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""

# ==========================================================================
# Jellyfin's log-level pin exists in the repo
# ==========================================================================
# Stage 4 of the music pipeline is observable only through one line:
#   [INF] Emby.Server.Implementations.IO.LibraryMonitor: "X" ("/p") will be refreshed.
# On 10.11.11 that is already Information level -- docs/music-pipeline-integration.md
# sec 8.3 claimed it was Debug and unverifiable, which was wrong. jellyfin/logging.json
# pins the category explicitly so a future upgrade cannot move it to Debug and take
# the pipeline's only observability with it.
#
# Jellyfin hot-reloads logging.json ONLY if it existed at the last server start, so a
# deploy that lacks the file needs one restart to adopt it. Asserting the repo copy
# exists is what stops a fresh deploy rediscovering that the hard way.
_jf_logging = "jellyfin/logging.json"
if not os.path.exists(_jf_logging):
    fail("jellyfin-logging", "ADR-0008",
         f"{_jf_logging} is missing -- Jellyfin's log levels are unpinned and the "
         "LibraryMonitor line that makes the music bridge verifiable can vanish on upgrade")
else:
    try:
        _jf_cfg = json.load(open(_jf_logging))
        _ovr = _jf_cfg["Serilog"]["MinimumLevel"]["Override"]
        _need = "Emby.Server.Implementations.IO.LibraryMonitor"
        if _ovr.get(_need) != "Information":
            fail("jellyfin-logging", "ADR-0008",
                 f"{_jf_logging} does not pin {_need} at Information "
                 f"(found {_ovr.get(_need)!r})")
        else:
            ok("jellyfin-logging", "LibraryMonitor pinned at Information")
    except (ValueError, KeyError) as exc:
        fail("jellyfin-logging", "ADR-0008", f"{_jf_logging} is not valid: {exc}")

# ==========================================================================
# 34. One door: tinyauth's credential, its socket-lessness, and its pin
# ==========================================================================
# Four separate assertions, because four different things would each quietly
# undo the design. ADR-0034.
_ta = services.get("tinyauth")
if _ta is None:
    fail("tinyauth-present", "ADR-0034",
         "there is no tinyauth service. It is the single door in front of every "
         "browser-only surface; without it every protected proxy-conf fails "
         "closed (502) and the stack has no login at all.")
else:
    _ta_env = env_of("tinyauth")

    # (a) The credential is a file, never an environment variable. An env var is
    # readable by anyone who can run `docker inspect` (ADR-0011), and the file
    # must be mounted :ro because a container that can rewrite its own
    # credential can escalate its own access (ADR-0033).
    _users_in_env = sorted(
        f"{svc}.{k}" for svc in services for k in env_of(svc)
        if k in ("TINYAUTH_AUTH_USERS", "TINYAUTH_USERS")
    )
    _usersfile = _ta_env.get("TINYAUTH_AUTH_USERSFILE")
    _users_mount = [
        v for v in (_ta.get("volumes") or [])
        if str(v.get("target", "")) == str(_usersfile or "\0")
    ]
    if _users_in_env:
        fail("tinyauth-credential-is-a-file", "ADR-0011",
             f"{_users_in_env} put the credential in an environment block. "
             "`docker inspect` hands an env var to anyone who can run it. Use "
             "TINYAUTH_AUTH_USERSFILE and `make tinyauth-users`.")
    elif not _usersfile:
        fail("tinyauth-credential-is-a-file", "ADR-0011",
             "tinyauth sets neither TINYAUTH_AUTH_USERS nor "
             "TINYAUTH_AUTH_USERSFILE, so it has no credential at all and "
             "every login attempt fails.")
    elif not _users_mount:
        fail("tinyauth-credential-is-a-file", "ADR-0011",
             f"TINYAUTH_AUTH_USERSFILE={_usersfile} but nothing is mounted "
             "there, so the container starts with no credential.")
    elif not _users_mount[0].get("read_only"):
        fail("tinyauth-credential-is-a-file", "ADR-0033",
             f"{_usersfile} is mounted read-WRITE. A container that can rewrite "
             "its own credential file can escalate its own access. Add :ro.")
    else:
        ok("tinyauth-credential-is-a-file", f"{_usersfile} mounted :ro")

    # (b) ... and the rendered file still matches .env, which is the one place a
    # human edits it. The bcrypt hash contains `$`, which Make re-expanded once
    # already (`$2: unbound variable`, 2026-09-04); this is what notices if the
    # rendering ever mangles it again, or if .env moved on without a re-render.
    _src = "secrets/tinyauth-users"
    _u = _env_file_value("TINYAUTH_USER")
    _h = _env_file_value("TINYAUTH_PASSWORD_HASH")
    if _u is None or _h is None:
        warn("tinyauth-credential-rendered", "ADR-0034",
             ".env is unreadable from here (expected in CI), so the rendered "
             f"{_src} could not be compared against it.")
    elif not os.path.exists(_src):
        fail("tinyauth-credential-rendered", "ADR-0034",
             f"{_src} does not exist. Run `make tinyauth-users`. Without it the "
             "bind mount is a DIRECTORY Docker creates, tinyauth reads no "
             "credential, and every login fails.")
    else:
        _want = f"{_u}:{_h}\n"
        _got = open(_src, encoding="utf-8", errors="replace").read()
        _mode = oct(os.stat(_src).st_mode & 0o777)
        if _got != _want:
            fail("tinyauth-credential-rendered", "ADR-0034",
                 f"{_src} does not match TINYAUTH_USER/TINYAUTH_PASSWORD_HASH in "
                 ".env. Re-run `make tinyauth-users`; if it still differs, the "
                 "`$` in the bcrypt hash is being re-expanded somewhere.")
        elif _mode != "0o600":
            fail("tinyauth-credential-rendered", "ADR-0034",
                 f"{_src} is mode {_mode}, not 0o600.")
        else:
            ok("tinyauth-credential-rendered", f"{_src} 0600, matches .env")

    # (c) No label provider, therefore no Docker socket and no dockerproxy
    # route. Tinyauth's default LABELPROVIDER is `auto`, which discovers per-app
    # ACLs from Docker labels and so needs the socket. dockerproxy is the sole
    # socket holder (ADR-0013) and was narrowed to what autoheal alone needs
    # (ADR-0025); this is the assertion that keeps it that way.
    _lp = _ta_env.get("TINYAUTH_LABELPROVIDER")
    _sock = [v for v in (_ta.get("volumes") or [])
             if "docker.sock" in str(v.get("source", ""))]
    _proxy_route = [
        k for k, v in _ta_env.items()
        if v and "dockerproxy" in str(v)
    ]
    if _lp != "none":
        fail("tinyauth-no-socket", "ADR-0013",
             f"TINYAUTH_LABELPROVIDER is {_lp!r}, not 'none'. `auto` and "
             "`docker` discover per-app ACLs from Docker LABELS, which needs the "
             "Docker socket. Express per-app rules as TINYAUTH_APPS_[NAME]_* "
             "variables instead.")
    elif _sock:
        fail("tinyauth-no-socket", "ADR-0013",
             f"tinyauth mounts {_sock}. dockerproxy is the only container in "
             "this stack permitted to touch the Docker socket.")
    elif _proxy_route:
        fail("tinyauth-no-socket", "ADR-0025",
             f"tinyauth points {_proxy_route} at dockerproxy. The proxy was "
             "narrowed to CONTAINERS/POST/PING/VERSION for autoheal alone; an "
             "auth server has no business there.")
    else:
        ok("tinyauth-no-socket", "LABELPROVIDER=none, no socket, no proxy route")

    # (d) Its healthcheck probes nothing outside its own container. The image
    # ships `tinyauth healthcheck`, which GETs its own 127.0.0.1:3000. A probe
    # that reaches an OAuth provider or another container turns someone else's
    # outage into every door closing. ADR-0009.
    _hc = _ta.get("healthcheck") or {}
    _test = " ".join(str(x) for x in (_hc.get("test") or []))
    _external = [tok for tok in ("http://", "https://") if tok in _test] and not any(
        h in _test for h in ("127.0.0.1", "localhost", "::1"))
    if _hc.get("disable"):
        fail("tinyauth-healthcheck-local", "ADR-0009",
             "tinyauth has no healthcheck, so swag's depends_on cannot wait for "
             "it and autoheal cannot restart it.")
    elif _external:
        fail("tinyauth-healthcheck-local", "ADR-0009",
             f"tinyauth's healthcheck reaches outside its own container: {_test!r}. "
             "A dependency in a healthcheck makes someone else's outage close "
             "every protected door.")
    else:
        ok("tinyauth-healthcheck-local", _test or "container-local")

    # (e) The tag is pinned, and the reason is written down. A floating auth
    # container is one bad upstream release away from closing every door.
    _img = str(_ta.get("image", ""))
    _tag = _img.rsplit(":", 1)[-1] if ":" in _img else ""
    if _tag in ("", "latest", "nightly") or not re.match(r"^v?\d+\.\d+\.\d+", _tag):
        fail("tinyauth-pinned", "ADR-0006",
             f"tinyauth's image is {_img!r}. Pin a concrete version: a bad auth "
             "container closes every protected door at once, so this update is "
             "chosen, never inherited.")
    elif "tinyauth" not in MANUAL_UPDATE_ONLY:
        fail("tinyauth-pinned", "ADR-0024",
             "tinyauth is pinned but absent from MANUAL_UPDATE_ONLY, so nothing "
             "records why a human must apply that update deliberately.")
    else:
        ok("tinyauth-pinned", f"{_tag}, MANUAL_UPDATE_ONLY")

# ==========================================================================
# 34b. The two forward-auth confs are tracked here and mounted :ro
# ==========================================================================
# These are /config/nginx/*.conf, not proxy-confs/*, so ADR-0022's mount and
# its conf<->label reconciliation never covered them -- and the copy that was
# live in this container was the 2025/06/08 sample, five revisions behind the
# 2025/12/17 one in the image and missing the identity headers entirely. A
# protected route whose auth conf lives only inside the container is a route
# that silently unprotects itself on the next recreate. ADR-0022, ADR-0034.
_AUTH_CONFS = ("tinyauth-server.conf", "tinyauth-location.conf")
_swag_all_mounts = {
    str(v.get("target", "")): v
    for v in (services.get("swag", {}).get("volumes") or [])
}
_conf_problems = []
for _name in _AUTH_CONFS:
    _repo = f"swag/{_name}"
    _target = f"/config/nginx/{_name}"
    if not os.path.isfile(_repo):
        _conf_problems.append(f"{_repo} is not in the repo")
        continue
    _m = _swag_all_mounts.get(_target)
    if _m is None:
        _conf_problems.append(f"{_repo} exists but nothing mounts it at {_target}")
    elif os.path.basename(str(_m.get("source", ""))) != _name:
        _conf_problems.append(
            f"{_target} is mounted from {_m.get('source')!r}, not from {_repo}")
    elif not _m.get("read_only"):
        _conf_problems.append(f"{_target} is mounted read-WRITE; add :ro")
if _conf_problems:
    fail("auth-confs-are-tracked", "ADR-0022",
         "; ".join(_conf_problems) + ". The conf IS the mechanism: an auth conf "
         "that only exists inside the container is a door that opens itself on "
         "the next `docker compose up -d swag`.")
else:
    ok("auth-confs-are-tracked", f"{len(_AUTH_CONFS)} confs tracked and :ro")

# ... and the identity headers in the tracked location conf are set
# UNCONDITIONALLY. proxy_set_header REPLACES whatever the client sent, and nginx
# omits a header whose value is empty -- so a request arriving with
# `Remote-User: admin` reaches the app with no Remote-User at all. Wrapping any
# of these in an `if`, or dropping one because "nothing reads it", reopens that
# hole for whichever app starts trusting the header later. ADR-0034 §2.7.
_IDENTITY_HEADERS = ("Remote-Email", "Remote-Groups", "Remote-Name", "Remote-User")
_loc = "swag/tinyauth-location.conf"
if not os.path.isfile(_loc):
    pass  # already failed above
else:
    _txt = open(_loc, encoding="utf-8", errors="replace").read()
    # Strip comments so a header named in prose does not count as a directive.
    _live = "\n".join(ln for ln in _txt.splitlines()
                      if not ln.lstrip().startswith("#"))
    _missing_hdr = [h for h in _IDENTITY_HEADERS
                    if not re.search(rf"^\s*proxy_set_header\s+{h}\s+\$\S+;",
                                     _live, re.M)]
    _conditional = "if " in _live or re.search(r"^\s*if\s*\(", _live, re.M)
    if _missing_hdr:
        fail("auth-identity-headers", "ADR-0034",
             f"{_loc} does not unconditionally set {_missing_hdr}. Without a "
             "proxy_set_header for a header an app might trust, a client can "
             "supply it itself and the auth subrequest never gets a say.")
    elif _conditional:
        fail("auth-identity-headers", "ADR-0034",
             f"{_loc} contains an `if`. Every proxy_set_header here must be "
             "unconditional -- a conditional one leaves a path on which the "
             "client's own header survives.")
    elif not re.search(r"^\s*auth_request\s+/tinyauth;", _live, re.M):
        fail("auth-identity-headers", "ADR-0034",
             f"{_loc} sets identity headers but issues no `auth_request`, so "
             "the headers are whatever the previous subrequest left behind.")
    else:
        ok("auth-identity-headers", "4 headers, unconditional, after auth_request")

# ... and the login URL is named in exactly one place. The conf computes
# `https://auth.$domain` as its fallback and TINYAUTH_APPURL is what tinyauth
# puts in its own X-Tinyauth-Location header; two places naming the login page
# is how a redirect loop happens.
_appurl = env_of("tinyauth").get("TINYAUTH_APPURL", "") if "tinyauth" in services else ""
_srv = "swag/tinyauth-server.conf"
if _appurl and os.path.isfile(_srv):
    _srv_txt = open(_srv, encoding="utf-8", errors="replace").read()
    _m = re.search(r"^\s*set\s+\$signin_url\s+https://([A-Za-z0-9-]+)\.\$domain",
                   _srv_txt, re.M)
    _conf_host = _m.group(1) if _m else None
    _url_host = re.sub(r"^https?://", "", _appurl).split("/")[0]
    _url_label = _url_host.split(".", 1)[0]
    if _conf_host is None:
        fail("auth-login-url-agrees", "ADR-0034",
             f"{_srv} has no `set $signin_url https://<label>.$domain` fallback, "
             "so a 401 with no X-Tinyauth-Location header would redirect to "
             "nowhere -- a broken door that still answers 302.")
    elif _conf_host != _url_label:
        fail("auth-login-url-agrees", "ADR-0034",
             f"{_srv} redirects to '{_conf_host}.<domain>' but TINYAUTH_APPURL is "
             f"'{_appurl}' ('{_url_label}.'). They must name the same host or an "
             "expired session bounces between two of them.")
    else:
        ok("auth-login-url-agrees", f"{_conf_host}. == TINYAUTH_APPURL")

# ==========================================================================
# 34c. Every `protect` route has its door; no `never` route has one
# ==========================================================================
# This is the assertion that catches the actual failure mode: a route that
# loses its auth include and answers 200 to the whole internet, which nothing
# else here would notice. Both directions, like ADR-0022's label<->conf
# reconciliation one layer up. ADR-0034.
_LOC_INC = "include /config/nginx/tinyauth-location.conf;"
_SRV_INC = "include /config/nginx/tinyauth-server.conf;"

def _conf_locations(text):
    """[(header, body)] for each top-level `location ... { }` in a server block.

    Brace-counted rather than regex-matched: a location body contains braces
    (map blocks, if blocks), and a naive regex stops at the first one.
    """
    out = []
    for m in re.finditer(r"^(\s*)location\s+([^\{]*)\{", text, re.M):
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        out.append((m.group(2).strip(), text[m.end():i]))
    return out

_undoored, _overdoored, _halfdoored, _absent = [], [], [], []
for _route, _decision in sorted(DOOR.items()):
    _path = f"swag/proxy-confs/{_route}.subdomain.conf"
    if not os.path.isfile(_path):
        _absent.append(_route)
        continue
    _txt = open(_path, encoding="utf-8", errors="replace").read()
    _live = "\n".join(ln for ln in _txt.splitlines()
                      if not ln.lstrip().startswith("#"))
    _locs = _conf_locations(_live)
    _root = [b for h, b in _locs if h == "/"]
    _has_srv = _SRV_INC in _live
    # the location include, counted only inside `location /`
    _has_loc = any(_LOC_INC in b for b in _root)
    _any_loc = _LOC_INC in _live

    if _decision == "never":
        if _has_loc or _any_loc or _has_srv:
            _overdoored.append(_route)
    elif _route in DOOR_PENDING:
        # not migrated yet -- must not be half-migrated either
        if _has_loc or _any_loc:
            _halfdoored.append(f"{_route} (listed as PENDING but already has the door)")
    elif not _has_loc:
        _undoored.append(_route)
    elif not _has_srv:
        _halfdoored.append(f"{_route} (location include without the server include)")

if _absent:
    fail("door-reconciliation", "ADR-0034",
         f"{_absent} are classified in DOOR but have no proxy-conf. Either the "
         "route was deleted (drop the DOOR entry) or the conf was lost.")
elif _undoored:
    fail("door-reconciliation", "ADR-0034",
         f"{_undoored} are classified `protect` but their `location /` does not "
         f"include tinyauth-location.conf. That route answers the whole internet "
         "with a 200 and nothing else here would notice.")
elif _overdoored:
    fail("door-reconciliation", "ADR-0034",
         f"{_overdoored} are classified `never` but reference the auth conf. "
         "Read ADR-0034 before changing this: each of those has clients that "
         "cannot follow a 302, and ntfy specifically must stay open or a broken "
         "door becomes a SILENT one.")
elif _halfdoored:
    fail("door-reconciliation", "ADR-0034", "; ".join(_halfdoored) +
         ". A location include without the server include means the 401 has "
         "nowhere to redirect to; nginx fails the request instead of showing a "
         "login page.")
else:
    _n_prot = sum(1 for r, d in DOOR.items()
                  if d == "protect" and r not in DOOR_PENDING)
    _n_never = sum(1 for d in DOOR.values() if d == "never")
    _msg = f"{_n_prot} doored, {_n_never} deliberately open"
    if DOOR_PENDING:
        _msg += f", {len(DOOR_PENDING)} still to migrate"
        warn("door-reconciliation", "ADR-0034",
             f"{sorted(DOOR_PENDING)} are classified `protect` but not migrated "
             "yet. This warning is the migration's own progress bar and must "
             "reach zero.")
    else:
        ok("door-reconciliation", _msg)

# ... and every published conf is classified. An unclassified one is a public
# route nobody decided about, which is how eleven of them ended up with no
# authentication at all.
_published = {
    f[: -len(".subdomain.conf")]
    for f in (os.listdir("swag/proxy-confs") if os.path.isdir("swag/proxy-confs") else [])
    if f.endswith(".subdomain.conf")
}
_unclassified = sorted(_published - set(DOOR))
if _unclassified:
    fail("door-classification-complete", "ADR-0034",
         f"{_unclassified} are published but absent from DOOR. Every public route "
         "gets a decision -- `protect` or `never` with a reason. Silence is how "
         "eleven of these ended up with no authentication at all.")
else:
    ok("door-classification-complete", f"{len(_published)} routes, all classified")

# ==========================================================================
# 34d. No nginx conf in this repo authenticates anyone by itself
# ==========================================================================
# One door means one door. A live `auth_basic` line is a second credential
# store, and it is worse than a duplicate: ngx_http_auth_basic_module runs
# BEFORE ngx_http_auth_request_module in the access phase, so its 401 is what
# `error_page 401 = @tinyauth_login` converts -- the tinyauth subrequest is
# never made, and a valid session gets bounced to the login page forever.
# Measured 2026-09-04 on ongehoord, which is why the two lines came out in the
# same commit that added the include. ADR-0034.
#
# Commented-out lines are fine and deliberately ignored: SWAG's upstream
# samples ship `#auth_basic "Restricted";` in every proxy-conf, and rewriting
# 17 files to delete a comment would be churn that hides the real signal.
_authbasic = []
for _root, _dirs, _files in os.walk("swag"):
    for _f in sorted(_files):
        if not _f.endswith(".conf"):
            continue
        _p = os.path.join(_root, _f)
        for _i, _ln in enumerate(
                open(_p, encoding="utf-8", errors="replace"), start=1):
            if _ln.lstrip().startswith("#"):
                continue
            if re.search(r"\bauth_basic(_user_file)?\b", _ln):
                _authbasic.append(f"{_p}:{_i}")
if _authbasic:
    fail("no-auth-basic", "ADR-0034",
         f"{_authbasic} carry a live auth_basic directive. It does not stack "
         "with forward auth -- it PREEMPTS it, so the route ends up gated by "
         "basic auth alone with the tinyauth login page as its 401 handler, "
         "and a valid session can never get in.")
else:
    ok("no-auth-basic", "no live auth_basic in any tracked conf")

# ==========================================================================
# 35. Nothing under secrets/ is tracked by git
# ==========================================================================
# secrets/ is gitignored, which is necessary and not sufficient: `git add -f`
# and a stale index entry both defeat it, and this repo has already had a
# `.sudo-pwd` sitting one `git add -A` away from the history. The credential for
# the entire public surface lives here now. ADR-0034.
_tracked_secrets = subprocess.run(
    ["git", "ls-files", "secrets"], capture_output=True, text=True,
).stdout.split()
if _tracked_secrets:
    fail("secrets-not-tracked", "ADR-0034",
         f"git tracks {_tracked_secrets}. These are credentials. "
         "`git rm --cached <path>` and confirm they never reached a pushed commit.")
else:
    ok("secrets-not-tracked", "nothing under secrets/ is tracked")


# ==========================================================================
# The cron-fleet tables in the pipeline doc are generated, not hand-written
# ==========================================================================
# They pointed at the wrong script twice -- section 5 had complete_sweep and
# cleanup swapped, section 9 listed one daily library scan where there are three
# weekly ones and four flock holders where there are five. Both times the
# docstrings were right. A table describing 29 jobs will drift again; this is
# what notices.
_gen = subprocess.run([sys.executable, "scripts/gen_pipeline_tables.py", "--check"],
                      capture_output=True, text=True)
if _gen.returncode == 0:
    ok("pipeline-tables", "cron-fleet tables match cron/crontab")
else:
    fail("pipeline-tables", "ADR-0003",
         (_gen.stderr or _gen.stdout).strip().replace("\n", " ")[:200]
         or "docs/music-pipeline-integration.md cron tables are stale; "
            "run scripts/gen_pipeline_tables.py --write")

if verbose:
    for check, msg in passes:
        print(f"{GREEN}  ok{RESET}   {check:22} {DIM}{msg}{RESET}")

for check, adr, msg in warnings:
    print(f"{YELLOW}  WARN{RESET} {check:22} [{adr}] {msg}")

for check, adr, msg in failures:
    print(f"{RED}  FAIL{RESET} {check:22} [{adr}] {msg}")

n_checks = len(passes) + len(failures)
print()
if failures:
    print(f"{RED}invariant check FAILED{RESET}: {len(failures)} violation(s), "
          f"{len(warnings)} warning(s), across {len(services)} services.")
    print("Read the ADR named on each line before changing the invariant. "
          "docs/decisions/")
    sys.exit(1)

print(f"{GREEN}invariants hold{RESET}: {n_checks} assertions over "
      f"{len(services)} services, {len(warnings)} warning(s).")
if warnings:
    print(f"{DIM}Warnings are known gaps with an ADR, not exemptions.{RESET}")
sys.exit(0)
PY
