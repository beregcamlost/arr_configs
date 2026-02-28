#!/usr/bin/env bash
set -euo pipefail

DB="/opt/bazarr/data/db/bazarr.db"
LOG="/config/berenstuff/automation/logs/arr_profile_extract_on_import.log"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

WRITES=0
SKIPS=0
PRUNES=0

source "$(dirname "$0")/lib_subtitle_common.sh"

# Source .env for API keys (BAZARR_API_KEY, EMBY_URL, EMBY_API_KEY, etc.)
# shellcheck disable=SC1091
[[ -f /config/berenstuff/.env ]] && source /config/berenstuff/.env

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

  # Strip ALL embedded subtitle tracks — after extraction, external SRTs are
  # the source of truth.  Removing embedded tracks eliminates Bazarr seeing
  # duplicates (e.g. "two en files") and keeps containers clean.
  strip_all_embedded_subs "$MEDIA_PATH" || log "WARN: strip_all_embedded_subs failed (non-fatal)"

  # Trigger Emby refresh for the imported file
  if [[ "$WRITES" -gt 0 ]] && [[ -n "${EMBY_URL:-}" && -n "${EMBY_API_KEY:-}" ]]; then
    emby_refresh_item "$MEDIA_PATH" || log "WARN: Emby refresh failed (non-fatal)"
  fi

  # Trigger Bazarr rescan so it picks up the new file immediately
  local bazarr_url="${BAZARR_URL:-http://127.0.0.1:6767/bazarr}"
  local bazarr_key="${BAZARR_API_KEY:-}"
  if [[ -n "$bazarr_key" && -n "$MEDIA_ID" ]]; then
    if [[ "$ARR_TYPE" == "sonarr" ]]; then
      bazarr_scan_disk_series "$MEDIA_ID" "$bazarr_url" "$bazarr_key" || log "WARN: Bazarr series rescan failed (non-fatal)"
    else
      bazarr_scan_disk_movie "$MEDIA_ID" "$bazarr_url" "$bazarr_key" || log "WARN: Bazarr movie rescan failed (non-fatal)"
    fi

    # Search for missing subtitles — for each profile language without an
    # external SRT on disk, trigger a per-episode/movie Bazarr search so
    # missing subs get downloaded immediately instead of waiting 6 hours.
    local stem dir
    stem="$(basename "${MEDIA_PATH%.*}")"
    dir="$(dirname "$MEDIA_PATH")"
    local bazarr_ref_id=""
    if [[ "$ARR_TYPE" == "sonarr" ]]; then
      local esc_path
      esc_path="$(sql_escape "$MEDIA_PATH")"
      bazarr_ref_id="$(sqlite3 "$DB" "SELECT sonarrEpisodeId FROM table_episodes WHERE path='$esc_path' LIMIT 1;" 2>/dev/null)" || true
    else
      bazarr_ref_id="$MEDIA_ID"
    fi
    if [[ -n "$bazarr_ref_id" ]]; then
      while IFS='|' read -r lang lang_forced; do
        [[ -z "$lang" ]] && continue
        lang="${lang,,}"
        lang_forced="${lang_forced,,}"
        [[ "$lang_forced" == "true" ]] && lang_forced="True" || lang_forced="False"
        # Skip if external SRT already exists for this language
        if [[ -n "$(find "$dir" -maxdepth 1 -name "${stem}.${lang}.srt" -type f 2>/dev/null | head -1)" ]]; then
          continue
        fi
        local search_endpoint search_http
        if [[ "$ARR_TYPE" == "sonarr" ]]; then
          search_endpoint="${bazarr_url}/api/episodes/subtitles?seriesid=${MEDIA_ID}&episodeid=${bazarr_ref_id}&language=${lang}&forced=${lang_forced}&hi=False"
        else
          search_endpoint="${bazarr_url}/api/movies/subtitles?radarrid=${MEDIA_ID}&language=${lang}&forced=${lang_forced}&hi=False"
        fi
        search_http="$(curl -s -o /dev/null -w '%{http_code}' -X PATCH \
          -H "X-API-KEY: ${bazarr_key}" "$search_endpoint" 2>/dev/null)" || true
        log "BAZARR_SEARCH $ARR_TYPE lang=$lang forced=$lang_forced ref=$bazarr_ref_id http=$search_http"
      done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)
    fi

    # Translation fallback — for profile languages still missing an external
    # SRT after Bazarr search, attempt machine translation from the best
    # available source subtitle.  Runs in background to not block import.
    (
      sleep 5  # let Bazarr search complete first
      local translate_type translate_id_param
      if [[ "$ARR_TYPE" == "sonarr" ]]; then
        translate_type="episode"
        translate_id_param="episodeid=${bazarr_ref_id}"
      else
        translate_type="movie"
        translate_id_param="radarrid=${MEDIA_ID}"
      fi
      while IFS='|' read -r tlang tforced; do
        [[ -z "$tlang" ]] && continue
        tlang="${tlang,,}"
        tforced="${tforced,,}"
        # Skip if external SRT already exists
        if [[ -n "$(find "$dir" -maxdepth 1 -name "${stem}.${tlang}.srt" -type f 2>/dev/null | head -1)" ]]; then
          continue
        fi
        # Find best source SRT (largest other-language file)
        local source_srt=""
        local source_size=0
        local candidate csize
        for candidate in "$dir"/"${stem}".*.srt; do
          [[ -f "$candidate" ]] || continue
          [[ "$candidate" == *".${tlang}."* ]] && continue  # skip same language
          [[ "$candidate" == *".forced."* ]] && continue     # skip forced subs
          csize="$(stat -c '%s' "$candidate" 2>/dev/null || echo 0)"
          if [[ "$csize" -gt "$source_size" ]]; then
            source_size="$csize"
            source_srt="$candidate"
          fi
        done
        if [[ -z "$source_srt" ]]; then
          log "TRANSLATE_SKIP lang=$tlang — no source SRT available"
          continue
        fi
        # Call Bazarr translate API
        local translate_http
        translate_http="$(curl -s -o /dev/null -w '%{http_code}' -X PATCH \
          -H "X-API-KEY: ${bazarr_key}" \
          "${bazarr_url}/api/${translate_type}s/subtitles?${translate_id_param}&language=${tlang}&forced=False&hi=False&original_format=False" \
          -H 'Content-Type: application/json' \
          -d "$(jq -nc --arg path "$source_srt" --arg lang "$tlang" '{action:"translate",language:$lang,path:$path}')" \
          2>/dev/null)" || true
        log "TRANSLATE $ARR_TYPE lang=$tlang source=$(basename "$source_srt") ref=$bazarr_ref_id http=$translate_http"
      done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)
    ) >> "${LOG}" 2>&1 </dev/null &
    disown
  fi

  # Enqueue for codec conversion at highest priority (background, non-blocking)
  local codec_media_type
  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    codec_media_type="series"
  else
    codec_media_type="movie"
  fi
  /config/berenstuff/scripts/library_codec_manager.sh enqueue-import \
    --file "$MEDIA_PATH" --media-type "$codec_media_type" --ref-id "$MEDIA_ID" \
    --state-dir /APPBOX_DATA/storage/.transcode-state-media \
    >> /config/berenstuff/automation/logs/codec_enqueue_import.log 2>&1 </dev/null &
  disown

  if [[ "$WRITES" -gt 0 ]]; then
    notify_discord "SUCCESS" "**$WRITES** extracted · **$SKIPS** skipped · **$PRUNES** pruned"
  else
    notify_discord "INFO" "No new extractions · **$SKIPS** skipped · **$PRUNES** pruned"
  fi

  log "Done"
}

main "$@"
