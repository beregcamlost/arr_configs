#!/usr/bin/env bash
set -euo pipefail

# One-time script: strip YIFY/YTS watermarks and <font> tags from all SRT files
# in the media library. Safe to re-run (idempotent — no-change files return 1).

MEDIA_ROOT="${1:-/APPBOX_DATA/storage/media}"

source "$(dirname "$0")/lib_subtitle_common.sh"

LOG="/config/berenstuff/automation/logs/onetime_strip_watermarks.log"
log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG"
}

total=0
stripped=0
deleted=0
errors=0

log "START one-time watermark strip media_root=$MEDIA_ROOT"

while IFS= read -r -d '' srt; do
  total=$((total + 1))
  rc=0
  strip_srt_watermarks "$srt" || rc=$?
  case $rc in
    0) stripped=$((stripped + 1)) ;;
    2) deleted=$((deleted + 1)) ;;
    1) ;; # no changes
    *) errors=$((errors + 1)); log "ERROR rc=$rc file=$srt" ;;
  esac
  if (( total % 500 == 0 )); then
    log "PROGRESS total=$total stripped=$stripped deleted=$deleted errors=$errors"
  fi
done < <(find "$MEDIA_ROOT" -name '*.srt' -type f -print0)

log "DONE total=$total stripped=$stripped deleted=$deleted errors=$errors"
