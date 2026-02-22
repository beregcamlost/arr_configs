#!/usr/bin/env bash
set -euo pipefail

DB="/opt/bazarr/data/db/bazarr.db"
LOG="/config/berenstuff/automation/logs/sonarr_profile_extract_on_import.log"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

WRITES=0
SKIPS=0
PRUNES=0
EVENT_TYPE=""
SERIES_ID=""
EPISODE_PATH=""
PROFILE_ID=""

source "$(dirname "$0")/lib_subtitle_common.sh"

notify_discord() {
  local status="$1"
  local details="$2"
  local file_name payload

  [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0

  file_name="$(basename "${EPISODE_PATH:-}")"
  payload="$(jq -nc \
    --arg status "$status" \
    --arg event "${EVENT_TYPE:-unknown}" \
    --arg series_id "${SERIES_ID:-unknown}" \
    --arg profile_id "${PROFILE_ID:-unknown}" \
    --arg file_name "${file_name:-n/a}" \
    --arg details "$details" \
    '{
      content: (
        [
          ("[Bazarr Extract][Sonarr] " + $status),
          ("Event: " + $event),
          ("SeriesID: " + $series_id),
          ("ProfileID: " + $profile_id),
          ("File: " + $file_name),
          $details
        ] | join("\n")
      )
    }')"

  curl -sS -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

resolve_profile_id() {
  local series_id="$1"
  local episode_path="$2"
  local esc_path profile_id default_profile attempt
  esc_path="${episode_path//\'/\'\'}"

  for attempt in $(seq 1 10); do
    profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_shows WHERE sonarrSeriesId=$series_id LIMIT 1;")"
    if [ -z "$profile_id" ] && [ -n "$episode_path" ]; then
      profile_id="$(sqlite3 "$DB" "SELECT s.profileId FROM table_episodes e JOIN table_shows s ON s.sonarrSeriesId=e.sonarrSeriesId WHERE e.path='$esc_path' LIMIT 1;")"
    fi
    if [ -n "$profile_id" ]; then
      printf '%s' "$profile_id"
      return 0
    fi
    sleep 2
  done

  default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_shows WHERE profileId IS NOT NULL GROUP BY profileId ORDER BY COUNT(*) DESC LIMIT 1;")"
  if [ -z "$default_profile" ]; then
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_languages_profiles ORDER BY profileId LIMIT 1;")"
  fi
  printf '%s' "$default_profile"
}

main() {
  local event_type episode_path series_id profile_id items

  event_type="$(getenv_fallback SONARR_EVENTTYPE sonarr_eventtype)"
  episode_path="$(getenv_fallback SONARR_EPISODEFILE_PATH sonarr_episodefile_path)"
  series_id="$(getenv_fallback SONARR_SERIES_ID sonarr_series_id)"

  EVENT_TYPE="$event_type"
  EPISODE_PATH="$episode_path"
  SERIES_ID="$series_id"

  log "EVENT=$event_type series_id=$series_id path=$episode_path"

  if [ -z "$episode_path" ] || [ ! -f "$episode_path" ]; then
    log "Skip: no episode file path"
    notify_discord "SKIP" "Reason: no episode file path"
    exit 0
  fi

  if [ -z "$series_id" ]; then
    series_id="$(sqlite3 "$DB" "SELECT sonarrSeriesId FROM table_episodes WHERE path='${episode_path//\'/\'\'}' LIMIT 1;")"
    SERIES_ID="$series_id"
  fi

  if [ -z "$series_id" ]; then
    log "Skip: series id not found"
    notify_discord "SKIP" "Reason: series id not found"
    exit 0
  fi

  profile_id="$(resolve_profile_id "$series_id" "$episode_path")"
  PROFILE_ID="$profile_id"

  if [ -z "$profile_id" ]; then
    log "Skip: profile not found for series $series_id"
    notify_discord "SKIP" "Reason: profile not found"
    exit 0
  fi

  if ! sqlite3 "$DB" "SELECT 1 FROM table_shows WHERE sonarrSeriesId=$series_id AND profileId=$profile_id LIMIT 1;" | grep -q 1; then
    log "Fallback profile applied for series $series_id: profile=$profile_id"
  fi

  items="$(sqlite3 "$DB" "SELECT items FROM table_languages_profiles WHERE profileId=$profile_id LIMIT 1;")"

  if [ -z "$items" ]; then
    log "Skip: empty profile items"
    notify_discord "SKIP" "Reason: empty profile items"
    exit 0
  fi

  while IFS='|' read -r code forced; do
    [ -z "$code" ] && continue
    code="$(printf '%s' "$code" | tr '[:upper:]' '[:lower:]')"
    forced="$(printf '%s' "$forced" | tr '[:upper:]' '[:lower:]')"
    if [ "$forced" != "true" ] && [ "$forced" != "false" ]; then
      forced="false"
    fi
    log "Applying extraction for language=$code forced=$forced profile=$profile_id"
    extract_target "$episode_path" "$code" "$forced"
  done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)

  if [ "$WRITES" -gt 0 ]; then
    notify_discord "SUCCESS" "Extracted files: $WRITES (existing skipped: $SKIPS, duplicates pruned: $PRUNES)"
  else
    notify_discord "INFO" "No new files extracted (existing skipped: $SKIPS, duplicates pruned: $PRUNES)"
  fi

  log "Done"
}

main "$@"
