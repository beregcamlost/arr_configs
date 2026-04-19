#!/usr/bin/env bash
set -euo pipefail

# batch_extract_embedded.sh — Unified batch subtitle extractor for all Bazarr language profiles.
#
# Replaces:
#   extract_fr_embedded_for_profile5.sh
#   extract_zh_embedded_for_chinese_profiles.sh
#
# Sources lib_subtitle_common.sh for quality-scoring extraction via extract_target().

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
: "${BAZARR_DB:=/opt/bazarr/data/db/bazarr.db}"
DB="$BAZARR_DB"
LOG="${LOG:-/config/berenstuff/automation/logs/batch_extract_embedded.log}"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

DRY_RUN=0
MEDIA_TYPE="both"
PROFILE_MODE=""            # "ids" or "all"
PROFILE_IDS_CSV=""

LOCK_FILE="/tmp/batch_extract_embedded.lock"

# Globals expected by lib_subtitle_common.sh
WRITES=0
SKIPS=0
PRUNES=0
ERRORS=0
FILES_SCANNED=0

# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------

usage() {
  cat <<'USAGE'
Usage: batch_extract_embedded.sh [OPTIONS]

Extract embedded subtitles for media files associated with Bazarr language profiles.

Options:
  --profile-ids IDS   Comma-separated profile IDs to process (e.g. 5,3,4)
  --all               Process ALL Bazarr language profiles
  --media-type TYPE   What to scan: episodes, movies, or both (default: both)
  --dry-run           Preview what would be extracted without writing files
  --bazarr-db PATH    Override Bazarr database path
  --log PATH          Override log file path
  --help              Show this help message

Either --profile-ids or --all is required.

Examples:
  batch_extract_embedded.sh --profile-ids 5 --media-type episodes
  batch_extract_embedded.sh --all --dry-run
  batch_extract_embedded.sh --profile-ids 3,4,5 --media-type both
USAGE
  exit 0
}

# ---------------------------------------------------------------------------
# Arg parsing
# ---------------------------------------------------------------------------

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --profile-ids)
        [[ -z "${2:-}" ]] && { echo "ERROR: --profile-ids requires a value" >&2; exit 1; }
        PROFILE_MODE="ids"
        PROFILE_IDS_CSV="$2"
        shift 2
        ;;
      --all)
        PROFILE_MODE="all"
        shift
        ;;
      --media-type)
        [[ -z "${2:-}" ]] && { echo "ERROR: --media-type requires a value" >&2; exit 1; }
        case "$2" in
          episodes|movies|both) MEDIA_TYPE="$2" ;;
          *) echo "ERROR: --media-type must be episodes, movies, or both" >&2; exit 1 ;;
        esac
        shift 2
        ;;
      --dry-run)
        DRY_RUN=1
        shift
        ;;
      --bazarr-db)
        [[ -z "${2:-}" ]] && { echo "ERROR: --bazarr-db requires a path" >&2; exit 1; }
        BAZARR_DB="$2"
        DB="$2"
        shift 2
        ;;
      --log)
        [[ -z "${2:-}" ]] && { echo "ERROR: --log requires a path" >&2; exit 1; }
        LOG="$2"
        shift 2
        ;;
      --help|-h)
        usage
        ;;
      *)
        echo "ERROR: Unknown option: $1" >&2
        echo "Run with --help for usage." >&2
        exit 1
        ;;
    esac
  done

  if [[ -z "$PROFILE_MODE" ]]; then
    echo "ERROR: Either --profile-ids or --all is required." >&2
    echo "Run with --help for usage." >&2
    exit 1
  fi
}

# ---------------------------------------------------------------------------
# Source shared library (after LOG and DB are set)
# ---------------------------------------------------------------------------

source_lib() {
  local lib="$SCRIPT_DIR/lib_subtitle_common.sh"
  if [[ ! -f "$lib" ]]; then
    echo "FATAL: lib_subtitle_common.sh not found at $lib" >&2
    exit 1
  fi
  # shellcheck source=lib_subtitle_common.sh
  source "$lib"
}

# ---------------------------------------------------------------------------
# Flock single-instance
# ---------------------------------------------------------------------------

acquire_lock() {
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    echo "Another instance of batch_extract_embedded.sh is already running." >&2
    exit 0
  fi
}

# ---------------------------------------------------------------------------
# Resolve profile IDs
# ---------------------------------------------------------------------------

resolve_profile_ids() {
  local -a ids=()

  if [[ "$PROFILE_MODE" == "all" ]]; then
    mapfile -t ids < <(
      sqlite3 -cmd ".timeout $SQLITE_TIMEOUT_MS" "$DB" "SELECT profileId FROM table_languages_profiles ORDER BY profileId;" 2>>"$LOG"
    )
  else
    IFS=',' read -ra ids <<< "$PROFILE_IDS_CSV"
  fi

  if [[ ${#ids[@]} -eq 0 ]]; then
    log "No profiles found to process."
    exit 0
  fi

  printf '%s\n' "${ids[@]}"
}

# ---------------------------------------------------------------------------
# Parse language items from a profile's JSON items column
# ---------------------------------------------------------------------------

parse_profile_languages() {
  local profile_id="$1"
  local items_json

  items_json="$(sqlite3 -cmd ".timeout $SQLITE_TIMEOUT_MS" "$DB" "
    SELECT items FROM table_languages_profiles WHERE profileId = $profile_id;
  " 2>>"$LOG" || true)"

  if [[ -z "$items_json" ]]; then
    log "WARN: Profile $profile_id not found or has no items."
    return 0
  fi

  # Extract language code and forced flag from each item.
  # Output: language_code forced_bool (one per line)
  printf '%s' "$items_json" | jq -r '.[] | "\(.language) \(.forced)"'
}

# ---------------------------------------------------------------------------
# Query media paths for a profile
# ---------------------------------------------------------------------------

query_episodes() {
  local profile_id="$1"
  sqlite3 -cmd ".timeout $SQLITE_TIMEOUT_MS" "$DB" "
    SELECT e.path
    FROM table_episodes e
    JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
    WHERE s.profileId = $profile_id
      AND s.monitored = 'True'
      AND e.monitored = 'True'
    ORDER BY e.path;
  " 2>>"$LOG"
}

query_movies() {
  local profile_id="$1"
  sqlite3 -cmd ".timeout $SQLITE_TIMEOUT_MS" "$DB" "
    SELECT path
    FROM table_movies
    WHERE profileId = $profile_id
      AND monitored = 'True'
    ORDER BY path;
  " 2>>"$LOG"
}

# ---------------------------------------------------------------------------
# Process a single media file for all languages in a profile
# ---------------------------------------------------------------------------

process_file() {
  local file="$1"
  shift
  # Remaining args: pairs of "lang forced" strings
  local -a lang_pairs=("$@")

  if [[ ! -f "$file" ]]; then
    log "MISS file not found: $file"
    ERRORS=$((ERRORS + 1))
    return 0
  fi

  FILES_SCANNED=$((FILES_SCANNED + 1))

  if [[ $DRY_RUN -eq 1 ]]; then
    local lp
    for lp in "${lang_pairs[@]}"; do
      local lang forced
      read -r lang forced <<< "$lp"
      local forced_label=""
      if [[ "$forced" == "True" ]]; then
        forced_label=" (forced)"
      fi
      log "DRY-RUN would extract: lang=$lang${forced_label} from $file"
    done
    return 0
  fi

  local lp
  for lp in "${lang_pairs[@]}"; do
    local lang forced
    read -r lang forced <<< "$lp"
    local forced_bool="false"
    if [[ "$forced" == "True" ]]; then
      forced_bool="true"
    fi

    # extract_target handles stream detection, quality scoring, dedup, and writing
    if ! extract_target "$file" "$lang" "$forced_bool"; then
      ERRORS=$((ERRORS + 1))
    fi
  done
}

# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------

notify_discord() {
  local status="$1"
  local color

  [[ -z "${DISCORD_WEBHOOK_URL:-}" ]] && return 0

  if [[ $ERRORS -gt 0 ]]; then
    color=15105570  # orange
  else
    color=3066993   # green
  fi

  local _desc=""
  if [[ $DRY_RUN -eq 1 ]]; then
    _desc="_(dry run — no changes made)_"
  fi

  local _fields
  _fields="$(jq -nc \
    --arg profiles "${#profile_ids[@]}" \
    --arg files "$FILES_SCANNED" \
    --arg writes "$WRITES" \
    --arg skips "$SKIPS" \
    --arg prunes "$PRUNES" \
    --arg errors "$ERRORS" \
    '[
      {name:"🏷️ Profiles",  value:$profiles, inline:true},
      {name:"📂 Files",     value:$files,    inline:true},
      {name:"✅ Writes",    value:$writes,   inline:true},
      {name:"⏭️ Skips",     value:$skips,    inline:true},
      {name:"🗑️ Prunes",    value:$prunes,   inline:true},
      {name:"❌ Errors",    value:$errors,   inline:true}
    ]')"

  local payload
  payload="$(jq -nc \
    --arg title "📦 Batch Extract — $status" \
    --arg desc "$_desc" \
    --argjson color "$color" \
    --argjson fields "$_fields" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      fields: $fields,
      footer: {text: "Batch Subtitle Extractor"},
      timestamp: $ts
    }]}')"

  curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors \
    -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
  parse_args "$@"
  acquire_lock
  source_lib

  log "=== batch_extract_embedded.sh starting ==="
  log "Mode: profile_mode=$PROFILE_MODE media_type=$MEDIA_TYPE dry_run=$DRY_RUN"
  [[ -n "$PROFILE_IDS_CSV" ]] && log "Requested profile IDs: $PROFILE_IDS_CSV"

  local -a profile_ids
  mapfile -t profile_ids < <(resolve_profile_ids)

  log "Profiles to process: ${profile_ids[*]}"

  local pid
  for pid in "${profile_ids[@]}"; do
    # Trim whitespace from profile ID
    pid="$(echo "$pid" | tr -d '[:space:]')"
    [[ -z "$pid" ]] && continue

    local profile_name
    profile_name="$(sqlite3 -cmd ".timeout $SQLITE_TIMEOUT_MS" "$DB" "SELECT name FROM table_languages_profiles WHERE profileId = $pid;" 2>/dev/null || echo "unknown")"
    log "--- Processing profile $pid ($profile_name) ---"

    # Parse language items for this profile
    local -a lang_pairs=()
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      lang_pairs+=("$line")
    done < <(parse_profile_languages "$pid")

    if [[ ${#lang_pairs[@]} -eq 0 ]]; then
      log "WARN: No languages found for profile $pid, skipping."
      continue
    fi

    local lang_summary=""
    local lp
    for lp in "${lang_pairs[@]}"; do
      local l f
      read -r l f <<< "$lp"
      if [[ "$f" == "True" ]]; then
        lang_summary+="${l}(forced) "
      else
        lang_summary+="${l} "
      fi
    done
    log "Languages: $lang_summary"

    # Collect media paths (deduplicated across episodes/movies)
    local -A seen_paths=()
    local -a media_paths=()

    if [[ "$MEDIA_TYPE" == "episodes" || "$MEDIA_TYPE" == "both" ]]; then
      while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        if [[ -z "${seen_paths[$p]:-}" ]]; then
          seen_paths["$p"]=1
          media_paths+=("$p")
        fi
      done < <(query_episodes "$pid")
    fi

    if [[ "$MEDIA_TYPE" == "movies" || "$MEDIA_TYPE" == "both" ]]; then
      while IFS= read -r p; do
        [[ -z "$p" ]] && continue
        if [[ -z "${seen_paths[$p]:-}" ]]; then
          seen_paths["$p"]=1
          media_paths+=("$p")
        fi
      done < <(query_movies "$pid")
    fi

    log "Media files for profile $pid: ${#media_paths[@]}"

    if [[ ${#media_paths[@]} -eq 0 ]]; then
      continue
    fi

    local mpath
    for mpath in "${media_paths[@]}"; do
      process_file "$mpath" "${lang_pairs[@]}"
    done
  done

  # Summary
  local summary
  summary="🏷️ **Profiles:** ${#profile_ids[@]} · 📂 **Files:** $FILES_SCANNED"$'\n'"✅ **Writes:** $WRITES · ⏭️ **Skips:** $SKIPS · 🗑️ **Prunes:** $PRUNES · ❌ **Errors:** $ERRORS"
  if [[ $DRY_RUN -eq 1 ]]; then
    summary="[DRY RUN] $summary"
  fi
  log "=== DONE: $summary ==="

  # Discord notification
  if [[ $DRY_RUN -eq 0 ]] && [[ $WRITES -gt 0 || $ERRORS -gt 0 ]]; then
    notify_discord "Completed" "$summary"
  fi
}

main "$@"
