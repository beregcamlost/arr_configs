#!/usr/bin/env bash
set -euo pipefail

DB="/opt/bazarr/data/db/bazarr.db"
LOG="/config/berenstuff/automation/logs/arr_profile_extract_on_import.log"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

WRITES=0
SKIPS=0
PRUNES=0

source "$(dirname "$0")/lib_subtitle_common.sh"

# ---------------------------------------------------------------------------
# Auto-detect Sonarr vs Radarr from env vars
# ---------------------------------------------------------------------------
ARR_TYPE=""
EVENT_TYPE=""
MEDIA_PATH=""
MEDIA_ID=""
PROFILE_ID=""

sonarr_event="$(getenv_fallback SONARR_EVENTTYPE sonarr_eventtype)"
radarr_event="$(getenv_fallback RADARR_EVENTTYPE radarr_eventtype)"

if [[ -n "$sonarr_event" ]]; then
  ARR_TYPE="sonarr"
  EVENT_TYPE="$sonarr_event"
  MEDIA_PATH="$(getenv_fallback SONARR_EPISODEFILE_PATH sonarr_episodefile_path)"
  MEDIA_ID="$(getenv_fallback SONARR_SERIES_ID sonarr_series_id)"
elif [[ -n "$radarr_event" ]]; then
  ARR_TYPE="radarr"
  EVENT_TYPE="$radarr_event"
  MEDIA_PATH="$(getenv_fallback RADARR_MOVIEFILE_PATH radarr_moviefile_path)"
  MEDIA_ID="$(getenv_fallback RADARR_MOVIE_ID radarr_movie_id)"
else
  echo "ERROR: No Sonarr or Radarr event type detected." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Discord notification (adapts to arr type)
# ---------------------------------------------------------------------------
notify_discord() {
  local status="$1" details="$2"
  local file_name color emoji label id_label

  [[ -n "${DISCORD_WEBHOOK_URL:-}" ]] || return 0

  file_name="$(basename "${MEDIA_PATH:-unknown}")"

  case "$ARR_TYPE" in
    sonarr) label="Sonarr"; id_label="Series ID" ;;
    radarr) label="Radarr"; id_label="Movie ID" ;;
  esac

  case "$status" in
    SUCCESS) color=3066993;  emoji="✅" ;;
    SKIP)    color=15844367; emoji="⏭️" ;;
    *)       color=3447003;  emoji="ℹ️" ;;
  esac

  local payload
  payload="$(jq -nc \
    --arg title "$emoji Subtitle Extract — $label" \
    --arg desc "$details" \
    --argjson color "$color" \
    --arg event "${EVENT_TYPE:-unknown}" \
    --arg media_id "${MEDIA_ID:-unknown}" \
    --arg profile_id "${PROFILE_ID:-unknown}" \
    --arg file_name "$file_name" \
    --arg id_label "$id_label" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      fields: [
        {name: $id_label, value: $media_id, inline: true},
        {name: "Profile", value: $profile_id, inline: true},
        {name: "File", value: ("`" + $file_name + "`")}
      ],
      footer: {text: ("Event: " + $event)},
      timestamp: $ts
    }]}')"

  curl -sS -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Resolve profile ID (works for both Sonarr series and Radarr movies)
# ---------------------------------------------------------------------------
resolve_profile_id() {
  local media_id="$1" media_path="$2"
  local esc_path profile_id default_profile attempt
  esc_path="$(sql_escape "$media_path")"

  for attempt in $(seq 1 10); do
    if [[ "$ARR_TYPE" == "sonarr" ]]; then
      profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_shows WHERE sonarrSeriesId=$media_id LIMIT 1;")"
      if [[ -z "$profile_id" && -n "$media_path" ]]; then
        profile_id="$(sqlite3 "$DB" "SELECT s.profileId FROM table_episodes e JOIN table_shows s ON s.sonarrSeriesId=e.sonarrSeriesId WHERE e.path='$esc_path' LIMIT 1;")"
      fi
    else
      profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE radarrId=$media_id LIMIT 1;")"
      if [[ -z "$profile_id" && -n "$media_path" ]]; then
        profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE path='$esc_path' LIMIT 1;")"
      fi
    fi
    if [[ -n "$profile_id" ]]; then
      printf '%s' "$profile_id"
      return 0
    fi
    sleep 2
  done

  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_shows WHERE profileId IS NOT NULL GROUP BY profileId ORDER BY COUNT(*) DESC LIMIT 1;")"
  else
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE profileId IS NOT NULL GROUP BY profileId ORDER BY COUNT(*) DESC LIMIT 1;")"
  fi
  if [[ -z "$default_profile" ]]; then
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_languages_profiles ORDER BY profileId LIMIT 1;")"
  fi
  printf '%s' "$default_profile"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
  log "EVENT=$EVENT_TYPE arr=$ARR_TYPE media_id=$MEDIA_ID path=$MEDIA_PATH"

  if [[ -z "$MEDIA_PATH" || ! -f "$MEDIA_PATH" ]]; then
    log "Skip: no media file path"
    notify_discord "SKIP" "Reason: no media file path"
    exit 0
  fi

  # Resolve media ID from DB if not provided
  if [[ -z "$MEDIA_ID" ]]; then
    local esc_path
    esc_path="$(sql_escape "$MEDIA_PATH")"
    if [[ "$ARR_TYPE" == "sonarr" ]]; then
      MEDIA_ID="$(sqlite3 "$DB" "SELECT sonarrSeriesId FROM table_episodes WHERE path='$esc_path' LIMIT 1;")"
    else
      MEDIA_ID="$(sqlite3 "$DB" "SELECT radarrId FROM table_movies WHERE path='$esc_path' LIMIT 1;")"
    fi
  fi

  if [[ -z "$MEDIA_ID" ]]; then
    log "Skip: media id not found"
    notify_discord "SKIP" "Reason: media id not found"
    exit 0
  fi

  PROFILE_ID="$(resolve_profile_id "$MEDIA_ID" "$MEDIA_PATH")"

  if [[ -z "$PROFILE_ID" ]]; then
    log "Skip: profile not found for $ARR_TYPE id=$MEDIA_ID"
    notify_discord "SKIP" "Reason: profile not found"
    exit 0
  fi

  # Check profile matches media
  local profile_check
  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    profile_check="$(sqlite3 "$DB" "SELECT 1 FROM table_shows WHERE sonarrSeriesId=$MEDIA_ID AND profileId=$PROFILE_ID LIMIT 1;")"
  else
    profile_check="$(sqlite3 "$DB" "SELECT 1 FROM table_movies WHERE radarrId=$MEDIA_ID AND profileId=$PROFILE_ID LIMIT 1;")"
  fi
  if [[ "$profile_check" != "1" ]]; then
    log "Fallback profile applied for $ARR_TYPE id=$MEDIA_ID: profile=$PROFILE_ID"
  fi

  local items
  items="$(sqlite3 "$DB" "SELECT items FROM table_languages_profiles WHERE profileId=$PROFILE_ID LIMIT 1;")"

  if [[ -z "$items" ]]; then
    log "Skip: empty profile items"
    notify_discord "SKIP" "Reason: empty profile items"
    exit 0
  fi

  while IFS='|' read -r code forced; do
    [[ -z "$code" ]] && continue
    code="${code,,}"
    forced="${forced,,}"
    if [[ "$forced" != "true" && "$forced" != "false" ]]; then
      forced="false"
    fi
    log "Applying extraction for language=$code forced=$forced profile=$PROFILE_ID"
    extract_target "$MEDIA_PATH" "$code" "$forced"
  done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)

  # Trigger Emby refresh for the imported file
  if [[ "$WRITES" -gt 0 ]] && [[ -n "${EMBY_URL:-}" && -n "${EMBY_API_KEY:-}" ]]; then
    emby_refresh_item "$MEDIA_PATH" || log "WARN: Emby refresh failed (non-fatal)"
  fi

  if [[ "$WRITES" -gt 0 ]]; then
    notify_discord "SUCCESS" "**$WRITES** extracted · **$SKIPS** skipped · **$PRUNES** pruned"
  else
    notify_discord "INFO" "No new extractions · **$SKIPS** skipped · **$PRUNES** pruned"
  fi

  log "Done"
}

main "$@"
