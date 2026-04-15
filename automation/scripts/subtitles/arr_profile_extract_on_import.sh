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
  local esc_path profile_id default_profile _attempt
  esc_path="$(sql_escape "$media_path")"

  for _attempt in $(seq 1 10); do
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
    sleep 0.5
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
# Deferred heavy work — runs in background after main() returns to arr
# Inherits: MEDIA_PATH, MEDIA_ID, PROFILE_ID, ARR_TYPE, EVENT_TYPE,
#           items, profile_set, WRITES, SKIPS, PRUNES (all global)
# ---------------------------------------------------------------------------
deferred_main() {
  # Cache raw ffprobe subtitle stream JSON once — reused by both the
  # non-profile extraction block and the selective-strip block below.
  local _raw_sub_json
  _raw_sub_json="$(ffprobe -v quiet -print_format json -show_streams -select_streams s "$MEDIA_PATH" 2>/dev/null || true)"

  # --- Extract profile languages from embedded streams ---
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

  # Extract non-profile embedded text subs before stripping — preserves them
  # as external SRTs for DeepL translation source use.
  # profile_set is already computed in main() — no recomputation needed.
  if [[ -n "$profile_set" ]]; then
    local emb_json emb_count np_dir np_stem
    emb_json="$(jq -c '[.streams[] | {index, codec_name, tags: {language: (.tags.language // "und")}, forced: (.disposition.forced // 0)}]' <<<"$_raw_sub_json")"
    emb_count="$(jq 'length' <<<"$emb_json")"
    np_dir="$(dirname "$MEDIA_PATH")"
    np_stem="$(basename "${MEDIA_PATH%.*}")"

    for ((i=0; i<emb_count; i++)); do
      local np_lang np_codec np_idx np_forced
      np_idx="$(jq -r ".[$i].index" <<<"$emb_json")"
      np_lang="$(jq -r ".[$i].tags.language" <<<"$emb_json")"
      np_codec="$(jq -r ".[$i].codec_name" <<<"$emb_json")"
      np_forced="$(jq -r ".[$i].forced" <<<"$emb_json")"

      lang_in_set "$np_lang" "$profile_set" && continue

      is_text_sub_codec "$np_codec" || continue

      local np_norm np_out_name np_out
      np_norm="$(normalize_track_lang "$np_lang")"
      np_out_name="${np_stem}.${np_norm}"
      [[ "$np_forced" -eq 1 ]] && np_out_name+=".forced"
      np_out_name+=".srt"
      np_out="${np_dir}/${np_out_name}"

      [[ -f "$np_out" ]] && continue

      if ffmpeg -v quiet -i "$MEDIA_PATH" -map "0:${np_idx}" -f srt "$np_out" </dev/null 2>/dev/null && [[ -s "$np_out" ]]; then
        log "EXTRACTED non-profile idx=${np_idx} lang=${np_norm} → ${np_out_name}"

        # Detect actual language for 'und' tracks and rename
        if [[ "$np_norm" == "und" ]]; then
          local detected_lang
          if detected_lang="$(detect_srt_language "$np_out" "${DEEPL_API_KEY:-}")"; then
            detected_lang="$(normalize_track_lang "$detected_lang")"
            local renamed="${np_stem}.${detected_lang}"
            [[ "$np_forced" -eq 1 ]] && renamed+=".forced"
            renamed+=".srt"
            local renamed_path="${np_dir}/${renamed}"
            if [[ ! -f "$renamed_path" ]]; then
              mv "$np_out" "$renamed_path"
              log "DETECTED und → ${detected_lang} → renamed to ${renamed}"
            fi
          else
            log "WARN: language detection failed for und track (kept as und)"
          fi
        fi
      else
        rm -f "$np_out"
        log "WARN: non-profile extraction failed idx=${np_idx} lang=${np_norm} (non-fatal)"
      fi
    done
  fi

  # --- enforce_one_per_lang: consolidate to 1-best per language ---
  local media_secs
  media_secs="$(media_duration_seconds "$MEDIA_PATH")"
  [[ -z "$media_secs" ]] && media_secs=0
  local -a eopl_strip_indices=()
  declare -A eopl_kept_langs=()
  enforce_one_per_lang "$MEDIA_PATH" "$media_secs" 0 eopl_strip_indices eopl_kept_langs

  # Selective strip: remove embedded tracks that lost in enforce_one_per_lang
  # AND remaining non-profile embedded tracks (after translation in 4C)
  # KEEP profile-lang embedded tracks that won their group
  local emb_json_sel emb_count_sel
  emb_json_sel="$(jq -c '[.streams[] | {index, codec_name, tags: {language: (.tags.language // "und")}, forced: (.disposition.forced // 0)}]' <<<"$_raw_sub_json")"
  emb_count_sel="$(jq 'length' <<<"$emb_json_sel")"
  local -a selective_strip_indices=()

  # Add enforce_one_per_lang losers
  for idx in "${eopl_strip_indices[@]}"; do
    selective_strip_indices+=("$idx")
  done

  # Add remaining non-profile embedded tracks
  if [[ -n "$profile_set" ]] && [[ "$emb_count_sel" -gt 0 ]]; then
    for ((i=0; i<emb_count_sel; i++)); do
      local sel_idx sel_lang sel_forced
      sel_idx="$(jq -r ".[$i].index" <<<"$emb_json_sel")"
      sel_lang="$(jq -r ".[$i].tags.language" <<<"$emb_json_sel")"
      sel_forced="$(jq -r ".[$i].forced" <<<"$emb_json_sel")"
      local sel_norm
      sel_norm="$(normalize_track_lang "$sel_lang")"
      local sel_key="${sel_norm}|${sel_forced}"

      # Skip if this track won its group in enforce_one_per_lang AND is a profile lang
      if [[ "${eopl_kept_langs[$sel_key]:-}" == "embedded" ]] && lang_in_set "$sel_norm" "$profile_set"; then
        log "KEEP embedded winner: idx=$sel_idx lang=$sel_norm (profile language)"
        continue
      fi

      # Check if already in the strip list (from eopl losers)
      local already_listed=0
      for existing_idx in "${selective_strip_indices[@]}"; do
        [[ "$existing_idx" == "$sel_idx" ]] && { already_listed=1; break; }
      done
      [[ "$already_listed" -eq 1 ]] && continue

      # Non-profile or non-winning embedded → strip
      selective_strip_indices+=("$sel_idx")
    done
  fi

  if [[ ${#selective_strip_indices[@]} -gt 0 ]]; then
    strip_embedded_by_indices "$MEDIA_PATH" selective_strip_indices || log "WARN: selective strip failed (non-fatal)"
  fi

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
          -H "X-API-KEY: ${bazarr_key}" "$search_endpoint" </dev/null 2>/dev/null)" || true
        log "BAZARR_SEARCH $ARR_TYPE lang=$lang forced=$lang_forced ref=$bazarr_ref_id http=$search_http"
      done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)
    fi

    # Synchronous translation — for profile languages still missing after
    # Bazarr search, translate from best available non-profile external SRT.
    # Non-profile SRTs are still on disk at this point (not yet cleaned up).
    local stem_tr dir_tr
    stem_tr="$(basename "${MEDIA_PATH%.*}")"
    dir_tr="$(dirname "$MEDIA_PATH")"
    while IFS='|' read -r tr_lang _tr_forced; do
      [[ -z "$tr_lang" ]] && continue
      tr_lang="${tr_lang,,}"
      # Skip if external SRT already exists
      if [[ -n "$(find "$dir_tr" -maxdepth 1 -name "${stem_tr}.${tr_lang}.srt" -type f 2>/dev/null | head -1)" ]]; then
        continue
      fi
      # Find best non-profile external SRT as translation source
      local best_src="" best_src_score=-999999
      while IFS= read -r src_srt; do
        [[ -z "$src_srt" ]] && continue
        local src_base src_lang_tr
        src_base="$(basename "$src_srt")"
        src_lang_tr="$(extract_srt_lang "$src_base" "$stem_tr")"
        [[ -z "$src_lang_tr" ]] && src_lang_tr="und"
        src_lang_tr="$(normalize_track_lang "$src_lang_tr")"
        # Only non-profile SRTs as source
        lang_in_set "$src_lang_tr" "$profile_set" && continue
        local src_score_val
        src_score_val="$(subtitle_quality_score "$src_srt" "$media_secs" 0)"
        if [[ "$src_score_val" -gt "$best_src_score" ]]; then
          best_src_score="$src_score_val"
          best_src="$src_srt"
        fi
      done < <(find "$dir_tr" -maxdepth 1 -name "${stem_tr}.*.srt" -type f 2>/dev/null)
      if [[ -n "$best_src" ]]; then
        log "SYNC_TRANSLATE: translating from $(basename "$best_src") for missing lang=$tr_lang"
        (
          source /config/berenstuff/.env
          PYTHONPATH=/config/berenstuff/automation/scripts \
            python3 /config/berenstuff/automation/scripts/translation/translator.py \
            translate --file "$MEDIA_PATH"
        ) >> /config/berenstuff/automation/logs/deepl_translate.log 2>&1 </dev/null
        # Break after first translation attempt (translator handles all missing langs)
        break
      fi
    done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)

    # Remove non-profile sidecar SRTs left over from extraction/translation
    if [[ -n "$profile_set" ]]; then
      while IFS= read -r stale_srt; do
        [[ -z "$stale_srt" ]] && continue
        local stale_base stale_lang
        stale_base="$(basename "$stale_srt")"
        stale_lang="$(extract_srt_lang "$stale_base" "$stem_tr")"
        [[ -z "$stale_lang" ]] && stale_lang="und"
        stale_lang="$(normalize_track_lang "$stale_lang")"
        if ! lang_in_set "$stale_lang" "$profile_set"; then
          rm -f "$stale_srt" "${stale_srt}.gtranslate"
          log "CLEANUP: removed non-profile sidecar: $stale_base"
        fi
      done < <(find "$dir_tr" -maxdepth 1 -name "${stem_tr}.*.srt" -type f 2>/dev/null)
    fi

    # Mark remaining profile language gaps for upgrade retry
    local upgrade_state_db="/APPBOX_DATA/storage/.subtitle-quality-state/subtitle_quality_state.db"
    while IFS='|' read -r gap_lang gap_forced; do
      [[ -z "$gap_lang" ]] && continue
      gap_lang="${gap_lang,,}"
      local gap_forced_num=0
      [[ "$gap_forced" == "true" ]] && gap_forced_num=1
      # Skip if SRT exists
      if [[ -n "$(find "$dir_tr" -maxdepth 1 -name "${stem_tr}.${gap_lang}*.srt" -type f 2>/dev/null | head -1)" ]]; then
        continue
      fi
      upsert_needs_upgrade "$upgrade_state_db" "$MEDIA_PATH" "$gap_lang" "$gap_forced_num" "MISSING" 0 "none"
      log "UPGRADE_MARK: missing profile lang=$gap_lang forced=$gap_forced_num for upgrade retry"
    done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)
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

  # Enqueue for subtitle auto-maintain (ensures processing even if --since window is missed)
  /config/berenstuff/automation/scripts/subtitles/subtitle_quality_manager.sh enqueue \
    "$MEDIA_PATH" >> /config/berenstuff/automation/logs/subtitle_quality_manager.log 2>&1 </dev/null &
  disown

  # Check streaming availability and tag (background, non-blocking)
  local streaming_media_type
  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    streaming_media_type="series"
  else
    streaming_media_type="movie"
  fi
  (
    source /config/berenstuff/.env
    PYTHONPATH=/config/berenstuff/automation/scripts \
      python3 /config/berenstuff/automation/scripts/streaming/streaming_checker.py \
      check-import --file "$MEDIA_PATH" --media-type "$streaming_media_type" --arr-id "$MEDIA_ID"
  ) >> /config/berenstuff/automation/logs/streaming_check_import.log 2>&1 </dev/null &
  disown

  if [[ "$WRITES" -gt 0 ]]; then
    notify_discord "SUCCESS" "**$WRITES** extracted · **$SKIPS** skipped · **$PRUNES** pruned"
  else
    notify_discord "INFO" "No new extractions · **$SKIPS** skipped · **$PRUNES** pruned"
  fi

  log "Done"
}

# ---------------------------------------------------------------------------
# Main — fast path only; spawns deferred_main in background before returning
# ---------------------------------------------------------------------------
main() {
  log "EVENT=$EVENT_TYPE arr=$ARR_TYPE media_id=$MEDIA_ID path=$MEDIA_PATH"

  # --- FAST PATH (synchronous) ---
  if [[ -z "$MEDIA_PATH" || ! -f "$MEDIA_PATH" ]]; then
    log "Skip: no media file path"
    notify_discord "SKIP" "Reason: no media file path"
    exit 0
  fi

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

  local profile_check
  if [[ "$ARR_TYPE" == "sonarr" ]]; then
    profile_check="$(sqlite3 "$DB" "SELECT 1 FROM table_shows WHERE sonarrSeriesId=$MEDIA_ID AND profileId=$PROFILE_ID LIMIT 1;")"
  else
    profile_check="$(sqlite3 "$DB" "SELECT 1 FROM table_movies WHERE radarrId=$MEDIA_ID AND profileId=$PROFILE_ID LIMIT 1;")"
  fi
  if [[ "$profile_check" != "1" ]]; then
    log "Fallback profile applied for $ARR_TYPE id=$MEDIA_ID: profile=$PROFILE_ID"
  fi

  # Pre-compute items and profile_set for deferred work
  items="$(sqlite3 "$DB" "SELECT items FROM table_languages_profiles WHERE profileId=$PROFILE_ID LIMIT 1;")"
  if [[ -z "$items" ]]; then
    log "Skip: empty profile items"
    notify_discord "SKIP" "Reason: empty profile items"
    exit 0
  fi

  profile_set=""
  while IFS='|' read -r _plang _; do
    [[ -z "$_plang" ]] && continue
    profile_set+=" $(expand_lang_codes "${_plang,,}") "
  done < <(printf '%s' "$items" | jq -r '.[] | "\(.language)|\(.forced)"' | sort -u)

  # --- SPAWN DEFERRED WORK IN BACKGROUND ---
  log "Spawning deferred work for $ARR_TYPE id=$MEDIA_ID profile=$PROFILE_ID"
  deferred_main </dev/null >> "$LOG" 2>&1 &
  disown
}

main "$@"
