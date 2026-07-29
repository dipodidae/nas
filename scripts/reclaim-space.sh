#!/usr/bin/env bash
#
# reclaim-space.sh — audit and reclaim disk space on the NVMe root filesystem.
#
# Default action is a read-only audit. Nothing is deleted unless you pass a
# tier flag. Every tier prints what it will do and asks for confirmation
# unless --yes is given.
#
#   ./reclaim-space.sh                 # audit only (safe, read-only)
#   ./reclaim-space.sh --safe          # docker build cache + dangling images
#   ./reclaim-space.sh --caches        # regenerable dev/tool caches
#   ./reclaim-space.sh --appdata       # lidarr artwork cache, relocate backups
#   ./reclaim-space.sh --all           # safe + caches + appdata
#   ./reclaim-space.sh --jellyfin-metadata   # opt-in only, see warning below
#   ./reclaim-space.sh --all --yes     # no prompts
#
set -uo pipefail

NAS="/home/tom/nas"
CFG="$NAS/.docker-config"
ARCHIVE="/mnt/drive/backups"        # 9.1T spinning disk, 5.7T free
TARGET_FS="/"

DO_SAFE=0; DO_CACHES=0; DO_APPDATA=0; DO_JELLYFIN=0; ASSUME_YES=0; ANY_TIER=0

for arg in "$@"; do
  case "$arg" in
    --safe)               DO_SAFE=1; ANY_TIER=1 ;;
    --caches)             DO_CACHES=1; ANY_TIER=1 ;;
    --appdata)            DO_APPDATA=1; ANY_TIER=1 ;;
    --jellyfin-metadata)  DO_JELLYFIN=1; ANY_TIER=1 ;;
    --all)                DO_SAFE=1; DO_CACHES=1; DO_APPDATA=1; ANY_TIER=1 ;;
    --yes|-y)             ASSUME_YES=1 ;;
    --audit)              ANY_TIER=0 ;;
    -h|--help)            sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m%s\033[0m\n' "$*"; }
warn()  { printf '\033[33m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }

# Size of a path in bytes, 0 if missing/unreadable.
size_of() { [ -e "$1" ] || { echo 0; return; }; du -sxb "$1" 2>/dev/null | awk '{print $1}' || echo 0; }
human()   { numfmt --to=iec --suffix=B "${1:-0}" 2>/dev/null || echo "${1:-0}B"; }
avail()   { df -B1 --output=avail "$TARGET_FS" | tail -1 | tr -d ' '; }

confirm() {
  [ "$ASSUME_YES" -eq 1 ] && return 0
  read -r -p "  → $1 [y/N] " a </dev/tty
  [[ "$a" =~ ^[Yy]$ ]]
}

START_AVAIL=$(avail)

# ---------------------------------------------------------------- audit ----
bold "=== DISK AUDIT $(date '+%Y-%m-%d %H:%M') ==="
df -h "$TARGET_FS" | tail -1 | awk '{printf "  root: %s used of %s (%s), %s free\n", $3,$2,$5,$4}'
df -h /mnt/drive 2>/dev/null | tail -1 | awk '{printf "  hdd : %s used of %s (%s), %s free\n", $3,$2,$5,$4}'
echo

bold "Docker"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker system df 2>/dev/null | sed 's/^/  /'
  echo "  dangling images: $(docker images -f dangling=true -q | wc -l)"
else
  warn "  docker unavailable — skipping docker sections"
fi
echo

bold "Largest reclaimable paths"
for p in \
  "$CFG/jellyfin/data/metadata" \
  "$CFG/lidarr/MediaCover" \
  "$CFG/lidarr/Backups" \
  "$CFG/jellyfin/cache" \
  "$HOME/.cache/sacad" \
  "$HOME/.local/share/pnpm" \
  "$HOME/.vscode-server/cli/servers" \
  "$HOME/.cache/pnpm" \
  "$HOME/.cache/ms-playwright" \
  "$HOME/.npm" \
  "$CFG/lidarr/logs" \
  "$NAS/logs" \
  "$HOME/.cache/pip" \
  "$HOME/.Trash"
do
  s=$(size_of "$p"); [ "$s" -gt 0 ] && printf "  %10s  %s\n" "$(human "$s")" "${p/#$HOME/~}"
done | sort -rh -k1
echo

if [ "$ANY_TIER" -eq 0 ]; then
  dim "Read-only audit. Re-run with --safe / --caches / --appdata / --all to reclaim."
  echo
  bold "Needs sudo (run manually):"
  echo "  sudo apt-get clean                  # ~563M apt package cache"
  echo "  sudo journalctl --vacuum-time=7d    # ~40M journals"
  echo "  sudo apt-get autoremove --purge     # old kernels (3 installed)"
  exit 0
fi

# ------------------------------------------------------- tier 1: safe ------
if [ "$DO_SAFE" -eq 1 ] && docker info >/dev/null 2>&1; then
  bold "[safe] Docker build cache"
  dim "  Fully reclaimable. Cost: next builds re-download/re-run layers."
  if confirm "docker builder prune -af"; then
    docker builder prune -af 2>&1 | tail -2 | sed 's/^/  /'
  fi

  bold "[safe] Dangling images"
  n=$(docker images -f dangling=true -q | wc -l)
  dim "  $n untagged images left behind by rebuilds. Nothing references them."
  if [ "$n" -gt 0 ] && confirm "docker image prune -f"; then
    docker image prune -f 2>&1 | tail -1 | sed 's/^/  /'
  fi

  bold "[safe] Stopped-container leftovers"
  if confirm "docker container prune -f"; then
    docker container prune -f 2>&1 | tail -1 | sed 's/^/  /'
  fi
  echo
fi

# ----------------------------------------------------- tier 2: caches ------
if [ "$DO_CACHES" -eq 1 ]; then
  bold "[caches] sacad album-art cache"
  s=$(size_of "$HOME/.cache/sacad/sacad-cache.sqlite")
  if [ "$s" -gt 0 ]; then
    dim "  $(human "$s") sqlite cache. VACUUM is non-destructive and usually enough."
    if command -v sqlite3 >/dev/null; then
      if confirm "VACUUM sacad-cache.sqlite"; then
        sqlite3 "$HOME/.cache/sacad/sacad-cache.sqlite" "VACUUM;" 2>&1 | sed 's/^/  /'
        green "  now $(human "$(size_of "$HOME/.cache/sacad/sacad-cache.sqlite")")"
      fi
    else
      warn "  sqlite3 not installed — cannot vacuum. sudo apt install sqlite3"
      # Deleting is destructive (re-downloads all cover art), so never do it
      # implicitly under --yes. Require a deliberate interactive answer.
      if [ "$ASSUME_YES" -eq 1 ]; then
        dim "  skipping destructive delete under --yes; run without --yes to choose it"
      elif confirm "DELETE sacad cache entirely (re-downloads cover art on next run)"; then
        rm -f "$HOME/.cache/sacad/sacad-cache.sqlite"
      fi
    fi
  fi

  bold "[caches] Package manager caches"
  if confirm "prune pnpm store + npm/pip caches"; then
    command -v pnpm >/dev/null && pnpm store prune 2>&1 | tail -2 | sed 's/^/  /'
    command -v npm  >/dev/null && npm cache clean --force 2>/dev/null && echo "  npm cache cleaned"
    command -v pip  >/dev/null && pip cache purge 2>/dev/null | sed 's/^/  /'
    rm -rf "$HOME/.cache/typescript" 2>/dev/null && echo "  typescript cache cleared"
  fi

  bold "[caches] Old VS Code remote servers"
  srv="$HOME/.vscode-server/cli/servers"
  if [ -d "$srv" ]; then
    keep=$(ls -1dt "$srv"/Stable-* 2>/dev/null | head -1)
    old=$(ls -1dt "$srv"/Stable-* 2>/dev/null | tail -n +2)
    if [ -n "$old" ]; then
      cnt=$(echo "$old" | wc -l)
      dim "  keeping newest: $(basename "$keep")"
      dim "  removing $cnt older build(s)"
      if confirm "delete $cnt old vscode server build(s)"; then
        echo "$old" | while read -r d; do [ -n "$d" ] && rm -rf "$d"; done
        echo "  done"
      fi
    fi
  fi
  echo
fi

# ---------------------------------------------------- tier 3: appdata ------
if [ "$DO_APPDATA" -eq 1 ]; then
  bold "[appdata] Lidarr MediaCover (artwork cache)"
  s=$(size_of "$CFG/lidarr/MediaCover")
  if [ "$s" -gt 0 ]; then
    dim "  $(human "$s"). Lidarr re-downloads artwork on demand. Safe but causes"
    dim "  a burst of image requests as you browse the library afterwards."
    if confirm "delete lidarr MediaCover"; then
      rm -rf "$CFG/lidarr/MediaCover" && mkdir -p "$CFG/lidarr/MediaCover"
      green "  freed $(human "$s")"
    fi
  fi

  bold "[appdata] Relocate Lidarr backups to the 9.1T HDD"
  dim "  These are real backups — moved, never deleted."
  dim "  (Nightly config archives already write straight to $ARCHIVE/nas-configs"
  dim "   via cron, so they never land on the NVMe. Lidarr writes its own"
  dim "   backups into /config internally, so they still need sweeping.)"
  if confirm "move lidarr backups to $ARCHIVE/lidarr"; then
    mkdir -p "$ARCHIVE/lidarr"
    # Lidarr scheduled backups: keep the newest local, archive the rest.
    if [ -d "$CFG/lidarr/Backups/scheduled" ]; then
      ls -1t "$CFG/lidarr/Backups/scheduled"/*.zip 2>/dev/null | tail -n +2 | while read -r f; do
        [ -n "$f" ] && mv -n "$f" "$ARCHIVE/lidarr/" && echo "  archived $(basename "$f")"
      done
    fi
  fi

  bold "[appdata] Jellyfin transcode/cache scratch"
  s=$(size_of "$CFG/jellyfin/cache")
  if [ "$s" -gt 0 ] && confirm "clear jellyfin cache ($(human "$s"))"; then
    find "$CFG/jellyfin/cache" -mindepth 1 -delete 2>/dev/null
    green "  freed $(human "$s")"
  fi

  bold "[appdata] Rotated logs older than 14 days"
  if confirm "delete old .log/.txt/.zip rotations under $CFG and $NAS/logs"; then
    find "$CFG" "$NAS/logs" -type f \( -name '*.log.*' -o -name '*.txt.*' -o -name '*.log.zip' \) \
      -mtime +14 -delete 2>/dev/null
    echo "  done"
  fi
  echo
fi

# ------------------------------------------- opt-in: jellyfin metadata -----
if [ "$DO_JELLYFIN" -eq 1 ]; then
  s=$(size_of "$CFG/jellyfin/data/metadata")
  bold "[opt-in] Jellyfin metadata ($(human "$s"))"
  warn "  This forces a full library metadata re-scrape: hours of work, and any"
  warn "  manually-set artwork or edits not written back to NFO files is LOST."
  warn "  Stop the container first: docker stop jellyfin"
  if confirm "I understand — delete jellyfin metadata"; then
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx jellyfin; then
      warn "  jellyfin is still running — aborting. Stop it and re-run."
    else
      rm -rf "$CFG/jellyfin/data/metadata" && mkdir -p "$CFG/jellyfin/data/metadata"
      green "  freed $(human "$s")"
    fi
  fi
  echo
fi

# --------------------------------------------------------------- report ----
END_AVAIL=$(avail)
FREED=$(( END_AVAIL - START_AVAIL ))
bold "=== RESULT ==="
df -h "$TARGET_FS" | tail -1 | awk '{printf "  root: %s used of %s (%s), %s free\n", $3,$2,$5,$4}'
if [ "$FREED" -gt 0 ]; then green "  reclaimed $(human "$FREED") this run"; else echo "  no change"; fi
echo
dim "Still needs sudo:  sudo apt-get clean && sudo journalctl --vacuum-time=7d"
