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

# Services deliberately NOT labelled for watchtower. This list is what lets the
# checker tell "deliberately unlabeled" from "someone forgot the label".
# ADR-0006. (Kept here rather than as an x- field in the compose files: a
# service-level x- key shows up in `docker compose config` and risks
# perturbing the config hash that decides whether `up -d` recreates. ADR-0000.)
# Services that pull an image but must not be auto-updated. Locally-built
# services are NOT listed here: "has a build: section" already proves
# watchtower cannot pull it, so that opt-out is derived rather than declared
# (which means a newly-added local project needs no edit to this list).
WATCHTOWER_OPTOUT = {
    "qbittorrent":           "pinned tag; watchtower's non-atomic recreate deleted it for 13h",
    "jellyfin":              "worst service to lose to a failed recreate; slow to stop",
    "dockerproxy":           "watchtower must not restart its own dependency",
    "watchtower":            "must not self-update",
    "autoheal":              "control plane",
    "playlist-generator-db": "never auto-update a database engine under its data",
    "scrutiny":              "omnibus bundles InfluxDB; same rule as playlist-generator-db",
    "diun":                  "pinned; it reports its OWN updates via the manifest",
}

def is_locally_built(svc):
    return "build" in services[svc]

# KNOWN GAP, not an exemption: these do not drop capabilities. ADR-0018.
# Warned about on every run so it cannot quietly become the convention.
# 2026-09-02: both closed with measured capability sets -- the dict is
# deliberately left in place (empty) so the next gap has an obvious home.
CAP_DROP_WAIVER = {}

# Ports that are public on purpose. Anything else must bind 127.0.0.1.
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
# 3. Watchtower labels: qbittorrent and jellyfin must carry none; every other
#    opt-out must be a documented one, and everything else must be labelled.
# ==========================================================================
WT = "com.centurylinklabs.watchtower.enable"
for svc in ("qbittorrent", "jellyfin"):
    if WT in labels(svc):
        fail("watchtower-optout", "ADR-0006",
             f"{svc} carries the watchtower enable label. Its non-atomic "
             "stop->remove->create leaves NO container when the remove fails "
             "(qbittorrent, 13h, 2026-09-01). Update it by hand instead.")
    else:
        ok("watchtower-optout", f"{svc} unlabelled")

for svc in sorted(services):
    labelled = labels(svc).get(WT) == "true"
    if labelled and is_locally_built(svc):
        fail("watchtower-optout", "ADR-0006",
             f"{svc} is locally built (has a build: section) but carries the "
             "watchtower enable label. Watchtower cannot pull a local image, so "
             "the label buys nothing and adds recreate risk.")
    elif labelled and svc in WATCHTOWER_OPTOUT:
        fail("watchtower-optout", "ADR-0006",
             f"{svc} is on the documented opt-out list "
             f"({WATCHTOWER_OPTOUT[svc]}) but carries the enable label.")
    elif not labelled and not is_locally_built(svc) and svc not in WATCHTOWER_OPTOUT:
        fail("watchtower-coverage", "ADR-0006",
             f"{svc} has no watchtower label, is not locally built, and is not "
             "on the documented opt-out list in this script. Either label it, "
             "or add it to WATCHTOWER_OPTOUT with a reason so 'deliberate' is "
             "distinguishable from 'forgotten'.")

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
    "lidarr": "ADR-0003",   # staged but must stay mounted
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
    _tracked = (os.path.isfile(f"swag/proxy-confs/{name}.subdomain.conf")
                or any(m.startswith(f"{name}.") for m in _swag_mounts))
    _live = bool(_swag_dir) and os.path.isfile(
        os.path.join(_swag_dir, f"{name}.subdomain.conf"))
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
_undeclared = sorted(
    f[: -len(".subdomain.conf")]
    for f in (os.listdir("swag/proxy-confs") if os.path.isdir("swag/proxy-confs") else [])
    if f.endswith(".subdomain.conf") and f[: -len(".subdomain.conf")] not in _labelled
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
# Report
# ==========================================================================
GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""

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
