#!/usr/bin/env bash
set -euo pipefail

DB="/opt/bazarr/data/db/bazarr.db"
LOG="/config/berenstuff/automation/logs/radarr_profile_extract_on_import.log"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

WRITES=0
SKIPS=0
PRUNES=0
EVENT_TYPE=""
MOVIE_ID=""
MOVIE_PATH=""
PROFILE_ID=""

source "$(dirname "$0")/lib_subtitle_common.sh"

notify_discord() {
  local status="$1"
  local details="$2"
  local file_name color emoji

  [ -z "${DISCORD_WEBHOOK_URL:-}" ] && return 0

  file_name="$(basename "${MOVIE_PATH:-unknown}")"

  case "$status" in
    SUCCESS) color=3066993;  emoji="✅" ;;
    SKIP)    color=15844367; emoji="⏭️" ;;
    *)       color=3447003;  emoji="ℹ️" ;;
  esac

  local payload
  payload="$(jq -nc \
    --arg title "$emoji Subtitle Extract — Radarr" \
    --arg desc "$details" \
    --argjson color "$color" \
    --arg event "${EVENT_TYPE:-unknown}" \
    --arg movie_id "${MOVIE_ID:-unknown}" \
    --arg profile_id "${PROFILE_ID:-unknown}" \
    --arg file_name "$file_name" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      fields: [
        {name: "🎥 Movie ID", value: $movie_id, inline: true},
        {name: "🏷️ Profile", value: $profile_id, inline: true},
        {name: "📁 File", value: ("`" + $file_name + "`")}
      ],
      footer: {text: ("Event: " + $event)},
      timestamp: $ts
    }]}')"

  curl -sS -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null 2>&1 || true
}

resolve_profile_id() {
  local movie_id="$1"
  local movie_path="$2"
  local esc_path profile_id default_profile attempt
  esc_path="${movie_path//\'/\'\'}"

  for attempt in $(seq 1 10); do
    profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE radarrId=$movie_id LIMIT 1;")"
    if [ -z "$profile_id" ] && [ -n "$movie_path" ]; then
      profile_id="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE path='$esc_path' LIMIT 1;")"
    fi
    if [ -n "$profile_id" ]; then
      printf '%s' "$profile_id"
      return 0
    fi
    sleep 2
  done

  default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_movies WHERE profileId IS NOT NULL GROUP BY profileId ORDER BY COUNT(*) DESC LIMIT 1;")"
  if [ -z "$default_profile" ]; then
    default_profile="$(sqlite3 "$DB" "SELECT profileId FROM table_languages_profiles ORDER BY profileId LIMIT 1;")"
  fi
  printf '%s' "$default_profile"
}

main() {
  local event_type movie_path movie_id profile_id items

  event_type="$(getenv_fallback RADARR_EVENTTYPE radarr_eventtype)"
  movie_path="$(getenv_fallback RADARR_MOVIEFILE_PATH radarr_moviefile_path)"
  movie_id="$(getenv_fallback RADARR_MOVIE_ID radarr_movie_id)"

  EVENT_TYPE="$event_type"
  MOVIE_PATH="$movie_path"
  MOVIE_ID="$movie_id"

  log "EVENT=$event_type movie_id=$movie_id path=$movie_path"

  if [ -z "$movie_path" ] || [ ! -f "$movie_path" ]; then
    log "Skip: no movie file path"
    notify_discord "SKIP" "Reason: no movie file path"
    exit 0
  fi

  if [ -z "$movie_id" ]; then
    movie_id="$(sqlite3 "$DB" "SELECT radarrId FROM table_movies WHERE path='${movie_path//\'/\'\'}' LIMIT 1;")"
    MOVIE_ID="$movie_id"
  fi

  if [ -z "$movie_id" ]; then
    log "Skip: movie id not found"
    notify_discord "SKIP" "Reason: movie id not found"
    exit 0
  fi

  profile_id="$(resolve_profile_id "$movie_id" "$movie_path")"
  PROFILE_ID="$profile_id"

  if [ -z "$profile_id" ]; then
    log "Skip: profile not found for movie $movie_id"
    notify_discord "SKIP" "Reason: profile not found"
    exit 0
  fi

  if ! sqlite3 "$DB" "SELECT 1 FROM table_movies WHERE radarrId=$movie_id AND profileId=$profile_id LIMIT 1;" | grep -q 1; then
    log "Fallback profile applied for movie $movie_id: profile=$profile_id"
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
    extract_target "$movie_path" "$code" "$forced"
  done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)

  if [ "$WRITES" -gt 0 ]; then
    notify_discord "SUCCESS" "✅ **$WRITES** extracted · ⏭️ **$SKIPS** skipped · 🗑️ **$PRUNES** pruned"
  else
    notify_discord "INFO" "No new extractions · ⏭️ **$SKIPS** skipped · 🗑️ **$PRUNES** pruned"
  fi

  log "Done"
}

main "$@"
