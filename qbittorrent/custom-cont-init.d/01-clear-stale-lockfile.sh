#!/usr/bin/with-contenv bash
# shellcheck shell=bash
#
# Clear a *stale* qBittorrent single-instance lock at container init.
#
# Background
# ----------
# qBittorrent 5.2.0 replaced the old fcntl-based QtLockedFile lock with Qt's
# QLockFile. The new lockfile is 5 lines of text:
#
#     1: PID
#     2: process name        (qbittorrent-nox)
#     3: hostname
#     4: (blank)
#     5: machine-id
#
# Two upstream bugs make a leftover lockfile fatal rather than self-healing:
#
#   * qbittorrent/qBittorrent#24164 - a 0-byte lockfile left by 5.1.x makes
#     5.2.0 abort with "Another qBittorrent instance is already running."
#     Fixed in 5.2.1 (PR #24218).
#   * qbittorrent/qBittorrent#24357 - the new-format lockfile breaks across a
#     Docker recreate. A container's hostname defaults to its container ID,
#     which changes on every recreate, so QLockFile sees an unfamiliar hostname,
#     cannot prove the lock is stale, and refuses to start. Fixed in 5.2.2
#     (PR #24363). Also mitigated here by pinning `hostname: qbittorrent` in
#     compose/media-download.yaml so the hostname is stable across recreates.
#
# When qbit refuses to start, qbittorrent-nox exits immediately, s6 restarts it
# about once a second, and the container sits at "Up (unhealthy)" forever. The
# tell in the log is a run of "qBittorrent vX.Y.Z started. Process ID: N" lines
# with N climbing steadily (qbittorrent/qBittorrent#24405).
#
# Why this is conditional, not `rm -f`
# ------------------------------------
# An earlier version of this script deleted the lockfile unconditionally on the
# reasoning that qbittorrent-nox has not started yet at custom-init time, so any
# lock must be stale. That holds for one container over one config dir, but it
# silently destroys the protection the lock exists to provide if two containers
# are ever pointed at the same /config (a copy-paste of the service block, a
# half-finished migration). A lock held by a genuinely live instance must
# survive. So: prove staleness, then delete.
#
# A lock is treated as stale when ANY of these is true:
#   * the file is 0 bytes                       -> old 5.1.x format (#24164)
#   * the hostname line != this container's     -> written by a different host
#   * the recorded PID is not a live            -> owner is gone
#     qbittorrent-nox
#
# Otherwise the lock is left alone and qBittorrent decides for itself.
#
# The ipc-socket next to the lockfile is removed only when its lockfile was
# judged stale, for the same reason.
#
# LIMIT OF THE SAFETY PROPERTY - read before trusting it
# ------------------------------------------------------
# "A live instance's lock survives" holds for ONE container over this /config.
# It does NOT hold for two, and pinning `hostname: qbittorrent` in compose is
# what breaks it: PIDs in the lockfile are namespace-local, so a PID written by
# container A means nothing when read inside container B, while the pinned
# hostname makes rule 2 match instead of firing. B therefore judges A's live
# lock stale and deletes it.
#
# This is not unique to this script - upstream's PID + hostname + machine-id
# check has the same hole, and the unconditional `rm -f` this replaced was
# strictly worse (it deleted in every case, single-container included). The
# machine-id on line 5 is the only field that could tell two containers apart,
# and neither upstream nor this script currently reads it.
#
# So: never point two containers at one qBittorrent /config. That was always
# true; it is simply no longer defended against.

shopt -s nullglob

readonly SELF='[init-qbit-lockfile]'
this_host="$(hostname)"
removed=0

# qBittorrent's profile layout has moved between versions, so check every
# location the lock is known to appear at rather than assuming one.
lock_paths=(
    /config/qBittorrent/lockfile
    /config/qBittorrent/config/lockfile
    /config/.config/qBittorrent/lockfile
)

# is_live_qbit <pid> - true if pid is a running qbittorrent-nox in this namespace
is_live_qbit() {
    local pid=$1
    [[ -n ${pid} && ${pid} =~ ^[0-9]+$ ]] || return 1
    [[ -r /proc/${pid}/comm ]] || return 1
    [[ "$(< "/proc/${pid}/comm")" == 'qbittorrent-nox' ]]
}

for lock in "${lock_paths[@]}"; do
    [[ -e ${lock} ]] || continue

    reason=''
    if [[ ! -s ${lock} ]]; then
        reason='0-byte lockfile (pre-5.2.0 format, upstream #24164)'
    else
        lock_pid="$(sed -n '1p' "${lock}" | tr -d '[:space:]')"
        lock_host="$(sed -n '3p' "${lock}" | tr -d '[:space:]')"

        if [[ ${lock_host} != "${this_host}" ]]; then
            reason="hostname mismatch: lockfile says '${lock_host}', we are '${this_host}' (upstream #24357)"
        elif ! is_live_qbit "${lock_pid}"; then
            reason="recorded PID ${lock_pid:-<empty>} is not a live qbittorrent-nox"
        fi
    fi

    if [[ -n ${reason} ]]; then
        echo "${SELF} removing stale lock ${lock}: ${reason}"
        rm -f "${lock}"
        removed=$((removed + 1))

        # Only meaningful once we know the owning instance is gone.
        sock="$(dirname "${lock}")/ipc-socket"
        if [[ -e ${sock} ]]; then
            echo "${SELF} removing stale ipc-socket ${sock}"
            rm -f "${sock}"
        fi
    else
        echo "${SELF} keeping ${lock}: held by live qbittorrent-nox PID ${lock_pid} on ${lock_host}"
    fi
done

if (( removed == 0 )); then
    echo "${SELF} no stale lock found; nothing to clean"
else
    echo "${SELF} cleared ${removed} stale lock(s)"
fi

echo "${SELF} stale-lock check complete"
