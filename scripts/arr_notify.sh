#!/bin/sh
#
# arr_notify.sh -- publish "new media you can actually watch" to nas-media.
#
# Runs INSIDE sonarr / radarr / lidarr as their Custom Script connector, bound
# to On Import and On Upgrade only. Not On Grab (a grab is a promise, not a
# file), not On Rename, not On Retag, not On Application Update, not On Health
# (the watchdog owns health -- ADR-0032). ADR-0033.
#
# Why a custom script rather than the native Ntfy connector
# ---------------------------------------------------------
# The native connector sends the *arr's own message text, which is a bare
# release name -- "The.Show.S02E07.1080p.WEB-DL.DDP5.1.H.264-NTb". Useful for
# an audit trail, useless as the thing that tells you there is something new to
# watch. This builds a message a human reads at a glance: the show and episode,
# the quality, the size, and a tap that opens Jellyfin.
#
# Hard rules
# ----------
# * **Exit 0 always.** This runs in the *arr's import pipeline. A non-zero exit
#   is reported as a failed notification and, worse, invites someone to "fix"
#   the import. A notifier must never be able to affect the thing it observes.
#   Every command that can fail is guarded, and the last line is `exit 0`.
# * **POSIX sh.** The LSIO images have no bash guarantee inside s6 scripts, so:
#   no arrays, no [[ ]], no ${var,,}, no local.
# * **The token comes from a file, never the environment.** ADR-0011: a
#   credential in a container's `environment:` block leaks into
#   `docker inspect`. `${CONFIG_DIRECTORY}/ntfy/arr-token` is mode 0600 and
#   bind-mounted read-only at /run/ntfy-arr-token.
#
# How the variable names are verified
# -----------------------------------
# NOT from the Test button. Measured 2026-09-03: the \*arr Custom Script test
# passes exactly ONE variable, `<app>_eventtype=Test`, and nothing else -- so
# it proves the script is invoked and exits 0, and can confirm no payload name
# whatever. (The names are also not literals in the shipped DLLs; Sonarr
# composes them at runtime, so `strings` finds nothing either.)
#
# So this script dumps its whole environment ONCE on the first real event it
# ever sees, guarded by a marker file, and every message it builds degrades to
# "imported" rather than rendering a blank. The next genuine import therefore
# proves the names in `/config/logs/arr_notify.log` without anyone having to
# guess -- and if one is wrong, the message is thin rather than absent.
#
# Environment (supplied by the *arr)
# ----------------------------------
#   sonarr_eventtype / radarr_eventtype / lidarr_eventtype
#   ..._series_title, ..._episodefile_*, ..._movie_*, ..._moviefile_*,
#   ..._artist_name, ..._album_title, ..._addedtrackpaths, ..._isupgrade
#
# Exit codes: 0, always. See above.

set -eu

TOKEN_FILE=/run/ntfy-arr-token
NTFY_URL=${NTFY_URL:-http://ntfy:8410}
TOPIC=${NTFY_TOPIC_MEDIA:-nas-media}
CLICK=${NTFY_MEDIA_CLICK:-}
LOG=/config/logs/arr_notify.log
FIRST_EVENT_MARKER=/config/logs/.arr_notify_first_event

log() {
  # Best-effort: /config/logs may not exist yet on a brand-new container, and a
  # notifier that dies because it cannot log is worse than one that logs
  # nothing. Never create the file as root via `docker exec` -- that is the
  # bazarr subcleaner trap (AGENTS.md).
  { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >>"$LOG"; } 2>/dev/null || true
}

# --- transport ------------------------------------------------------------
# curl is present in all three LSIO images (verified); wget is the fallback for
# a future image that drops it. Both are wrapped so a publish failure is a log
# line, not an exit code.
publish() {
  # $1 title, $2 tags, $3 body
  _token=$(cat "$TOKEN_FILE" 2>/dev/null || true)
  if [ -z "$_token" ]; then
    log "SKIP no token at $TOKEN_FILE"
    return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -m 10 -o /dev/null \
      -H "Authorization: Bearer $_token" \
      -H "X-Title: $1" \
      -H "X-Priority: 3" \
      -H "X-Tags: $2" \
      -H "X-Markdown: true" \
      ${CLICK:+-H "X-Click: $CLICK"} \
      -d "$3" \
      "$NTFY_URL/$TOPIC" 2>>"$LOG" || log "WARN curl publish failed"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null -T 10 \
      --header "Authorization: Bearer $_token" \
      --header "X-Title: $1" \
      --header "X-Priority: 3" \
      --header "X-Tags: $2" \
      --header "X-Markdown: true" \
      ${CLICK:+--header "X-Click: $CLICK"} \
      --post-data "$3" \
      "$NTFY_URL/$TOPIC" 2>>"$LOG" || log "WARN wget publish failed"
  else
    log "SKIP neither curl nor wget present"
  fi
  return 0
}

# --- helpers --------------------------------------------------------------

# Human size of one or more paths. `du -ch` totals them in one pass, which is
# what makes a multi-file season import report one size rather than the first
# file's. Falls back to empty so a message is never blocked on a missing file.
human_size() {
  [ "$#" -gt 0 ] || return 0
  du -ch "$@" 2>/dev/null | tail -n 1 | cut -f1 || true
}

# Count the fields in a separator-delimited *arr list variable. Sonarr uses
# commas for episode numbers; Lidarr uses `|` for added track paths.
count_fields() {
  # $1 string, $2 separator
  [ -n "$1" ] || { echo 0; return 0; }
  printf '%s' "$1" | tr "$2" '\n' | grep -c . || echo 0
}

# " · "-joined, skipping empties, so a missing release group does not leave a
# dangling separator.
join_meta() {
  _out=""
  for _part in "$@"; do
    [ -n "$_part" ] || continue
    if [ -z "$_out" ]; then _out="$_part"; else _out="$_out · $_part"; fi
  done
  printf '%s' "$_out"
}

# "Upgrade" prefix plus old -> new quality when the app told us the old one.
# Sonarr/Radarr expose the replaced file's quality only on some versions, so
# this degrades to a bare "upgrade" rather than inventing an arrow.
upgrade_note() {
  # $1 isupgrade, $2 old quality, $3 new quality
  case "$1" in
    [Tt]rue|1|yes) ;;
    *) printf ''; return 0 ;;
  esac
  if [ -n "$2" ] && [ "$2" != "$3" ]; then
    printf 'upgrade %s → %s' "$2" "$3"
  else
    printf 'upgrade'
  fi
}

# One-time full environment dump on the first REAL event. This is the only
# thing that can confirm the variable names this file reads: the Test button
# carries none of them. Marker-gated so it happens once and never grows the log
# again.
dump_env_once() {
  [ -e "$FIRST_EVENT_MARKER" ] && return 0
  log "FIRST real event ($1); dumping the environment once to confirm variable names"
  env | sort | sed 's/^/    /' >>"$LOG" 2>/dev/null || true
  : >"$FIRST_EVENT_MARKER" 2>/dev/null || true
  return 0
}

# --- per-app message builders --------------------------------------------

notify_sonarr() {
  _series=${sonarr_series_title:-unknown series}
  _season=${sonarr_episodefile_seasonnumber:-}
  _epnums=${sonarr_episodefile_episodenumbers:-}
  _eptitles=${sonarr_episodefile_episodetitles:-}
  _quality=${sonarr_episodefile_quality:-}
  _group=${sonarr_episodefile_releasegroup:-}
  _path=${sonarr_episodefile_path:-}
  _paths=${sonarr_episodefile_paths:-$_path}
  _isupgrade=${sonarr_isupgrade:-False}
  _oldquality=${sonarr_deletedfilequalities:-}

  # One message per import EVENT, not per file. OnImportComplete fires once for
  # a whole season pack and lists every file, so the episode range and the
  # total size both come from the list -- 10 separate pushes for one import is
  # exactly the noise this exercise exists to remove.
  _count=$(count_fields "$_epnums" ",")
  [ "$_count" -gt 0 ] || _count=1

  # SxxEyy, or SxxEyy-Ezz for a multi-episode import.
  if [ -n "$_season" ] && [ -n "$_epnums" ]; then
    _first=$(printf '%s' "$_epnums" | cut -d, -f1 | tr -d ' ')
    _last=$(printf '%s' "$_epnums" | tr ',' '\n' | tail -n 1 | tr -d ' ')
    if [ "$_first" = "$_last" ]; then
      _ep=$(printf 'S%02dE%02d' "$_season" "$_first" 2>/dev/null || printf 'S%sE%s' "$_season" "$_first")
    else
      _ep=$(printf 'S%02dE%02d-E%02d' "$_season" "$_first" "$_last" 2>/dev/null \
            || printf 'S%sE%s-E%s' "$_season" "$_first" "$_last")
    fi
  else
    _ep=${sonarr_episodefile_relativepath:-}
  fi

  # shellcheck disable=SC2086  # deliberate word splitting: a `|`-free path list
  _size=$(human_size $_paths)
  _note=$(upgrade_note "$_isupgrade" "$_oldquality" "$_quality")
  _files=""
  [ "$_count" -gt 1 ] && _files="$_count episodes"
  _body=$(join_meta "$_eptitles" "$_files" "$_quality" "$_size" "$_group" "$_note")
  publish "📺 $_series $_ep" "tv" "${_body:-imported}"
}

notify_radarr() {
  _title=${radarr_movie_title:-unknown movie}
  _year=${radarr_movie_year:-}
  _quality=${radarr_moviefile_quality:-}
  _group=${radarr_moviefile_releasegroup:-}
  _path=${radarr_moviefile_path:-}
  _isupgrade=${radarr_isupgrade:-False}
  _oldquality=${radarr_deletedfilequalities:-}

  _size=$(human_size "$_path")
  _note=$(upgrade_note "$_isupgrade" "$_oldquality" "$_quality")
  _body=$(join_meta "$_quality" "$_size" "$_group" "$_note")
  _head="🎬 $_title"
  [ -n "$_year" ] && _head="🎬 $_title ($_year)"
  publish "$_head" "film_projector" "${_body:-imported}"
}

notify_lidarr() {
  _artist=${lidarr_artist_name:-unknown artist}
  _album=${lidarr_album_title:-unknown album}
  _quality=${lidarr_release_quality:-${lidarr_trackfile_quality:-}}
  _added=${lidarr_addedtrackpaths:-}
  _isupgrade=${lidarr_isupgrade:-False}

  _tracks=$(count_fields "$_added" "|")
  _size=""
  if [ -n "$_added" ]; then
    # Lidarr separates paths with `|`, which no shell splits on by default --
    # so newline-split into an argument list rather than relying on IFS.
    _size=$(printf '%s' "$_added" | tr '|' '\n' | tr '\n' '\0' \
            | xargs -0 du -ch 2>/dev/null | tail -n 1 | cut -f1 || true)
  fi
  _note=$(upgrade_note "$_isupgrade" "" "$_quality")
  _tracklabel=""
  [ "$_tracks" -gt 0 ] && _tracklabel="$_tracks tracks"
  _body=$(join_meta "$_tracklabel" "$_quality" "$_size" "$_note")
  publish "🎵 $_artist — $_album" "musical_note" "${_body:-imported}"
}

# --- dispatch -------------------------------------------------------------

EVENT=${sonarr_eventtype:-${radarr_eventtype:-${lidarr_eventtype:-}}}

case "$EVENT" in
  Test)
    # The *arr Test button. Exit 0 silently so the UI reports success, but dump
    # the environment once -- this is the only way to see the variable names the
    # app ACTUALLY passes, and guessing them produces a message full of blanks
    # that still returns HTTP 200.
    log "TEST event; environment follows"
    env | sort | sed 's/^/    /' >>"$LOG" 2>/dev/null || true
    exit 0
    ;;
  Download|ImportComplete)
    # Sonarr's import is `Download` (legacy) or `ImportComplete` (current);
    # Radarr's is `Download`. Upgrades arrive as the same event with
    # ..._isupgrade=True, which is why there is no separate Upgrade branch.
    dump_env_once "$EVENT"
    if [ -n "${sonarr_eventtype:-}" ]; then notify_sonarr
    elif [ -n "${radarr_eventtype:-}" ]; then notify_radarr
    fi
    ;;
  AlbumDownload|ReleaseImport|TrackFileImport)
    dump_env_once "$EVENT"
    notify_lidarr
    ;;
  "")
    log "SKIP no *_eventtype in the environment -- not invoked by an *arr"
    ;;
  *)
    # Any other event type is a no-op, deliberately. A connector toggled on in
    # the UI must not start producing messages this script has no shape for.
    log "SKIP unhandled event type: $EVENT"
    ;;
esac

exit 0
