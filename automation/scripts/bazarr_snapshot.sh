#!/usr/bin/env bash
# bazarr_snapshot.sh — Take a read-only snapshot of Bazarr's SQLite DB for safe
# concurrent reads. Uses SQLite's online .backup which never locks the source.
#
# Usage:
#   bazarr_snapshot.sh                    # write to default location
#   bazarr_snapshot.sh /path/to/snap.db   # custom output path
#
# Use the snapshot path everywhere we do read-only Bazarr DB queries (codec_manager
# audit, our analytics, etc). Refresh by re-running this script (cron-friendly).
set -euo pipefail

SRC="${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}"
DST="${1:-/APPBOX_DATA/storage/.bazarr-snapshot/bazarr.db}"
LOG="${BAZARR_SNAPSHOT_LOG:-/config/berenstuff/automation/logs/bazarr_snapshot.log}"

mkdir -p "$(dirname "$DST")"
mkdir -p "$(dirname "$LOG")"

ts() { date +'%Y-%m-%d %H:%M:%S'; }

if [[ ! -f "$SRC" ]]; then
  echo "$(ts) [error] source not found: $SRC" >> "$LOG"
  exit 2
fi

# Use sqlite3 .backup — uses the online backup API, doesn't lock the source.
# Output to a tempfile then atomic-mv so readers never see a half-written file.
TMP="${DST}.tmp.$$"
trap 'rm -f "$TMP" "${TMP}-journal" "${TMP}-wal" "${TMP}-shm"' EXIT

if sqlite3 "$SRC" ".backup '$TMP'" 2>>"$LOG"; then
  mv -f "$TMP" "$DST"
  size=$(stat -c %s "$DST")
  echo "$(ts) [info] snapshot OK size=$size dst=$DST" >> "$LOG"
else
  echo "$(ts) [error] backup failed src=$SRC" >> "$LOG"
  exit 1
fi
