#!/usr/bin/env bash
set -euo pipefail

DB="/opt/bazarr/data/db/bazarr.db"
LOG="/config/berenstuff/automation/logs/sonarr_profile_extract_on_import.log"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-https://discord.com/api/webhooks/1471677059360227478/REDACTED_WEBHOOK_TOKEN}"

WRITES=0
SKIPS=0
PRUNES=0
EVENT_TYPE=""
SERIES_ID=""
EPISODE_PATH=""
PROFILE_ID=""

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

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

getenv_fallback() {
  local a="$1"
  local b="$2"
  local v="${!a:-}"
  if [ -z "$v" ]; then
    v="${!b:-}"
  fi
  printf '%s' "$v"
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

file_size_bytes() {
  stat -c '%s' "$1" 2>/dev/null || echo 0
}

media_duration_seconds() {
  local file="$1"
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$file" 2>/dev/null | awk '{printf("%d\n",$1+0)}'
}

subtitle_quality_score() {
  local sub_file="$1"
  local media_seconds="$2"
  local forced_num="$3"
  awk -v media="$media_seconds" -v forced="$forced_num" '
    function ts_to_s(ts, a, b, c, d) {
      gsub(",", ".", ts)
      split(ts, a, ":")
      if (length(a) != 3) return 0
      return (a[1] * 3600) + (a[2] * 60) + a[3]
    }
    BEGIN {
      cues=0; text_lines=0; text_chars=0; shown=0.0
    }
    /^[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9][[:space:]]+-->[[:space:]]+[0-9][0-9]:[0-9][0-9]:[0-9][0-9],[0-9][0-9][0-9]/ {
      split($0, p, /[[:space:]]+-->[[:space:]]+/)
      s = ts_to_s(p[1]); e = ts_to_s(p[2])
      if (e > s) shown += (e - s)
      cues++
      next
    }
    NF > 0 && $0 !~ /^[0-9]+$/ {
      text_lines++
      text_chars += length($0)
    }
    END {
      if (cues <= 0 || text_chars <= 0) {
        print 0
        exit
      }
      coverage = (media > 0) ? ((shown / media) * 100.0) : 0.0
      score = (cues * 250) + (text_lines * 120) + text_chars
      if (forced == 1) {
        ideal = 8.0
        if (coverage < 0.2) score -= 200000
        if (coverage > 60.0) score -= int((coverage - 60.0) * 5000)
      } else {
        ideal = 35.0
        if (coverage < 5.0) score -= int((5.0 - coverage) * 10000)
        if (coverage > 98.0) score -= int((coverage - 98.0) * 5000)
      }
      diff = coverage - ideal
      if (diff < 0) diff = -diff
      score -= int(diff * 3000)
      printf "%d\n", score
    }
  ' "$sub_file"
}

list_lang_candidates() {
  local file="$1"
  local code="$2"
  local forced_num="$3"
  local stem
  declare -A seen=()
  stem="${file%.*}"

  shopt -s nullglob
  if [ "$forced_num" -eq 1 ]; then
    local raw=(
      "${stem}.${code}.forced.srt"
      "${stem}."*"${code}.forced.srt"
      "${stem}.${code}."*"forced.srt"
      "${stem}."*"${code}."*"forced.srt"
    )
    local p
    for p in "${raw[@]}"; do
      [ -f "$p" ] || continue
      if [ -z "${seen[$p]:-}" ]; then
        seen["$p"]=1
        printf '%s\n' "$p"
      fi
    done
  else
    local raw=(
      "${stem}.${code}.srt"
      "${stem}."*"${code}.srt"
      "${stem}.${code}."*"srt"
      "${stem}."*"${code}."*"srt"
    )
    local p
    for p in "${raw[@]}"; do
      [ -f "$p" ] || continue
      if [ -z "${seen[$p]:-}" ]; then
        seen["$p"]=1
        printf '%s\n' "$p"
      fi
    done
  fi
  shopt -u nullglob
}

prune_candidates_keep() {
  local keep="$1"
  shift
  local f
  for f in "$@"; do
    [ "$f" = "$keep" ] && continue
    [ -f "$f" ] || continue
    rm -f -- "$f"
    PRUNES=$((PRUNES + 1))
    log "PRUNE duplicate subtitle: $f"
  done
}

escape_regex() {
  printf '%s' "$1" | sed 's/[][(){}.^$*+?|\\/]/\\&/g'
}

extra_title_regex() {
  case "$1" in
    en) printf '%s' '(^|[^a-z])(english|eng|ingles)([^a-z]|$)' ;;
    es) printf '%s' '(^|[^a-z])(spanish|espanol|castellano|latino)([^a-z]|$)' ;;
    fr) printf '%s' '(^|[^a-z])(french|francais)([^a-z]|$)' ;;
    pt) printf '%s' '(^|[^a-z])(portuguese|portugues|brazilian)([^a-z]|$)' ;;
    zt) printf '%s' '(繁體|traditional|hant|cht|big5)' ;;
    zh) printf '%s' '(chinese|mandarin|cantonese|中文|国语|國語|粤语|粵語|簡體|简体|simplified|hans|chs)' ;;
    *) printf '%s' '' ;;
  esac
}

get_stream_idx_standard() {
  local json="$1"
  local code="$2"
  local forced_num="$3"
  local code3=""
  local code3b=""
  local lname=""
  local name_rx=""
  local extra_rx=""
  local title_rx=""

  IFS='|' read -r code3 code3b lname <<<"$(sqlite3 -separator '|' "$DB" "
    SELECT lower(coalesce(code3,'')), lower(coalesce(code3b,'')), lower(coalesce(name,''))
    FROM table_settings_languages
    WHERE lower(code2)='${code}'
    LIMIT 1;
  ")"

  extra_rx="$(extra_title_regex "$code")"
  if [ -n "$lname" ]; then
    name_rx="$(escape_regex "$lname")"
  fi

  if [ -n "$name_rx" ] && [ -n "$extra_rx" ]; then
    title_rx="${name_rx}|${extra_rx}"
  elif [ -n "$name_rx" ]; then
    title_rx="$name_rx"
  else
    title_rx="$extra_rx"
  fi

  printf '%s' "$json" | jq -r \
    --arg code "$code" \
    --arg code3 "$code3" \
    --arg code3b "$code3b" \
    --arg titleRx "$title_rx" \
    --argjson forced "$forced_num" '
      [ .streams[]
        | (.tags.language // "" | ascii_downcase) as $lang
        | (.tags.title // "" | ascii_downcase) as $title
        | select(
            ($lang == $code)
            or ($code3 != "" and $lang == $code3)
            or ($code3b != "" and $lang == $code3b)
            or ($titleRx != "" and ($title | test($titleRx; "i")))
          )
        | select((.disposition.forced // 0) == $forced)
        | .index
      ] | first // empty
    '
}

get_stream_idx_zh() {
  local json="$1"
  local code="$2"
  local forced_num="$3"
  local lang_rx title_rx

  if [ "$code" = "zt" ]; then
    lang_rx='^(zht|cht)$'
    title_rx='(繁體|traditional|hant|cht|big5)'
  else
    lang_rx='^(zho|chi|zh|zhs|chs|zht|cht)$'
    title_rx='(chinese|mandarin|cantonese|中文|国语|國語|粤语|粵語|簡體|简体|繁體|traditional|simplified|hant|hans|cht|chs)'
  fi

  printf '%s' "$json" | jq -r \
    --arg langRx "$lang_rx" \
    --arg titleRx "$title_rx" \
    --argjson forced "$forced_num" '
      [ .streams[]
        | (.tags.language // "" | ascii_downcase) as $lang
        | (.tags.title // "" | ascii_downcase) as $title
        | select(($lang | test($langRx; "i")) or ($title | test($titleRx; "i")))
        | select((.disposition.forced // 0) == $forced)
        | .index
      ] | first // empty
    '
}

extract_target() {
  local file="$1"
  local code="$2"
  local forced_bool="$3"
  local forced_num="0"
  local json idx out suffix tmp_out
  local -a existing=()
  local best_existing=""
  local best_existing_size=0
  local best_existing_score=0
  local extracted_size=0
  local extracted_score=0
  local media_seconds=0
  local f fsize fscore

  if [ "$forced_bool" = "true" ]; then
    forced_num="1"
  fi

  json="$(ffprobe -v error -print_format json -show_streams -select_streams s "$file" 2>/dev/null || true)"
  [ -z "$json" ] && return 0

  case "$code" in
    zh|zt)
      idx="$(get_stream_idx_zh "$json" "$code" "$forced_num" || true)"
      ;;
    *)
      idx="$(get_stream_idx_standard "$json" "$code" "$forced_num" || true)"
      ;;
  esac

  [ -z "$idx" ] && return 0

  suffix="$code"
  out="${file%.*}.${suffix}.srt"
  if [ "$forced_num" -eq 1 ]; then
    out="${file%.*}.${suffix}.forced.srt"
  fi

  mapfile -t existing < <(list_lang_candidates "$file" "$code" "$forced_num")
  media_seconds="$(media_duration_seconds "$file")"
  [ -z "$media_seconds" ] && media_seconds=0

  for f in "${existing[@]}"; do
    [ -f "$f" ] || continue
    fsize="$(file_size_bytes "$f")"
    fscore="$(subtitle_quality_score "$f" "$media_seconds" "$forced_num")"
    if [ -z "$best_existing" ] || [ "$fscore" -gt "$best_existing_score" ] || { [ "$fscore" -eq "$best_existing_score" ] && [ "$fsize" -gt "$best_existing_size" ]; }; then
      best_existing_score="$fscore"
      best_existing_size="$fsize"
      best_existing="$f"
    fi
  done

  tmp_out="$(mktemp --suffix=.srt)"
  if ! ffmpeg -nostdin -loglevel error -y -i "$file" -map "0:${idx}" -c:s srt "$tmp_out"; then
    rm -f "$tmp_out"
    return 0
  fi
  extracted_size="$(file_size_bytes "$tmp_out")"
  extracted_score="$(subtitle_quality_score "$tmp_out" "$media_seconds" "$forced_num")"

  if [ -z "$best_existing" ] || [ "$extracted_score" -gt "$best_existing_score" ] || { [ "$extracted_score" -eq "$best_existing_score" ] && [ "$extracted_size" -gt "$best_existing_size" ]; }; then
    mv -f "$tmp_out" "$out"
    WRITES=$((WRITES + 1))
    log "WROTE best subtitle: $out (score=$extracted_score size=$extracted_size, prev_best_score=$best_existing_score prev_best_size=$best_existing_size)"
    if [ "${#existing[@]}" -gt 0 ]; then
      prune_candidates_keep "$out" "${existing[@]}"
    fi
    return 0
  fi

  rm -f "$tmp_out"
  SKIPS=$((SKIPS + 1))
  log "SKIP extracted subtitle for code=$code forced=$forced_num (extracted_score=$extracted_score extracted_size=$extracted_size <= best_existing_score=$best_existing_score best_existing_size=$best_existing_size)"

  if [ "$best_existing" != "$out" ]; then
    mv -f "$best_existing" "$out"
    best_existing="$out"
    log "NORMALIZE keep best existing as canonical: $out"
  fi
  if [ "${#existing[@]}" -gt 0 ]; then
    prune_candidates_keep "$best_existing" "${existing[@]}"
  fi
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
