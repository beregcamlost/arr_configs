#!/usr/bin/env bash
# SUPERSEDED: This script is replaced by batch_extract_embedded.sh
# Kept as a thin backward-compatibility wrapper.
#
# Previously this script auto-discovered all profiles containing zh/zt languages.
# The new batch extractor does the same when given those profile IDs.
#
# Equivalent to:
#   batch_extract_embedded.sh --profile-ids 3,4 --media-type episodes
#
# To replicate the old auto-discovery behavior across ALL profiles:
#   batch_extract_embedded.sh --all --media-type episodes
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Discover Chinese profile IDs the same way the old script did
DB="${DB:-/opt/bazarr/data/db/bazarr.db}"
mapfile -t PROFILE_IDS < <(
  sqlite3 "$DB" "
    SELECT profileId
    FROM table_languages_profiles
    WHERE (items LIKE '%\"language\":\"zh\"%' OR items LIKE '%\"language\": \"zh\"%'
        OR items LIKE '%\"language\":\"zt\"%' OR items LIKE '%\"language\": \"zt\"%')
    ORDER BY profileId;
  "
)

if [ "${#PROFILE_IDS[@]}" -eq 0 ]; then
  echo "No Chinese profiles found (zh/zt)."
  exit 0
fi

id_csv="$(IFS=,; echo "${PROFILE_IDS[*]}")"

exec "$SCRIPT_DIR/batch_extract_embedded.sh" \
  --profile-ids "$id_csv" \
  --media-type episodes \
  "$@"
