#!/usr/bin/env bash
set -euo pipefail

BAZARR_URL="${BAZARR_URL:-http://127.0.0.1:6767/bazarr}"
BAZARR_API_KEY="${BAZARR_API_KEY:-}"
BAZARR_DB="${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}"
RADARR_URL="${RADARR_URL:-http://127.0.0.1:7878/radarr}"
RADARR_KEY="${RADARR_KEY:-}"
SONARR_URL="${SONARR_URL:-http://127.0.0.1:8989/sonarr}"
SONARR_KEY="${SONARR_KEY:-}"
STATE_DIR="${STATE_DIR:-/APPBOX_DATA/storage/.subtitle-recovery-state}"
STATE_DB="${STATE_DB:-$STATE_DIR/recovery_state.db}"
LOG_PATH="${LOG_PATH:-/config/berenstuff/automation/logs/bazarr_subtitle_recovery.log}"
MAX_ITEMS="${MAX_ITEMS:-0}"
BAZARR_RETRY_COOLDOWN_SEC="${BAZARR_RETRY_COOLDOWN_SEC:-21600}"   # 6h
ARR_RETRY_COOLDOWN_SEC="${ARR_RETRY_COOLDOWN_SEC:-86400}"         # 24h
TRANSLATE_RETRY_COOLDOWN_SEC="${TRANSLATE_RETRY_COOLDOWN_SEC:-86400}" # 24h

usage() {
  cat <<'EOF'
Usage: bazarr_subtitle_recovery.sh [options]

For items with missing subtitles in Bazarr:
1) try Bazarr subtitle download
2) if still missing and another subtitle language exists for that file, auto-translate from existing -> missing language
3) if still missing, trigger Sonarr/Radarr search command

Options:
  --bazarr-url URL
  --bazarr-api-key KEY
  --bazarr-db PATH
  --radarr-url URL
  --radarr-key KEY
  --sonarr-url URL
  --sonarr-key KEY
  --state-dir PATH
  --state-db PATH
  --log PATH
  --max-items N
  --bazarr-retry-cooldown-sec N
  --arr-retry-cooldown-sec N
  --translate-retry-cooldown-sec N
  --help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bazarr-url) BAZARR_URL="${2:-}"; shift 2 ;;
    --bazarr-api-key) BAZARR_API_KEY="${2:-}"; shift 2 ;;
    --bazarr-db) BAZARR_DB="${2:-}"; shift 2 ;;
    --radarr-url) RADARR_URL="${2:-}"; shift 2 ;;
    --radarr-key) RADARR_KEY="${2:-}"; shift 2 ;;
    --sonarr-url) SONARR_URL="${2:-}"; shift 2 ;;
    --sonarr-key) SONARR_KEY="${2:-}"; shift 2 ;;
    --state-dir) STATE_DIR="${2:-}"; shift 2 ;;
    --state-db) STATE_DB="${2:-}"; shift 2 ;;
    --log) LOG_PATH="${2:-}"; shift 2 ;;
    --max-items) MAX_ITEMS="${2:-}"; shift 2 ;;
    --bazarr-retry-cooldown-sec) BAZARR_RETRY_COOLDOWN_SEC="${2:-}"; shift 2 ;;
    --arr-retry-cooldown-sec) ARR_RETRY_COOLDOWN_SEC="${2:-}"; shift 2 ;;
    --translate-retry-cooldown-sec) TRANSLATE_RETRY_COOLDOWN_SEC="${2:-}"; shift 2 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

for n in "$MAX_ITEMS" "$BAZARR_RETRY_COOLDOWN_SEC" "$ARR_RETRY_COOLDOWN_SEC" "$TRANSLATE_RETRY_COOLDOWN_SEC"; do
  [[ "$n" =~ ^[0-9]+$ ]] || { echo "Numeric option expected, got: $n" >&2; exit 1; }
done

[[ -n "$BAZARR_API_KEY" ]] || { echo "Missing Bazarr API key." >&2; exit 1; }
[[ -n "$RADARR_KEY" ]] || { echo "Missing Radarr API key." >&2; exit 1; }
[[ -n "$SONARR_KEY" ]] || { echo "Missing Sonarr API key." >&2; exit 1; }

mkdir -p "$STATE_DIR" "$(dirname "$LOG_PATH")"

TMPDIR_RECOVERY="$(mktemp -d /tmp/bazarr_subtitle_recovery.XXXXXXXXXX)"
cleanup_tmp() { rm -rf "$TMPDIR_RECOVERY"; }
trap cleanup_tmp EXIT

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$LOG_PATH" >/dev/null
}

LOCK_FILE="$STATE_DIR/bazarr_subtitle_recovery.lock"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  log "Another recovery run is active. Exiting."
  exit 0
fi

sqlite3 "$STATE_DB" '
CREATE TABLE IF NOT EXISTS recovery_state (
  media_type TEXT NOT NULL,
  media_id INTEGER NOT NULL,
  lang_code TEXT NOT NULL,
  forced INTEGER NOT NULL DEFAULT 0,
  hi INTEGER NOT NULL DEFAULT 0,
  last_bazarr_try_ts INTEGER,
  last_translate_try_ts INTEGER,
  last_arr_try_ts INTEGER,
  bazarr_attempts INTEGER NOT NULL DEFAULT 0,
  last_result TEXT,
  updated_at TEXT,
  PRIMARY KEY (media_type, media_id, lang_code, forced, hi)
);
'

if [[ -z "$(sqlite3 "$STATE_DB" "SELECT 1 FROM pragma_table_info('recovery_state') WHERE name='bazarr_attempts' LIMIT 1;")" ]]; then
  sqlite3 "$STATE_DB" "ALTER TABLE recovery_state ADD COLUMN bazarr_attempts INTEGER NOT NULL DEFAULT 0;"
fi

jsonish_to_json() {
  python3 -c "
import sys, json, ast
try:
    data = ast.literal_eval(sys.stdin.read())
    json.dump(data, sys.stdout)
except Exception:
    sys.exit(1)
"
}

state_get_ts() {
  local media_type="$1" media_id="$2" lang="$3" forced="$4" hi="$5" col="$6"
  sqlite3 "$STATE_DB" "
    SELECT COALESCE($col,0) FROM recovery_state
    WHERE media_type='$media_type' AND media_id=$media_id AND lang_code='$lang' AND forced=$forced AND hi=$hi
    LIMIT 1;
  "
}

state_set() {
  local media_type="$1" media_id="$2" lang="$3" forced="$4" hi="$5"
  local baz_ts="${6:-0}" tr_ts="${7:-0}" arr_ts="${8:-0}" result="$9"
  [[ -n "$baz_ts" ]] || baz_ts=0
  [[ -n "$tr_ts" ]] || tr_ts=0
  [[ -n "$arr_ts" ]] || arr_ts=0
  result="${result//\'/\'\'}"
  sqlite3 "$STATE_DB" "
    INSERT INTO recovery_state
      (media_type, media_id, lang_code, forced, hi, last_bazarr_try_ts, last_translate_try_ts, last_arr_try_ts, bazarr_attempts, last_result, updated_at)
    VALUES
      ('$media_type', $media_id, '$lang', $forced, $hi, $baz_ts, $tr_ts, $arr_ts, 0, '$result', datetime('now'))
    ON CONFLICT(media_type, media_id, lang_code, forced, hi) DO UPDATE SET
      last_bazarr_try_ts=excluded.last_bazarr_try_ts,
      last_translate_try_ts=excluded.last_translate_try_ts,
      last_arr_try_ts=excluded.last_arr_try_ts,
      last_result=excluded.last_result,
      updated_at=excluded.updated_at;
  "
}

state_get_bazarr_attempts() {
  local media_type="$1" media_id="$2" lang="$3" forced="$4" hi="$5"
  sqlite3 "$STATE_DB" "
    SELECT COALESCE(bazarr_attempts,0) FROM recovery_state
    WHERE media_type='$media_type' AND media_id=$media_id AND lang_code='$lang' AND forced=$forced AND hi=$hi
    LIMIT 1;
  "
}

state_inc_bazarr_attempts() {
  local media_type="$1" media_id="$2" lang="$3" forced="$4" hi="$5"
  sqlite3 "$STATE_DB" "
    INSERT INTO recovery_state
      (media_type, media_id, lang_code, forced, hi, bazarr_attempts, updated_at)
    VALUES
      ('$media_type', $media_id, '$lang', $forced, $hi, 1, datetime('now'))
    ON CONFLICT(media_type, media_id, lang_code, forced, hi) DO UPDATE SET
      bazarr_attempts=COALESCE(bazarr_attempts,0)+1,
      updated_at=datetime('now');
  "
}

state_reset_bazarr_attempts() {
  local media_type="$1" media_id="$2" lang="$3" forced="$4" hi="$5"
  sqlite3 "$STATE_DB" "
    UPDATE recovery_state
    SET bazarr_attempts=0, updated_at=datetime('now')
    WHERE media_type='$media_type' AND media_id=$media_id AND lang_code='$lang' AND forced=$forced AND hi=$hi;
  "
}

should_run_with_cooldown() {
  local last_ts="$1" cooldown="$2" now_ts="$3"
  [[ -z "$last_ts" || "$last_ts" -eq 0 ]] && return 0
  (( now_ts - last_ts >= cooldown ))
}

has_missing_target() {
  local missing_raw="$1" lang="$2" forced="$3" hi="$4"
  local target="$lang"
  [[ "$forced" == "1" ]] && target="${target}:forced"
  [[ "$hi" == "1" ]] && target="${target}:hi"
  [[ "$missing_raw" == "[]" || -z "$missing_raw" ]] && return 1
  printf '%s' "$missing_raw" | jsonish_to_json | jq -e --arg t "$target" '.[] | select(. == $t)' >/dev/null 2>&1
}

pick_best_source_sub_for_target() {
  local subtitles_raw="$1" target_lang="$2"
  [[ -z "$subtitles_raw" || "$subtitles_raw" == "[]" ]] && { printf ''; return 0; }
  printf '%s' "$subtitles_raw" | jsonish_to_json | jq -r '
    [ .[]
      | select(length >= 2 and .[1] != null and .[0] != null)
      | {
          lang: ((.[0] | split(":"))[0]),
          path: .[1],
          size: (.[2] // 0)
        }
      | select(.lang != $target)
    ]
    | sort_by(.size)
    | reverse
    | .[0] // {}
    | [.lang, .path]
    | @tsv
  ' --arg target "$target_lang"
}

try_bazarr_download_episode() {
  local series_id="$1" episode_id="$2" lang="$3" forced="$4" hi="$5"
  curl -sS -o "$TMPDIR_RECOVERY/bazarr_episode_dl.out" -w "%{http_code}" \
    -X PATCH \
    -H "X-API-KEY: $BAZARR_API_KEY" \
    --data-urlencode "seriesid=$series_id" \
    --data-urlencode "episodeid=$episode_id" \
    --data-urlencode "language=$lang" \
    --data-urlencode "forced=$forced" \
    --data-urlencode "hi=$hi" \
    "$BAZARR_URL/api/episodes/subtitles"
}

try_bazarr_download_movie() {
  local movie_id="$1" lang="$2" forced="$3" hi="$4"
  curl -sS -o "$TMPDIR_RECOVERY/bazarr_movie_dl.out" -w "%{http_code}" \
    -X PATCH \
    -H "X-API-KEY: $BAZARR_API_KEY" \
    --data-urlencode "radarrid=$movie_id" \
    --data-urlencode "language=$lang" \
    --data-urlencode "forced=$forced" \
    --data-urlencode "hi=$hi" \
    "$BAZARR_URL/api/movies/subtitles"
}

try_translate_to_lang() {
  local media_type="$1" media_id="$2" source_sub_path="$3" target_lang="$4"
  local btype="episode"
  [[ "$media_type" == "movie" ]] && btype="movie"
  curl -sS -o "$TMPDIR_RECOVERY/bazarr_translate.out" -w "%{http_code}" \
    -X PATCH \
    -H "X-API-KEY: $BAZARR_API_KEY" \
    --data-urlencode "action=translate" \
    --data-urlencode "language=$target_lang" \
    --data-urlencode "path=$source_sub_path" \
    --data-urlencode "type=$btype" \
    --data-urlencode "id=$media_id" \
    --data-urlencode "forced=False" \
    --data-urlencode "hi=False" \
    "$BAZARR_URL/api/subtitles"
}

trigger_arr_search_episode() {
  local episode_id="$1"
  curl -sS -o "$TMPDIR_RECOVERY/sonarr_search.out" -w "%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"EpisodeSearch\",\"episodeIds\":[${episode_id}]}" \
    "$SONARR_URL/api/v3/command?apikey=$SONARR_KEY"
}

trigger_arr_search_movie() {
  local movie_id="$1"
  curl -sS -o "$TMPDIR_RECOVERY/radarr_search.out" -w "%{http_code}" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"MoviesSearch\",\"movieIds\":[${movie_id}]}" \
    "$RADARR_URL/api/v3/command?apikey=$RADARR_KEY"
}

now_ts="$(date +%s)"
scanned=0
handled=0
bazarr_attempts=0
translations=0
arr_triggers=0
SONARR_ACTIVE_IDS=""
RADARR_ACTIVE_IDS=""
BAZARR_SUB_BUSY=0

log "Start recovery max_items=$MAX_ITEMS"

load_active_arr_queue_ids() {
  SONARR_ACTIVE_IDS="$(
    curl -sS "$SONARR_URL/api/v3/queue?page=1&pageSize=1000&sortDirection=descending&apikey=$SONARR_KEY" 2>/dev/null \
      | jq -r '.records[]? | .episodeId // empty' \
      | awk 'NF>0' \
      | sort -u \
      | tr '\n' ' '
  )"
  RADARR_ACTIVE_IDS="$(
    curl -sS "$RADARR_URL/api/v3/queue?page=1&pageSize=1000&sortDirection=descending&apikey=$RADARR_KEY" 2>/dev/null \
      | jq -r '.records[]? | .movieId // empty' \
      | awk 'NF>0' \
      | sort -u \
      | tr '\n' ' '
  )"
}

id_in_list() {
  local id="$1" list="$2"
  [[ " $list " == *" $id "* ]]
}

detect_bazarr_subtitle_jobs_busy() {
  local running
  running="$(
    curl -sS -H "X-API-KEY: $BAZARR_API_KEY" "$BAZARR_URL/api/system/jobs" 2>/dev/null \
      | jq -r '
          [.data[]?
            | select((.status=="running" or .status=="pending")
                     and ((.job_name // "") | test("Searching subtitles|Downloading missing subtitles|Translated from"; "i")))
          ] | length
        ' 2>/dev/null || echo 0
  )"
  [[ -n "$running" ]] || running=0
  if [[ "$running" -gt 0 ]]; then
    BAZARR_SUB_BUSY=1
  else
    BAZARR_SUB_BUSY=0
  fi
}

process_item() {
  local media_type="$1" media_id="$2" series_id="$3" missing_raw="$4" subtitles_raw="$5"
  local -a miss=()
  local item lang forced hi token
  local last_baz last_tr last_arr
  local dl_http tr_http arr_http
  local missing_after src_lang src_path
  local baz_attempts=0

  if [[ "$BAZARR_SUB_BUSY" -eq 1 ]]; then
    log "SKIP_BAZARR_BUSY type=$media_type id=$media_id"
    return 0
  fi

  if [[ "$media_type" == "episode" ]] && id_in_list "$media_id" "$SONARR_ACTIVE_IDS"; then
    log "SKIP_ACTIVE_QUEUE type=episode id=$media_id source=sonarr"
    return 0
  fi
  if [[ "$media_type" == "movie" ]] && id_in_list "$media_id" "$RADARR_ACTIVE_IDS"; then
    log "SKIP_ACTIVE_QUEUE type=movie id=$media_id source=radarr"
    return 0
  fi

  mapfile -t miss < <(printf '%s' "$missing_raw" | jsonish_to_json | jq -r '.[]')
  for item in "${miss[@]}"; do
    lang="${item%%:*}"
    forced=0
    hi=0
    [[ "$item" == *":forced" ]] && forced=1
    [[ "$item" == *":hi" ]] && hi=1
    token="$lang"
    [[ "$forced" -eq 1 ]] && token="${token}:forced"
    [[ "$hi" -eq 1 ]] && token="${token}:hi"

    last_baz="$(state_get_ts "$media_type" "$media_id" "$lang" "$forced" "$hi" "last_bazarr_try_ts")"
    last_tr="$(state_get_ts "$media_type" "$media_id" "$lang" "$forced" "$hi" "last_translate_try_ts")"
    last_arr="$(state_get_ts "$media_type" "$media_id" "$lang" "$forced" "$hi" "last_arr_try_ts")"
    baz_attempts="$(state_get_bazarr_attempts "$media_type" "$media_id" "$lang" "$forced" "$hi")"
    [[ -n "$baz_attempts" ]] || baz_attempts=0

    if should_run_with_cooldown "$last_baz" "$BAZARR_RETRY_COOLDOWN_SEC" "$now_ts"; then
      if [[ "$media_type" == "episode" ]]; then
        dl_http="$(try_bazarr_download_episode "$series_id" "$media_id" "$lang" "$([[ "$forced" -eq 1 ]] && echo true || echo false)" "$([[ "$hi" -eq 1 ]] && echo true || echo false)")"
      else
        dl_http="$(try_bazarr_download_movie "$media_id" "$lang" "$([[ "$forced" -eq 1 ]] && echo true || echo false)" "$([[ "$hi" -eq 1 ]] && echo true || echo false)")"
      fi
      bazarr_attempts=$((bazarr_attempts + 1))
      state_inc_bazarr_attempts "$media_type" "$media_id" "$lang" "$forced" "$hi"
      baz_attempts=$((baz_attempts + 1))
      state_set "$media_type" "$media_id" "$lang" "$forced" "$hi" "$now_ts" "$last_tr" "$last_arr" "bazarr_dl_http_$dl_http"
      log "BAZARR_TRY type=$media_type id=$media_id lang=$token http=$dl_http"
    fi

    if [[ "$media_type" == "episode" ]]; then
      missing_after="$(sqlite3 "$BAZARR_DB" "SELECT missing_subtitles FROM table_episodes WHERE sonarrEpisodeId=$media_id LIMIT 1;")"
      subtitles_raw="$(sqlite3 "$BAZARR_DB" "SELECT subtitles FROM table_episodes WHERE sonarrEpisodeId=$media_id LIMIT 1;")"
    else
      missing_after="$(sqlite3 "$BAZARR_DB" "SELECT missing_subtitles FROM table_movies WHERE radarrId=$media_id LIMIT 1;")"
      subtitles_raw="$(sqlite3 "$BAZARR_DB" "SELECT subtitles FROM table_movies WHERE radarrId=$media_id LIMIT 1;")"
    fi

    if ! has_missing_target "$missing_after" "$lang" "$forced" "$hi"; then
      state_set "$media_type" "$media_id" "$lang" "$forced" "$hi" "$now_ts" "$last_tr" "$last_arr" "resolved_after_bazarr"
      state_reset_bazarr_attempts "$media_type" "$media_id" "$lang" "$forced" "$hi"
      continue
    fi

    if [[ "$forced" -eq 0 && "$hi" -eq 0 && "$baz_attempts" -ge 5 ]] && should_run_with_cooldown "$last_tr" "$TRANSLATE_RETRY_COOLDOWN_SEC" "$now_ts"; then
      IFS=$'\t' read -r src_lang src_path <<<"$(pick_best_source_sub_for_target "$subtitles_raw" "$lang")"
      if [[ -n "${src_path:-}" && -f "$src_path" ]]; then
        tr_http="$(try_translate_to_lang "$media_type" "$media_id" "$src_path" "$lang")"
        translations=$((translations + 1))
        state_set "$media_type" "$media_id" "$lang" "$forced" "$hi" "$last_baz" "$now_ts" "$last_arr" "translate_http_$tr_http"
        log "TRANSLATE_TRY type=$media_type id=$media_id lang=$lang source=$src_lang http=$tr_http"

        if [[ "$media_type" == "episode" ]]; then
          missing_after="$(sqlite3 "$BAZARR_DB" "SELECT missing_subtitles FROM table_episodes WHERE sonarrEpisodeId=$media_id LIMIT 1;")"
        else
          missing_after="$(sqlite3 "$BAZARR_DB" "SELECT missing_subtitles FROM table_movies WHERE radarrId=$media_id LIMIT 1;")"
        fi
        if ! has_missing_target "$missing_after" "$lang" "$forced" "$hi"; then
          state_set "$media_type" "$media_id" "$lang" "$forced" "$hi" "$last_baz" "$now_ts" "$last_arr" "resolved_after_translate"
          state_reset_bazarr_attempts "$media_type" "$media_id" "$lang" "$forced" "$hi"
          continue
        fi
      fi
    fi

    if should_run_with_cooldown "$last_arr" "$ARR_RETRY_COOLDOWN_SEC" "$now_ts"; then
      if [[ "$media_type" == "episode" ]]; then
        arr_http="$(trigger_arr_search_episode "$media_id")"
      else
        arr_http="$(trigger_arr_search_movie "$media_id")"
      fi
      arr_triggers=$((arr_triggers + 1))
      state_set "$media_type" "$media_id" "$lang" "$forced" "$hi" "$last_baz" "$last_tr" "$now_ts" "arr_search_http_$arr_http"
      log "ARR_RETRY type=$media_type id=$media_id lang=$token http=$arr_http"
    fi
  done
}

load_active_arr_queue_ids
detect_bazarr_subtitle_jobs_busy
log "Runtime guards bazarr_sub_busy=$BAZARR_SUB_BUSY sonarr_active_count=$(wc -w <<<"$SONARR_ACTIVE_IDS") radarr_active_count=$(wc -w <<<"$RADARR_ACTIVE_IDS")"

while IFS='|' read -r episode_id series_id missing_raw subtitles_raw; do
  [[ -n "$episode_id" ]] || continue
  scanned=$((scanned + 1))
  if [[ "$MAX_ITEMS" -gt 0 && "$handled" -ge "$MAX_ITEMS" ]]; then
    break
  fi
  handled=$((handled + 1))
  process_item "episode" "$episode_id" "$series_id" "$missing_raw" "$subtitles_raw"
done < <(
  sqlite3 -separator '|' "$BAZARR_DB" "
    SELECT sonarrEpisodeId, sonarrSeriesId, missing_subtitles, subtitles
    FROM table_episodes
    WHERE missing_subtitles IS NOT NULL AND missing_subtitles <> '[]'
    ORDER BY sonarrEpisodeId;
  "
)

while IFS='|' read -r movie_id missing_raw subtitles_raw; do
  [[ -n "$movie_id" ]] || continue
  scanned=$((scanned + 1))
  if [[ "$MAX_ITEMS" -gt 0 && "$handled" -ge "$MAX_ITEMS" ]]; then
    break
  fi
  handled=$((handled + 1))
  process_item "movie" "$movie_id" "0" "$missing_raw" "$subtitles_raw"
done < <(
  sqlite3 -separator '|' "$BAZARR_DB" "
    SELECT radarrId, missing_subtitles, subtitles
    FROM table_movies
    WHERE missing_subtitles IS NOT NULL AND missing_subtitles <> '[]'
    ORDER BY radarrId;
  "
)

log "Done scanned=$scanned handled=$handled bazarr_attempts=$bazarr_attempts translations=$translations arr_triggers=$arr_triggers"
