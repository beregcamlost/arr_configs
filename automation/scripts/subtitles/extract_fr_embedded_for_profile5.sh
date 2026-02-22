#!/usr/bin/env bash
# SUPERSEDED: This script is replaced by batch_extract_embedded.sh
# Kept as a thin backward-compatibility wrapper.
#
# Equivalent to:
#   batch_extract_embedded.sh --profile-ids 5 --media-type episodes
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROFILE_ID="${PROFILE_ID:-5}"

exec "$SCRIPT_DIR/batch_extract_embedded.sh" \
  --profile-ids "$PROFILE_ID" \
  --media-type episodes \
  "$@"
