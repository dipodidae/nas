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
}

def is_locally_built(svc):
    return "build" in services[svc]

# KNOWN GAP, not an exemption: these do not drop capabilities. ADR-0018.
# Warned about on every run so it cannot quietly become the convention.
CAP_DROP_WAIVER = {
    "playlist-generator":    "ADR-0018 -- nginx needs NET_BIND_SERVICE; set not yet determined",
    "playlist-generator-db": "ADR-0018 -- pg entrypoint chowns PGDATA; set not yet determined",
}

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
