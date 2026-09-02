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
# It backs up through a STABLE symlink path, not the timestamped archive.
# restic's `forget` groups by --group-by host,paths and applies the keep policy
# per group; a new path every night makes every snapshot its own group of one,
# so --keep-daily 7 keeps everything forever and nothing is ever pruned. The
# stable path also lets restic pick a parent snapshot instead of rescanning.
#
# This backs up CONFIG, not media. The 4.6T under ${SHARE_DIRECTORY} is not
# backed up anywhere, by choice.
#
# Dedup caveat, stated rather than discovered: config_backup.py emits a fresh
# gzip stream each night, so restic sees ~320MB of entirely new data per run and
# deduplication buys almost nothing. Roughly 18 retained snapshots means ~6GB at
# the destination. That is the accepted cost of backing up the quiesced archive
# instead of live WAL-mode SQLite.
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

RESTIC="${RESTIC_BIN:-restic}"
LOCAL_BACKUPS="${LOCAL_BACKUP_DIR:-/mnt/drive/backups/nas-configs}"
STABLE="$LOCAL_BACKUPS/latest"
DRY_RUN=1
[ "${1:-}" = "--apply" ] && DRY_RUN=0

if ! command -v "$RESTIC" >/dev/null 2>&1; then
  echo "FATAL: restic not found (tried '$RESTIC'). Install it with" >&2
  echo "       sudo apt-get install -y restic, or set RESTIC_BIN." >&2
  exit 2
fi

for v in RESTIC_REPOSITORY RESTIC_PASSWORD_FILE; do
  if [ -z "${!v:-}" ]; then echo "FATAL: $v is not set in .env" >&2; exit 2; fi
done
if [ ! -r "$RESTIC_PASSWORD_FILE" ]; then
  echo "FATAL: RESTIC_PASSWORD_FILE ($RESTIC_PASSWORD_FILE) is not readable" >&2; exit 2
fi

# config_backup.py writes one `configs-<timestamp>.tar.gz` per night here, not
# a timestamped directory -- match whichever shape is present rather than
# assuming, because getting this wrong makes the job exit 2 every night.
newest=$(find "$LOCAL_BACKUPS" -mindepth 1 -maxdepth 1 ! -name latest \
           -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
if [ -z "$newest" ]; then
  echo "FATAL: no local config backup under $LOCAL_BACKUPS -- run config_backup.py first" >&2
  exit 2
fi
# The stable path is a HARD link, not a symlink. restic does not follow
# symlinks -- it archives the link itself, which silently produces a snapshot
# reading "processed 0 files, 0 B" and an exit code of 0. A hard link at a
# fixed path IS the file, so restic sees real content at a stable path and
# retention, parent selection and grouping all work.
# A directory cannot be hard-linked; on a layout that produces directories,
# back up the resolved path directly and let --group-by host,tags carry
# retention (which is why forget groups by tags rather than paths).
rm -f "$STABLE"
if [ -d "$newest" ]; then
  ln -sfn "$newest" "$STABLE"
  TARGET="$newest"
elif ln -f "$newest" "$STABLE" 2>/dev/null; then
  TARGET="$STABLE"
else
  echo "WARNING: could not hard-link $newest to $STABLE (different filesystem?);" >&2
  echo "         backing up the timestamped path instead. Retention still works" >&2
  echo "         because forget groups by host,tags rather than host,paths." >&2
  TARGET="$newest"
fi

age_h=$(( ( $(date +%s) - $(stat -Lc %Y "$TARGET") ) / 3600 ))
echo "newest local backup: $newest"
echo "  backing up:        $TARGET (${age_h}h old, $(du -sLh "$TARGET" | cut -f1))"
if [ "$age_h" -gt 48 ]; then
  echo "WARNING: local backup is ${age_h}h old; the nightly config_backup cron may be broken" >&2
fi

if [ "$DRY_RUN" -eq 1 ]; then
  echo "DRY-RUN: would run: restic backup --tag nas-config $TARGET"
  "$RESTIC" snapshots --compact 2>/dev/null | tail -5
  echo "pass --apply to actually push"
  exit 0
fi

rc=0
echo "==> backing up"
"$RESTIC" backup --tag nas-config --host "$(hostname)" "$TARGET" || exit 2

# --group-by host,tags: the path is stable now, but tagging is the durable
# guarantee that one changed path cannot silently disable retention again.
echo "==> pruning (keep 7 daily, 5 weekly, 6 monthly)"
"$RESTIC" forget --tag nas-config --group-by host,tags \
  --keep-daily 7 --keep-weekly 5 --keep-monthly 6 --prune || rc=1

# check alone verifies STRUCTURE only. --read-data-subset actually reads the
# stored blobs back; 1/7 per night covers the whole repository each week at a
# seventh of the egress.
week=$(( $(date +%-j) % 7 + 1 ))
echo "==> verifying repository integrity (structure + data subset ${week}/7)"
"$RESTIC" check --read-data-subset=${week}/7 || rc=1

echo "==> latest snapshots"
"$RESTIC" snapshots --compact --tag nas-config | tail -5
exit $rc
