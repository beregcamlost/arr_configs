#!/usr/bin/env bash
set -euo pipefail

SCRIPT_NAME="$(basename "$0")"
BAZARR_DB_DEFAULT="/opt/bazarr/data/db/bazarr.db"
STATE_DIR_DEFAULT="/APPBOX_DATA/storage/.transcode-state"
DB_DEFAULT="${STATE_DIR_DEFAULT}/library_codec_state.db"
LOG_DEFAULT="${STATE_DIR_DEFAULT}/manager.log"
BACKUP_DIR_DEFAULT="${STATE_DIR_DEFAULT}/backups"
TMP_DIR_DEFAULT="${STATE_DIR_DEFAULT}/work"
RETENTION_DAYS_DEFAULT=7

STATE_DIR="$STATE_DIR_DEFAULT"
DB_PATH="$DB_DEFAULT"
LOG_PATH="$LOG_DEFAULT"
BAZARR_DB="$BAZARR_DB_DEFAULT"
BACKUP_DIR="$BACKUP_DIR_DEFAULT"
TMP_DIR="$TMP_DIR_DEFAULT"
BATCH_SIZE=0
LIMIT=0
DRY_RUN=0
INCLUDE_SERIES=1
INCLUDE_MOVIES=1
LOG_LEVEL="info"
PATH_PREFIX=""

TARGET_VIDEO_CODEC="h264"
TARGET_AUDIO_CODEC="aac"
TARGET_PIX_FMT="yuv420p"
TARGET_SAMPLE_RATE=48000
TARGET_AUDIO_BITRATE="192k"
TARGET_CRF=19
TARGET_PRESET="medium"
IMPORT_FILE=""
IMPORT_MEDIA_TYPE=""
IMPORT_REF_ID=""
DISCORD_WEBHOOK_AUDIT_DONE="${DISCORD_WEBHOOK_AUDIT_DONE:-}"
DISCORD_WEBHOOK_STATUS="${DISCORD_WEBHOOK_STATUS:-$DISCORD_WEBHOOK_AUDIT_DONE}"
DEFAULT_TARGET_CONTAINER="mkv"
MAX_ATTEMPTS_DEFAULT=30
MAX_ATTEMPTS="$MAX_ATTEMPTS_DEFAULT"
RETENTION_DAYS="$RETENTION_DAYS_DEFAULT"

comma_fmt() {
  local n="$1"
  echo "$n" | sed ':a;s/\B[0-9]\{3\}\>$/,&/;ta'
}

progress_bar() {
  local current="$1" total="$2" width="${3:-16}" result=""
  [[ "$total" -le 0 ]] && { for ((i=0; i<width; i++)); do result+='\u2591'; done; printf "$result"; return; }
  local filled=$(( current * width / total ))
  [[ "$filled" -gt "$width" ]] && filled="$width"
  local empty=$(( width - filled ))
  for ((i=0; i<filled; i++)); do result+='\u2588'; done
  for ((i=0; i<empty; i++)); do result+='\u2591'; done
  printf "$result"
}

cleanup() {
  rm -f "$TMP_DIR"/*.json "$TMP_DIR"/*.tmp 2>/dev/null || true
}
trap cleanup EXIT

usage() {
  cat <<USAGE
Usage: $SCRIPT_NAME <command> [options]

Commands:
  audit           Probe files from Bazarr DB and store metadata in SQLite
  plan            Build conversion eligibility plan from latest audit data
  report          Emit summary report/csv from audit + plan tables
  daily-status    Send daily conversion status to Discord webhook
  convert         Convert planned eligible files (single-file sequential)
  resume          Alias for convert
  prune-backups   Remove backups older than retention window
  enqueue-import  Fast-path: probe one file and enqueue at highest priority

Options:
  --state-dir PATH      State root directory (default: $STATE_DIR_DEFAULT)
  --db PATH             SQLite state db path (default: state-dir/library_codec_state.db)
  --bazarr-db PATH      Bazarr db path (default: $BAZARR_DB_DEFAULT)
  --log PATH            Manager log path (default: state-dir/manager.log)
  --batch-size N        Max files to process in convert run (default: 0 = all)
  --limit N             Max files for audit/plan/report sampling (default: 0 = all)
  --dry-run             No media mutations; print planned actions
  --path-prefix PATH    Restrict audit source rows to paths under this prefix
  --include-series      Restrict scope to series only
  --include-movies      Restrict scope to movies only
  --max-attempts N      Max conversion attempts per media before skip (default: $MAX_ATTEMPTS_DEFAULT)
  --retention-days N    Backup retention window in days for prune-backups (default: $RETENTION_DAYS_DEFAULT)
  --log-level LEVEL     info|debug (default: info)
  --file PATH           Media file path (enqueue-import only)
  --media-type TYPE     series|movie (enqueue-import only)
  --ref-id ID           Bazarr ref ID — sonarrSeriesId or radarrId (enqueue-import only)
  --help                Show help
USAGE
}

log() {
  local level="$1"; shift
  local msg="$*"
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  if [[ "$LOG_LEVEL" == "debug" || "$level" != "debug" ]]; then
    printf '%s [%s] %s\n' "$ts" "$level" "$msg" | tee -a "$LOG_PATH" >/dev/null
  else
    printf '%s [%s] %s\n' "$ts" "$level" "$msg" >>"$LOG_PATH"
  fi
}

# Emby per-item refresh after codec conversion (self-contained, no lib dependency)
emby_refresh_item() {
  local file_path="$1"
  local emby_url="${EMBY_URL:-}" emby_key="${EMBY_API_KEY:-}"
  [[ -z "$emby_url" || -z "$emby_key" ]] && return 0
  local search_name item_id
  search_name="$(basename "$file_path" .mkv | sed 's/ - S[0-9]*E[0-9]*.*//' | sed 's/ ([0-9]*)$//')"
  item_id="$(curl -fsS "${emby_url}/Items?api_key=${emby_key}&SearchTerm=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$search_name'))")&Recursive=true&Limit=10" 2>/dev/null \
    | jq -r --arg path "$file_path" '[.Items[] | select(.Path == $path)] | .[0].Id // empty' 2>/dev/null)" || true
  if [[ -z "$item_id" ]]; then
    local parent_name
    parent_name="$(basename "$(dirname "$file_path")")"
    item_id="$(curl -fsS "${emby_url}/Items?api_key=${emby_key}&SearchTerm=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$parent_name'))")&Recursive=true&Limit=10" 2>/dev/null \
      | jq -r --arg path "$file_path" '[.Items[] | select(.Path == $path)] | .[0].Id // empty' 2>/dev/null)" || true
  fi
  [[ -n "$item_id" ]] && curl -fsS -X POST "${emby_url}/Items/${item_id}/Refresh?api_key=${emby_key}&Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default" >/dev/null 2>&1 || true
  return 0
}

# Trigger Sonarr RescanSeries or Radarr RescanMovie after file swap.
# Uses the codec DB media_type + bazarr_ref_id to determine which arr to call.
# $1=media_type  $2=bazarr_ref_id
arr_rescan_for_media() {
  local media_type="$1" ref_id="$2"
  [[ -z "$ref_id" || "$ref_id" == "NULL" ]] && return 0

  if [[ "$media_type" == "series" ]]; then
    local sonarr_url="${SONARR_URL:-http://127.0.0.1:8989/sonarr}"
    local sonarr_key="${SONARR_KEY:-}"
    [[ -z "$sonarr_key" ]] && return 0
    # Get sonarrSeriesId from episode ref
    local series_id
    series_id="$(sqlite3 -cmd ".timeout 5000" "$BAZARR_DB" \
      "SELECT sonarrSeriesId FROM table_episodes WHERE sonarrEpisodeId = $ref_id LIMIT 1;" 2>/dev/null)" || true
    [[ -z "$series_id" ]] && return 0
    curl -fsS -X POST "${sonarr_url}/api/v3/command" \
      -H "X-Api-Key: ${sonarr_key}" -H "Content-Type: application/json" \
      -d "{\"name\":\"RescanSeries\",\"seriesId\":${series_id}}" >/dev/null 2>&1 || true
    log "debug" "Sonarr RescanSeries id=$series_id triggered"
  else
    local radarr_url="${RADARR_URL:-http://127.0.0.1:7878/radarr}"
    local radarr_key="${RADARR_KEY:-}"
    [[ -z "$radarr_key" ]] && return 0
    # radarr_ref_id IS the radarrId
    curl -fsS -X POST "${radarr_url}/api/v3/command" \
      -H "X-Api-Key: ${radarr_key}" -H "Content-Type: application/json" \
      -d "{\"name\":\"RescanMovie\",\"movieId\":${ref_id}}" >/dev/null 2>&1 || true
    log "debug" "Radarr RescanMovie id=$ref_id triggered"
  fi
}

# Trigger Bazarr scan-disk for a media item after file swap.
# $1=media_type  $2=bazarr_ref_id
bazarr_rescan_for_media() {
  local media_type="$1" ref_id="$2"
  local bazarr_url="${BAZARR_URL:-http://127.0.0.1:6767/bazarr}"
  local bazarr_key="${BAZARR_API_KEY:-}"
  [[ -z "$bazarr_key" || -z "$ref_id" || "$ref_id" == "NULL" ]] && return 0

  local endpoint http_code
  if [[ "$media_type" == "series" ]]; then
    local series_id
    series_id="$(sqlite3 -cmd ".timeout 5000" "$BAZARR_DB" \
      "SELECT sonarrSeriesId FROM table_episodes WHERE sonarrEpisodeId = $ref_id LIMIT 1;" 2>/dev/null)" || true
    [[ -z "$series_id" ]] && return 0
    endpoint="${bazarr_url}/api/series?seriesid=${series_id}&action=scan-disk"
  else
    endpoint="${bazarr_url}/api/movies?radarrid=${ref_id}&action=scan-disk"
  fi

  http_code="$(curl -s -o /dev/null -w '%{http_code}' -X PATCH \
    -H "X-API-KEY: ${bazarr_key}" "$endpoint" 2>/dev/null)" || true
  log "debug" "Bazarr scan-disk type=$media_type ref=$ref_id http=$http_code"
}

# Map 3-letter ISO 639-2 code → English name (inverse of lang_name_to_iso).
iso_to_lang_name() {
  case "${1,,}" in
    eng) echo "English" ;; spa) echo "Spanish" ;; fre|fra) echo "French" ;;
    ger|deu) echo "German" ;; ita) echo "Italian" ;; por) echo "Portuguese" ;;
    zho|chi) echo "Chinese Simplified" ;; jpn) echo "Japanese" ;; kor) echo "Korean" ;;
    ara) echo "Arabic" ;; rus) echo "Russian" ;; nld) echo "Dutch" ;;
    swe) echo "Swedish" ;; dan) echo "Danish" ;; fin) echo "Finnish" ;;
    nor) echo "Norwegian" ;; pol) echo "Polish" ;; ces) echo "Czech" ;;
    hun) echo "Hungarian" ;; ron) echo "Romanian" ;; tur) echo "Turkish" ;;
    tha) echo "Thai" ;; vie) echo "Vietnamese" ;; ell) echo "Greek" ;;
    heb) echo "Hebrew" ;; hin) echo "Hindi" ;; ind) echo "Indonesian" ;;
    ukr) echo "Ukrainian" ;; bul) echo "Bulgarian" ;; hrv) echo "Croatian" ;;
    *) echo "" ;;
  esac
}

# Update Bazarr audio_language field to match actual audio tracks after conversion.
# Sonarr/Radarr API won't update their import-time language tags, so we write directly.
# $1=media_type  $2=bazarr_ref_id  $3=selected_audio_desc (comma-sep "idx:lang" pairs)
update_bazarr_audio_language() {
  local media_type="$1" ref_id="$2" selected_desc="$3"
  [[ -z "$ref_id" || "$ref_id" == "NULL" || ! -f "$BAZARR_DB" ]] && return 0

  # Build Python-style list: "['English', 'Spanish']"
  local -A seen_names=()
  local names_list="" name
  IFS=',' read -ra pairs <<< "$selected_desc"
  for pair in "${pairs[@]}"; do
    local lang="${pair#*:}"
    name="$(iso_to_lang_name "$lang")"
    [[ -z "$name" || -n "${seen_names[$name]:-}" ]] && continue
    seen_names["$name"]=1
    [[ -n "$names_list" ]] && names_list+=", "
    names_list+="'$name'"
  done
  [[ -z "$names_list" ]] && return 0
  local new_val="[$names_list]"

  if [[ "$media_type" == "series" ]]; then
    sqlite3 -cmd ".timeout 5000" "$BAZARR_DB" \
      "UPDATE table_episodes SET audio_language = '$new_val' WHERE sonarrEpisodeId = $ref_id;" 2>/dev/null || true
  else
    sqlite3 -cmd ".timeout 5000" "$BAZARR_DB" \
      "UPDATE table_movies SET audio_language = '$new_val' WHERE radarrId = $ref_id;" 2>/dev/null || true
  fi
  log "debug" "Bazarr audio_language updated type=$media_type ref=$ref_id val=$new_val"
}

notify_discord_audit_done() {
  local processed="$1"
  local probe_ok="$2"
  local missing="$3"
  local probe_fail="$4"
  local elapsed="$5"
  local skipped="${6:-0}"

  [[ -z "${DISCORD_WEBHOOK_AUDIT_DONE:-}" ]] && return 0

  # Query DB for conversion progress context
  local media_count swapped_total eligible_count audio_remaining video_remaining
  local swapped_7d compliant_count uhd_hdr_count
  media_count="$(db "SELECT COUNT(*) FROM media_files;" 2>/dev/null || echo 0)"
  swapped_total="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped';" 2>/dev/null || echo 0)"
  eligible_count="$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;" 2>/dev/null || echo 0)"
  audio_remaining="$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1 AND priority=1;" 2>/dev/null || echo 0)"
  video_remaining="$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1 AND priority=10;" 2>/dev/null || echo 0)"
  swapped_7d="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped' AND COALESCE(end_ts,start_ts) >= datetime('now','-7 days');" 2>/dev/null || echo 0)"
  compliant_count="$(db "SELECT COUNT(*) FROM media_files WHERE id NOT IN (SELECT media_id FROM conversion_plan WHERE eligible=1) AND id IN (SELECT media_id FROM audit_status WHERE probe_ok=1);" 2>/dev/null || echo 0)"
  uhd_hdr_count="$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=0 AND reason LIKE '%UHD%' OR reason LIKE '%HDR%' OR reason LIKE '%4K%';" 2>/dev/null || echo 0)"

  # Calculate progress, rate, ETA
  local total_convertible pct_10x pct_int pct_frac bar_str rate_str eta_str
  total_convertible=$((swapped_total + eligible_count))
  if [[ "$total_convertible" -gt 0 ]]; then
    pct_10x=$(( swapped_total * 1000 / total_convertible ))
  else
    pct_10x=0
  fi
  pct_int=$(( pct_10x / 10 ))
  pct_frac=$(( pct_10x % 10 ))
  bar_str="$(progress_bar "$swapped_total" "$total_convertible" 16)"

  if [[ "$swapped_7d" -gt 0 ]]; then
    local rate_per_day=$(( swapped_7d / 7 ))
    [[ "$rate_per_day" -lt 1 ]] && rate_per_day=1
    rate_str="~${rate_per_day}/day"
    if [[ "$eligible_count" -gt 0 ]]; then
      local eta_days=$(( eligible_count / rate_per_day ))
      eta_str="~${eta_days} days"
    else
      eta_str="done"
    fi
  else
    rate_str="n/a"
    eta_str="n/a"
  fi

  local unchanged=$(( media_count - processed + skipped ))
  [[ "$unchanged" -lt 0 ]] && unchanged=0

  local payload
  payload="$(jq -nc \
    --arg bar "$bar_str" \
    --arg pct "${pct_int}.${pct_frac}%" \
    --arg swapped "$(comma_fmt "$swapped_total")" \
    --arg total_c "$(comma_fmt "$total_convertible")" \
    --arg audio "$(comma_fmt "$audio_remaining")" \
    --arg video "$(comma_fmt "$video_remaining")" \
    --arg swap_total "$(comma_fmt "$swapped_total")" \
    --arg rate "$rate_str" \
    --arg eta "$eta_str" \
    --arg elapsed_s "${elapsed}s" \
    --arg probed "$(comma_fmt "$processed")" \
    --arg pfail "$probe_fail" \
    --arg unchanged_s "$(comma_fmt "$unchanged")" \
    --arg tracked "$(comma_fmt "$media_count")" \
    --arg compliant "$(comma_fmt "$compliant_count")" \
    --arg uhd "$(comma_fmt "$uhd_hdr_count")" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: "🔍 Codec Manager — Audit Complete",
      description: ($bar + "  " + $pct + "  ·  " + $swapped + " / " + $total_c + " converted"),
      color: 3066993,
      fields: [
        {name: "🔊 Audio-only left", value: $audio, inline: true},
        {name: "🎬 Video left", value: $video, inline: true},
        {name: "✅ Swapped (total)", value: $swap_total, inline: true},
        {name: "📈 Rate", value: $rate, inline: true},
        {name: "⏳ ETA", value: $eta, inline: true},
        {name: "⏱ Elapsed", value: $elapsed_s, inline: true},
        {name: "🔍 Probed", value: ($probed + " (fail: " + $pfail + ")"), inline: true},
        {name: "⬜ Unchanged", value: $unchanged_s, inline: true}
      ],
      footer: {text: ($tracked + " tracked · " + $compliant + " compliant · " + $uhd + " UHD/HDR skipped")},
      timestamp: $ts
    }]}')"

  local resp_file err_file http_code resp_snippet err_snippet curl_rc
  resp_file="$(mktemp)"
  err_file="$(mktemp)"
  if http_code="$(curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors -o "$resp_file" -w '%{http_code}' -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_AUDIT_DONE" 2>"$err_file")"; then
    curl_rc=0
  else
    curl_rc=$?
  fi
  if [[ "$http_code" == "200" || "$http_code" == "204" ]]; then
    log "info" "Audit completion notification sent (http_code=$http_code)"
  else
    resp_snippet="$(tr -d '\n' <"$resp_file" | cut -c1-300)"
    err_snippet="$(tr -d '\n' <"$err_file" | cut -c1-300)"
    log "warn" "Audit completion notification failed (curl_rc=${curl_rc:-none} http_code=${http_code:-none} error=${err_snippet:-none} response=${resp_snippet:-empty})"
    case "${curl_rc:-}" in
      6)
        log "warn" "Notifier hint: DNS resolution failed for webhook host. Check resolver/network from this runtime."
        ;;
      7)
        log "warn" "Notifier hint: TCP connect failed to webhook host. Check outbound firewall/routing."
        ;;
      28)
        log "warn" "Notifier hint: Network timeout while reaching webhook host."
        ;;
      35|60)
        log "warn" "Notifier hint: TLS/SSL handshake or certificate failure."
        ;;
    esac
  fi
  rm -f "$resp_file"
  rm -f "$err_file"
}

notify_discord_daily_status() {
  [[ -z "${DISCORD_WEBHOOK_STATUS:-}" ]] && return 0

  local media_count eligible_count audio_only_count video_tx_count swapped_total failed_total running_now attempt_limited_total
  local swapped_24h failed_24h recovered_24h attempt_limited_24h last_run
  media_count="$(db "SELECT COUNT(*) FROM media_files;")"
  eligible_count="$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;")"
  audio_only_count="$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1 AND priority=1;")"
  video_tx_count="$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1 AND priority=10;")"
  swapped_total="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped';")"
  failed_total="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='failed';")"
  attempt_limited_total="$(db "SELECT COUNT(DISTINCT media_id) FROM conversion_runs WHERE status='attempt_limit_reached';")"
  running_now="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='running' AND end_ts IS NULL;")"
  swapped_24h="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  failed_24h="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='failed' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  attempt_limited_24h="$(db "SELECT COUNT(DISTINCT media_id) FROM conversion_runs WHERE status='attempt_limit_reached' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  recovered_24h="$(db "SELECT COUNT(*) FROM conversion_runs WHERE status='failed' AND error='stale_running_recovered' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  last_run="$(db "SELECT COALESCE((SELECT run_id || ' [' || status || '] @ ' || COALESCE(end_ts,start_ts) FROM conversion_runs ORDER BY id DESC LIMIT 1),'none');")"

  local payload
  payload="$(jq -nc \
    --arg db "$DB_PATH" \
    --arg m "$media_count" \
    --arg e "$eligible_count" \
    --arg ao "$audio_only_count" \
    --arg vt "$video_tx_count" \
    --arg st "$swapped_total" \
    --arg ft "$failed_total" \
    --arg at "$attempt_limited_total" \
    --arg r "$running_now" \
    --arg s24 "$swapped_24h" \
    --arg f24 "$failed_24h" \
    --arg a24 "$attempt_limited_24h" \
    --arg rec24 "$recovered_24h" \
    --arg lr "$last_run" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: "📊 Codec Manager — Daily Status",
      description: ("🗃️ **" + $m + "** tracked · 📋 **" + $e + "** eligible · 🔄 **" + $r + "** running"),
      color: 3447003,
      fields: [
        {name: "🔊 Audio-only", value: $ao, inline: true},
        {name: "🎬 Video",      value: $vt, inline: true},
        {name: "🔄 Running",    value: $r,  inline: true},
        {name: "✅ Swapped",    value: ($st + " (24h: " + $s24 + ")"), inline: true},
        {name: "❌ Failed",     value: ($ft + " (24h: " + $f24 + ")"), inline: true},
        {name: "⚠️ Attempt Ltd", value: ($at + " (24h: " + $a24 + ")"), inline: true},
        {name: "🔧 Recovered (24h)", value: $rec24, inline: true},
        {name: "📌 Last Run",   value: ("`" + $lr + "`"), inline: false}
      ],
      footer: {text: ("State DB: " + $db)},
      timestamp: $ts
    }]}')"

  local resp_file err_file http_code resp_snippet err_snippet curl_rc
  resp_file="$(mktemp)"
  err_file="$(mktemp)"
  if http_code="$(curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors -o "$resp_file" -w '%{http_code}' -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_STATUS" 2>"$err_file")"; then
    curl_rc=0
  else
    curl_rc=$?
  fi
  if [[ "$http_code" == "200" || "$http_code" == "204" ]]; then
    log "info" "Daily status notification sent (http_code=$http_code)"
  else
    resp_snippet="$(tr -d '\n' <"$resp_file" | cut -c1-300)"
    err_snippet="$(tr -d '\n' <"$err_file" | cut -c1-300)"
    log "warn" "Daily status notification failed (curl_rc=${curl_rc:-none} http_code=${http_code:-none} error=${err_snippet:-none} response=${resp_snippet:-empty})"
  fi
  rm -f "$resp_file"
  rm -f "$err_file"
}

notify_discord_attempt_limit() {
  local media_id="$1"
  local path="$2"
  local attempts="$3"
  local max_attempts="$4"
  [[ -z "${DISCORD_WEBHOOK_STATUS:-}" ]] && return 0

  local payload
  payload="$(jq -nc \
    --arg media_id "$media_id" \
    --arg path "$path" \
    --arg attempts "$attempts" \
    --arg max "$max_attempts" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: "⚠️ Codec Manager — Attempt Limit Reached",
      description: "Skipped for now — other files continue processing.",
      color: 15105570,
      fields: [
        {name: "🆔 Media ID",  value: $media_id, inline: true},
        {name: "🔄 Attempts",  value: ($attempts + " / " + $max), inline: true},
        {name: "📁 File",      value: ("`" + $path + "`"), inline: false}
      ],
      footer: {text: "Codec Manager"},
      timestamp: $ts
    }]}')"

  curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors \
    -H 'Content-Type: application/json' -d "$payload" "$DISCORD_WEBHOOK_STATUS" >/dev/null 2>&1 || true
}

die() {
  log "error" "$*"
  exit 1
}

require_cmds() {
  local missing=()
  for cmd in sqlite3 ffprobe ffmpeg jq curl stat awk sed grep find sha256sum flock; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "Missing required commands: ${missing[*]}"
  fi
}

acquire_convert_lock() {
  local lock_file="$STATE_DIR/convert.lock"
  exec 9>"$lock_file"
  if ! flock -n 9; then
    log "info" "Convert already running (lock held: $lock_file); skipping this invocation"
    return 1
  fi
  return 0
}

parse_args() {
  COMMAND="${1:-}"
  if [[ -z "$COMMAND" || "$COMMAND" == "--help" || "$COMMAND" == "-h" ]]; then
    usage
    exit 0
  fi
  shift || true

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --state-dir)
        STATE_DIR="$2"; shift 2 ;;
      --db)
        DB_PATH="$2"; shift 2 ;;
      --bazarr-db)
        BAZARR_DB="$2"; shift 2 ;;
      --log)
        LOG_PATH="$2"; shift 2 ;;
      --batch-size)
        BATCH_SIZE="$2"; shift 2 ;;
      --limit)
        LIMIT="$2"; shift 2 ;;
      --dry-run)
        DRY_RUN=1; shift ;;
      --path-prefix)
        PATH_PREFIX="$2"; shift 2 ;;
      --include-series)
        INCLUDE_SERIES=1; INCLUDE_MOVIES=0; shift ;;
      --include-movies)
        INCLUDE_MOVIES=1; INCLUDE_SERIES=0; shift ;;
      --max-attempts)
        MAX_ATTEMPTS="$2"; shift 2 ;;
      --retention-days)
        RETENTION_DAYS="$2"; shift 2 ;;
      --log-level)
        LOG_LEVEL="$2"; shift 2 ;;
      --file)
        IMPORT_FILE="$2"; shift 2 ;;
      --media-type)
        IMPORT_MEDIA_TYPE="$2"; shift 2 ;;
      --ref-id)
        IMPORT_REF_ID="$2"; shift 2 ;;
      --help|-h)
        usage; exit 0 ;;
      *)
        die "Unknown option: $1"
        ;;
    esac
  done

  if [[ "$DB_PATH" == "$DB_DEFAULT" ]]; then
    DB_PATH="${STATE_DIR}/library_codec_state.db"
  fi
  if [[ "$LOG_PATH" == "$LOG_DEFAULT" ]]; then
    LOG_PATH="${STATE_DIR}/manager.log"
  fi
  BACKUP_DIR="${STATE_DIR}/backups"
  TMP_DIR="${STATE_DIR}/work"
}

ensure_state_dirs() {
  mkdir -p "$STATE_DIR" "$BACKUP_DIR" "$TMP_DIR"
  touch "$LOG_PATH"
}

sql_quote() {
  printf "%s" "$1" | sed "s/'/''/g"
}

STREAMING_STATE_DB="/APPBOX_DATA/storage/.streaming-checker-state/streaming_state.db"

# --- Shared helpers for path-set loading and lookup ---

# Load paths from streaming DB into a nameref associative array.
_load_candidate_paths() {
  local -n _arr="$1"
  local sql="$2"
  _arr=()
  [[ -f "$STREAMING_STATE_DB" ]] || return 0
  while IFS= read -r spath; do
    [[ -n "$spath" ]] && _arr["$spath"]=1
  done < <(sqlite3 -cmd ".timeout 5000" "$STREAMING_STATE_DB" "$sql" 2>/dev/null)
}

# Resolve a media file path to its series/movie directory (pure bash, no subprocess).
_resolve_match_dir() {
  local filepath="$1"
  if [[ "$filepath" == *"/tv/"* || "$filepath" == *"/tvanimated/"* ]]; then
    if [[ "$filepath" == *"/Season "* ]]; then
      printf '%s' "${filepath%%/Season [0-9]*}"
    else
      printf '%s' "${filepath%/*}"
    fi
  else
    printf '%s' "${filepath%/*}"
  fi
}

# Check if a filepath matches any path in a nameref associative array.
_check_path_in_set() {
  local -n _ref_array="$1"
  local filepath="$2"
  [[ ${#_ref_array[@]} -eq 0 ]] && return 1
  local match_dir
  match_dir="$(_resolve_match_dir "$filepath")"
  [[ -n "${_ref_array[$match_dir]:-}" ]]
}

# Pre-loaded streaming candidate paths (populated by load_streaming_candidates)
declare -A _STREAMING_PATHS=()

load_streaming_candidates() {
  _load_candidate_paths _STREAMING_PATHS \
    "SELECT path FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL;"
}

# Pure bash lookup — safe inside pipelines (no subprocess spawning)
is_streaming_candidate_inline() { _check_path_in_set _STREAMING_PATHS "$1"; }

# --- Stale candidate exclusion (tier 1.5) ---
declare -A _STALE_PATHS=()

load_stale_candidates() {
  _load_candidate_paths _STALE_PATHS \
    "SELECT DISTINCT path FROM streaming_status WHERE stale_flagged_at IS NOT NULL AND deleted_at IS NULL AND path IS NOT NULL;"
}

is_stale_candidate_inline() { _check_path_in_set _STALE_PATHS "$1"; }

# SQLite wrapper — ensures busy_timeout (30s) on every connection.
# Accepts sqlite3 flags before the query (e.g., db -separator $'\t' "SQL").
db() {
  local flags=()
  while [[ "${1:-}" == -* ]]; do
    flags+=("$1" "$2"); shift 2
  done
  sqlite3 "${flags[@]}" -cmd ".timeout 30000" "$DB_PATH" "$@"
}

# --- Inline language helpers (mirrors of lib_subtitle_common.sh — keep in sync) ---

# Expand comma-separated lang codes to space-separated set with 2+3-letter variants.
# Input: "en,es" or "eng,spa"   Output: "en eng es spa "
expand_lang_codes_inline() {
  local input="$1"
  local -A seen=()
  local result=""
  IFS=',' read -ra codes <<< "$input"
  for code in "${codes[@]}"; do
    code="${code,,}"; code="${code// /}"
    [[ -z "$code" || -n "${seen[$code]:-}" ]] && continue
    seen["$code"]=1; result+="$code "
    case "$code" in
      en)  [[ -z "${seen[eng]:-}" ]] && { seen[eng]=1; result+="eng "; } ;;
      eng) [[ -z "${seen[en]:-}" ]]  && { seen[en]=1;  result+="en "; }  ;;
      es)  [[ -z "${seen[spa]:-}" ]] && { seen[spa]=1; result+="spa "; } ;;
      spa) [[ -z "${seen[es]:-}" ]]  && { seen[es]=1;  result+="es "; }  ;;
      fr)  [[ -z "${seen[fre]:-}" ]] && { seen[fre]=1; result+="fre "; } ;;
      fre|fra) [[ -z "${seen[fr]:-}" ]] && { seen[fr]=1; result+="fr "; }
               [[ "$code" == "fre" && -z "${seen[fra]:-}" ]] && { seen[fra]=1; result+="fra "; }
               [[ "$code" == "fra" && -z "${seen[fre]:-}" ]] && { seen[fre]=1; result+="fre "; } ;;
      pt)  [[ -z "${seen[por]:-}" ]] && { seen[por]=1; result+="por "; } ;;
      por) [[ -z "${seen[pt]:-}" ]]  && { seen[pt]=1;  result+="pt "; }  ;;
      de)  [[ -z "${seen[ger]:-}" ]] && { seen[ger]=1; result+="ger "; } ;;
      ger|deu) [[ -z "${seen[de]:-}" ]] && { seen[de]=1; result+="de "; }
               [[ "$code" == "ger" && -z "${seen[deu]:-}" ]] && { seen[deu]=1; result+="deu "; }
               [[ "$code" == "deu" && -z "${seen[ger]:-}" ]] && { seen[ger]=1; result+="ger "; } ;;
      it)  [[ -z "${seen[ita]:-}" ]] && { seen[ita]=1; result+="ita "; } ;;
      ita) [[ -z "${seen[it]:-}" ]]  && { seen[it]=1;  result+="it "; }  ;;
      zh)  [[ -z "${seen[zho]:-}" ]] && { seen[zho]=1; result+="zho chi "; seen[chi]=1; } ;;
      zho|chi) [[ -z "${seen[zh]:-}" ]] && { seen[zh]=1; result+="zh "; } ;;
      ja)  [[ -z "${seen[jpn]:-}" ]] && { seen[jpn]=1; result+="jpn "; } ;;
      jpn) [[ -z "${seen[ja]:-}" ]]  && { seen[ja]=1;  result+="ja "; }  ;;
      ko)  [[ -z "${seen[kor]:-}" ]] && { seen[kor]=1; result+="kor "; } ;;
      kor) [[ -z "${seen[ko]:-}" ]]  && { seen[ko]=1;  result+="ko "; }  ;;
    esac
  done
  echo "$result"
}

# Check if a language code is in an expanded set (space-separated).
lang_in_set_inline() {
  local lang="$1" set="$2"
  [[ " $set " == *" ${lang,,} "* ]]
}

# Map English language name → 3-letter ISO 639-2 code (for Bazarr audio_language parsing).
lang_name_to_iso() {
  local name="$1"
  case "${name,,}" in
    english)              echo "eng" ;;
    spanish)              echo "spa" ;;
    french)               echo "fre" ;;
    german)               echo "ger" ;;
    italian)              echo "ita" ;;
    portuguese)           echo "por" ;;
    chinese*|mandarin)    echo "zho" ;;
    japanese)             echo "jpn" ;;
    korean)               echo "kor" ;;
    arabic)               echo "ara" ;;
    russian)              echo "rus" ;;
    dutch)                echo "nld" ;;
    swedish)              echo "swe" ;;
    danish)               echo "dan" ;;
    finnish)              echo "fin" ;;
    norwegian)            echo "nor" ;;
    polish)               echo "pol" ;;
    czech)                echo "ces" ;;
    hungarian)            echo "hun" ;;
    romanian)             echo "ron" ;;
    turkish)              echo "tur" ;;
    thai)                 echo "tha" ;;
    vietnamese)           echo "vie" ;;
    greek)                echo "ell" ;;
    hebrew)               echo "heb" ;;
    hindi)                echo "hin" ;;
    indonesian)           echo "ind" ;;
    ukrainian)            echo "ukr" ;;
    bulgarian)            echo "bul" ;;
    croatian)             echo "hrv" ;;
    "chinese simplified") echo "zho" ;;
    *)                    echo "" ;;
  esac
}

# --- Bazarr profile / original language resolvers ---

# Resolve Bazarr language profile for a media file by its codec DB ref ID.
# Returns comma-separated 2-letter codes (e.g. "en,es") or empty string.
# $1=media_type ("series"|"movie")  $2=bazarr_ref_id  $3=bazarr_db_path
resolve_profile_langs_by_id() {
  local media_type="$1" ref_id="$2" bazarr_db="$3"
  [[ -z "$ref_id" || "$ref_id" == "NULL" || ! -f "$bazarr_db" ]] && return 0

  local items=""
  if [[ "$media_type" == "series" ]]; then
    items="$(sqlite3 -cmd ".timeout 5000" "$bazarr_db" "
      SELECT lp.items FROM table_episodes e
      JOIN table_shows s ON s.sonarrSeriesId = e.sonarrSeriesId
      JOIN table_languages_profiles lp ON lp.profileId = s.profileId
      WHERE e.sonarrEpisodeId = $ref_id LIMIT 1;
    " 2>/dev/null)" || true
  else
    items="$(sqlite3 -cmd ".timeout 5000" "$bazarr_db" "
      SELECT lp.items FROM table_movies m
      JOIN table_languages_profiles lp ON lp.profileId = m.profileId
      WHERE m.radarrId = $ref_id LIMIT 1;
    " 2>/dev/null)" || true
  fi

  [[ -z "$items" ]] && return 0
  # Parse JSON array: extract "language" values → comma-separated
  jq -r '.[].language' <<< "$items" 2>/dev/null | paste -sd ',' -
}

# Resolve the TRUE original language of a media item from Bazarr metadata.
# For series: uses the most common audio_language across all episodes of the same show
#   (avoids per-episode variance from dual-audio releases).
# For movies: uses table_movies.audio_language directly.
# Returns space-separated 3-letter ISO codes (e.g. "eng") or empty string.
# $1=media_type  $2=bazarr_ref_id  $3=bazarr_db_path
resolve_original_lang_by_id() {
  local media_type="$1" ref_id="$2" bazarr_db="$3"
  [[ -z "$ref_id" || "$ref_id" == "NULL" || ! -f "$bazarr_db" ]] && return 0

  local raw_lang=""
  if [[ "$media_type" == "series" ]]; then
    # Use the most common audio_language across all episodes of this show
    # to avoid outlier multi-language releases (e.g. French+English in a mostly-English show).
    raw_lang="$(sqlite3 -cmd ".timeout 5000" "$bazarr_db" "
      SELECT e2.audio_language
      FROM table_episodes e2
      WHERE e2.sonarrSeriesId = (
        SELECT sonarrSeriesId FROM table_episodes WHERE sonarrEpisodeId = $ref_id LIMIT 1
      )
      AND e2.audio_language IS NOT NULL AND e2.audio_language <> '[]'
      GROUP BY e2.audio_language ORDER BY COUNT(*) DESC LIMIT 1;
    " 2>/dev/null)" || true
  else
    raw_lang="$(sqlite3 -cmd ".timeout 5000" "$bazarr_db" "
      SELECT audio_language FROM table_movies WHERE radarrId = $ref_id LIMIT 1;
    " 2>/dev/null)" || true
  fi

  [[ -z "$raw_lang" || "$raw_lang" == "[]" ]] && return 0

  # Parse Python-style list: "['English']" or "['French', 'English']"
  # Take only the FIRST element — the primary/original language.
  local first_name
  first_name="$(echo "$raw_lang" | sed "s/^\[//;s/\]$//;s/'//g" | cut -d',' -f1 | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [[ -z "$first_name" ]] && return 0

  local iso
  iso="$(lang_name_to_iso "$first_name")"
  [[ -n "$iso" ]] && echo "$iso"
}

init_db() {
  db <<'SQL' >/dev/null
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=30000;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS media_files (
  id INTEGER PRIMARY KEY,
  media_type TEXT NOT NULL,
  bazarr_ref_id INTEGER,
  path TEXT NOT NULL UNIQUE,
  size_bytes INTEGER,
  mtime INTEGER,
  container TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS probe_streams (
  id INTEGER PRIMARY KEY,
  media_id INTEGER NOT NULL,
  stream_index INTEGER,
  stream_type TEXT,
  codec TEXT,
  profile TEXT,
  pix_fmt TEXT,
  width INTEGER,
  height INTEGER,
  fps REAL,
  channels INTEGER,
  sample_rate INTEGER,
  language TEXT,
  forced INTEGER DEFAULT 0,
  default_flag INTEGER DEFAULT 0,
  is_hdr INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(media_id) REFERENCES media_files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_probe_streams_media_id ON probe_streams(media_id);
CREATE INDEX IF NOT EXISTS idx_probe_streams_type ON probe_streams(stream_type);

CREATE TABLE IF NOT EXISTS audit_status (
  id INTEGER PRIMARY KEY,
  media_id INTEGER NOT NULL UNIQUE,
  audit_ts TEXT,
  exists_flag INTEGER DEFAULT 0,
  probe_ok INTEGER DEFAULT 0,
  probe_error TEXT,
  FOREIGN KEY(media_id) REFERENCES media_files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversion_plan (
  id INTEGER PRIMARY KEY,
  media_id INTEGER NOT NULL UNIQUE,
  plan_ts TEXT,
  eligible INTEGER DEFAULT 0,
  priority INTEGER DEFAULT 10,
  reason TEXT,
  target_video TEXT,
  target_audio TEXT,
  target_container TEXT,
  skip_reason TEXT,
  FOREIGN KEY(media_id) REFERENCES media_files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversion_runs (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  media_id INTEGER NOT NULL,
  start_ts TEXT,
  end_ts TEXT,
  status TEXT,
  attempt INTEGER DEFAULT 1,
  error TEXT,
  FOREIGN KEY(media_id) REFERENCES media_files(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conversion_runs_run_id ON conversion_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_conversion_runs_media_id ON conversion_runs(media_id);

CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY,
  media_id INTEGER NOT NULL,
  new_path TEXT,
  backup_path TEXT,
  verify_hash_old TEXT,
  verify_hash_new TEXT,
  verify_probe_ok INTEGER DEFAULT 0,
  swapped_ts TEXT,
  FOREIGN KEY(media_id) REFERENCES media_files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  event_ts TEXT DEFAULT CURRENT_TIMESTAMP,
  severity TEXT,
  scope TEXT,
  media_id INTEGER,
  message TEXT,
  context_json TEXT
);
SQL

  # Migration: add priority column if missing (existing DBs)
  local has_priority
  has_priority="$(db "SELECT COUNT(*) FROM pragma_table_info('conversion_plan') WHERE name='priority';")"
  if [[ "$has_priority" -eq 0 ]]; then
    db "ALTER TABLE conversion_plan ADD COLUMN priority INTEGER DEFAULT 10;"
    log "info" "Migrated conversion_plan: added priority column"
  fi
}

insert_event() {
  local sev="$1" scope="$2" media_id="$3" msg="$4" ctx="$5"
  local msg_q ctx_q
  msg_q="$(sql_quote "$msg")"
  ctx_q="$(sql_quote "$ctx")"
  db "INSERT INTO events(severity,scope,media_id,message,context_json) VALUES('$(sql_quote "$sev")','$(sql_quote "$scope")',${media_id:-NULL},'$msg_q','$ctx_q');"
}

fetch_sources_query() {
  local limit_clause=""
  if [[ "$LIMIT" -gt 0 ]]; then
    limit_clause=" LIMIT $LIMIT"
  fi

  local where_movies="path IS NOT NULL AND path <> ''"
  local where_series="path IS NOT NULL AND path <> ''"
  if [[ -n "$PATH_PREFIX" ]]; then
    local qprefix
    qprefix="$(sql_quote "$PATH_PREFIX")"
    where_movies+=" AND path LIKE '${qprefix}%'"
    where_series+=" AND path LIKE '${qprefix}%'"
  fi

  local parts=()
  if [[ "$INCLUDE_SERIES" -eq 1 ]]; then
    parts+=("SELECT 'series' AS media_type, sonarrEpisodeId AS ref_id, path FROM table_episodes WHERE ${where_series}")
  fi
  if [[ "$INCLUDE_MOVIES" -eq 1 ]]; then
    parts+=("SELECT 'movie' AS media_type, radarrId AS ref_id, path FROM table_movies WHERE ${where_movies}")
  fi

  if [[ ${#parts[@]} -eq 0 ]]; then
    die "Scope excludes both series and movies"
  fi

  local query
  query="${parts[0]}"
  if [[ ${#parts[@]} -gt 1 ]]; then
    query+=" UNION ALL ${parts[1]}"
  fi
  query+=" ORDER BY path${limit_clause};"
  printf '%s' "$query"
}

upsert_media_file() {
  local media_type="$1" ref_id="$2" path="$3" size_bytes="$4" mtime="$5" container="$6"
  local p qtype qpath qcont
  qtype="$(sql_quote "$media_type")"
  qpath="$(sql_quote "$path")"
  qcont="$(sql_quote "$container")"

  db <<SQL
INSERT INTO media_files(media_type,bazarr_ref_id,path,size_bytes,mtime,container,updated_at)
VALUES('$qtype',${ref_id:-NULL},'$qpath',${size_bytes:-NULL},${mtime:-NULL},'$qcont',CURRENT_TIMESTAMP)
ON CONFLICT(path) DO UPDATE SET
  media_type=excluded.media_type,
  bazarr_ref_id=excluded.bazarr_ref_id,
  size_bytes=excluded.size_bytes,
  mtime=excluded.mtime,
  container=excluded.container,
  updated_at=CURRENT_TIMESTAMP;
SQL
}

media_id_for_path() {
  local path="$1"
  local qpath
  qpath="$(sql_quote "$path")"
  db "SELECT id FROM media_files WHERE path='$qpath' LIMIT 1;"
}

upsert_audit_status() {
  local media_id="$1" exists_flag="$2" probe_ok="$3" probe_error="$4"
  local qerr
  qerr="$(sql_quote "$probe_error")"
  db <<SQL
INSERT INTO audit_status(media_id,audit_ts,exists_flag,probe_ok,probe_error)
VALUES($media_id,CURRENT_TIMESTAMP,$exists_flag,$probe_ok,'$qerr')
ON CONFLICT(media_id) DO UPDATE SET
  audit_ts=CURRENT_TIMESTAMP,
  exists_flag=excluded.exists_flag,
  probe_ok=excluded.probe_ok,
  probe_error=excluded.probe_error;
SQL
}

clear_probe_streams() {
  local media_id="$1"
  db "DELETE FROM probe_streams WHERE media_id=$media_id;"
}

fps_to_float() {
  local fps_raw="$1"
  if [[ -z "$fps_raw" || "$fps_raw" == "0/0" ]]; then
    printf '0'
    return
  fi
  awk -v v="$fps_raw" 'BEGIN{split(v,a,"/"); if (a[2]==0||a[2]=="") print 0; else printf "%.6f", a[1]/a[2]}'
}

insert_probe_streams_from_json() {
  local media_id="$1" json_file="$2"

  # Single jq call extracts all stream fields — eliminates ~15 jq forks per stream.
  # HDR detection and fps calculation are done inside jq to avoid any extra subprocess calls.
  # Uses pipe-delimited output (not @tsv) because bash read with IFS=$'\t' collapses
  # consecutive tabs, dropping empty fields like pix_fmt on audio/subtitle streams.
  # All INSERTs are batched into a single transaction (one sqlite3 subprocess).
  local sql_batch="BEGIN TRANSACTION;"
  while IFS='|' read -r idx stype codec profile pix_fmt width height fps channels sample_rate lang forced dflag hdr; do
    [[ -z "$idx" ]] && continue
    sql_batch+="
INSERT INTO probe_streams(media_id,stream_index,stream_type,codec,profile,pix_fmt,width,height,fps,channels,sample_rate,language,forced,default_flag,is_hdr)
VALUES(
  $media_id,
  ${idx:-0},
  '$(sql_quote "${stype:-}")',
  '$(sql_quote "${codec:-}")',
  '$(sql_quote "${profile:-}")',
  '$(sql_quote "${pix_fmt:-}")',
  ${width:-0},
  ${height:-0},
  ${fps:-0},
  ${channels:-0},
  ${sample_rate:-0},
  '$(sql_quote "${lang:-}")',
  ${forced:-0},
  ${dflag:-0},
  ${hdr:-0}
);"
  done < <(jq -r '.streams[]? | [
    (.index // 0),
    ((.codec_type // "") | ascii_downcase),
    ((.codec_name // "") | ascii_downcase),
    (.profile // ""),
    (.pix_fmt // ""),
    (.width // 0),
    (.height // 0),
    ((.avg_frame_rate // "0/0") | split("/") |
      if length == 2 and (.[1] | tonumber? // 0) > 0
      then ((.[0] | tonumber) / (.[1] | tonumber) * 1000000 | floor) / 1000000
      else 0
      end),
    (.channels // 0),
    (.sample_rate // 0),
    ((.tags.language // "") | ascii_downcase),
    (.disposition.forced // 0),
    (.disposition.default // 0),
    (
      ((.color_transfer // "") | ascii_downcase) as $ct |
      ((.color_primaries // "") | ascii_downcase) as $cp |
      if $ct == "smpte2084" or $ct == "arib-std-b67" or $cp == "bt2020" then 1 else 0 end
    )
  ] | join("|")' "$json_file")
  sql_batch+="
COMMIT;"
  db <<<"$sql_batch"
}

audit_cmd() {
  [[ -f "$BAZARR_DB" ]] || die "Bazarr DB not found: $BAZARR_DB"
  log "info" "Starting audit using Bazarr DB: $BAZARR_DB"
  local start_ts end_ts elapsed
  start_ts="$(date +%s)"

  local query
  query="$(fetch_sources_query)"

  local total=0 ok=0 missing=0 probe_fail=0 skipped=0

  local sources_tmp
  sources_tmp="$(mktemp)"
  if ! sqlite3 -separator $'\t' "$BAZARR_DB" "$query" >"$sources_tmp"; then
    rm -f "$sources_tmp"
    die "Failed to query Bazarr DB for audit sources"
  fi

  while IFS=$'\t' read -r media_type ref_id path; do
    total=$((total + 1))
    [[ -z "$path" ]] && continue

    local container size_bytes mtime media_id
    container="${path##*.}"
    container="$(printf '%s' "$container" | tr '[:upper:]' '[:lower:]')"

    if [[ -f "$path" ]]; then
      size_bytes="$(stat -c '%s' "$path" 2>/dev/null || echo 0)"
      mtime="$(stat -c '%Y' "$path" 2>/dev/null || echo 0)"
    else
      size_bytes=0
      mtime=0
    fi

    upsert_media_file "$media_type" "$ref_id" "$path" "$size_bytes" "$mtime" "$container"
    media_id="$(media_id_for_path "$path")"

    if [[ ! -f "$path" ]]; then
      missing=$((missing + 1))
      clear_probe_streams "$media_id"
      upsert_audit_status "$media_id" 0 0 "missing_file"
      insert_event "warn" "audit" "$media_id" "Missing file" "{\"path\":\"$(sql_quote "$path")\"}"
      continue
    fi

    # Incremental: skip ffprobe if mtime unchanged and probe data already exists
    local stored_mtime has_probes
    stored_mtime="$(db "SELECT mtime FROM media_files WHERE id=$media_id LIMIT 1;" 2>/dev/null)"
    if [[ "$stored_mtime" == "$mtime" && -n "$stored_mtime" ]]; then
      has_probes="$(db "SELECT 1 FROM probe_streams WHERE media_id=$media_id LIMIT 1;" 2>/dev/null)"
      if [[ "$has_probes" == "1" ]]; then
        skipped=$((skipped + 1))
        ok=$((ok + 1))
        if (( total % 500 == 0 )); then
          log "info" "Audit progress: $total processed ($skipped unchanged)"
        fi
        continue
      fi
    fi

    local tmp_json tmp_err
    tmp_json="$(mktemp)"
    tmp_err="$(mktemp)"
    if ffprobe -v error -print_format json -show_streams -show_format "$path" >"$tmp_json" 2>"$tmp_err"; then
      clear_probe_streams "$media_id"
      insert_probe_streams_from_json "$media_id" "$tmp_json"
      upsert_audit_status "$media_id" 1 1 ""
      ok=$((ok + 1))
    else
      local err
      err="$(tr -d '\n' <"$tmp_err" | cut -c1-4000)"
      clear_probe_streams "$media_id"
      upsert_audit_status "$media_id" 1 0 "$err"
      insert_event "error" "audit" "$media_id" "ffprobe failed" "{\"error\":\"$(sql_quote "$err")\"}"
      probe_fail=$((probe_fail + 1))
    fi
    rm -f "$tmp_json" "$tmp_err"

    if (( total % 100 == 0 )); then
      log "info" "Audit progress: $total processed ($skipped unchanged)"
    fi
  done <"$sources_tmp"
  rm -f "$sources_tmp"

  log "info" "Audit completed. processed=$total probe_ok=$ok skipped=$skipped missing=$missing probe_fail=$probe_fail"
  end_ts="$(date +%s)"
  elapsed="$((end_ts - start_ts))"
  notify_discord_audit_done "$total" "$ok" "$missing" "$probe_fail" "$elapsed" "$skipped"
}

plan_cmd() {
  log "info" "Building conversion plan"
  load_streaming_candidates
  load_stale_candidates

  db "DELETE FROM conversion_plan;"

  local where_limit=""
  if [[ "$LIMIT" -gt 0 ]]; then
    where_limit=" LIMIT $LIMIT"
  fi

  db -separator $'\t' "
SELECT m.id,m.path,m.container,
       COALESCE(a.exists_flag,0),COALESCE(a.probe_ok,0),
       COALESCE((SELECT MAX(CASE WHEN stream_type='video' THEN width ELSE 0 END) FROM probe_streams ps WHERE ps.media_id=m.id),0) AS max_w,
       COALESCE((SELECT MAX(CASE WHEN stream_type='video' THEN height ELSE 0 END) FROM probe_streams ps WHERE ps.media_id=m.id),0) AS max_h,
       COALESCE((SELECT MAX(is_hdr) FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='video'),0) AS has_hdr,
       COALESCE((SELECT SUM(CASE WHEN stream_type='video' AND codec='h264' THEN 1 ELSE 0 END) FROM probe_streams ps WHERE ps.media_id=m.id),0) AS h264_v,
       COALESCE((SELECT SUM(CASE WHEN stream_type='video' THEN 1 ELSE 0 END) FROM probe_streams ps WHERE ps.media_id=m.id),0) AS total_v,
       COALESCE((SELECT SUM(CASE WHEN stream_type='audio' AND codec='aac' AND channels <= 2 THEN 1 ELSE 0 END) FROM probe_streams ps WHERE ps.media_id=m.id),0) AS good_a,
       COALESCE((SELECT SUM(CASE WHEN stream_type='audio' THEN 1 ELSE 0 END) FROM probe_streams ps WHERE ps.media_id=m.id),0) AS total_a
FROM media_files m
LEFT JOIN audit_status a ON a.media_id=m.id
ORDER BY m.path${where_limit};
" | while IFS=$'\t' read -r media_id path container exists_flag probe_ok max_w max_h has_hdr h264_v total_v good_a total_a; do

    local eligible=0 reason="" skip_reason=""
    local target_container="$container"
    local container_ok=0
    if [[ "$container" == "mp4" || "$container" == "mkv" ]]; then
      container_ok=1
    else
      target_container="$DEFAULT_TARGET_CONTAINER"
    fi

    local priority=10
    if [[ "$exists_flag" -ne 1 ]]; then
      skip_reason="missing_file"
    elif [[ "$probe_ok" -ne 1 ]]; then
      skip_reason="probe_failed"
    elif [[ "$has_hdr" -eq 1 ]]; then
      skip_reason="hdr_skipped"
    elif [[ "$max_w" -ge 3840 || "$max_h" -ge 2160 ]]; then
      skip_reason="uhd_skipped"
    elif is_streaming_candidate_inline "$path"; then
      skip_reason="streaming_candidate"
    elif is_stale_candidate_inline "$path"; then
      skip_reason="stale_candidate"
    else
      if [[ "$container_ok" -eq 1 && "$total_v" -gt 0 && "$h264_v" -eq "$total_v" && "$total_a" -gt 0 && "$good_a" -eq "$total_a" ]]; then
        skip_reason="already_compliant"
      else
        eligible=1
        if [[ "$total_v" -gt 0 && "$h264_v" -eq "$total_v" ]]; then
          reason="audio_only"
          priority=1
        else
          reason="needs_transcode"
          priority=10
        fi
      fi
    fi

    db <<SQL
INSERT INTO conversion_plan(media_id,plan_ts,eligible,priority,reason,target_video,target_audio,target_container,skip_reason)
VALUES(
  $media_id,
  CURRENT_TIMESTAMP,
  $eligible,
  $priority,
  '$(sql_quote "$reason")',
  '$TARGET_VIDEO_CODEC',
  '$TARGET_AUDIO_CODEC',
  '$(sql_quote "$target_container")',
  '$(sql_quote "$skip_reason")'
);
SQL
  done

  log "info" "Plan completed"
}

report_cmd() {
  local report_md="$STATE_DIR/latest_report.md"
  local audit_csv="$STATE_DIR/audit_summary.csv"
  local plan_csv="$STATE_DIR/plan_summary.csv"
  local plan_items_csv="$STATE_DIR/plan_items.csv"
  local failed_csv="$STATE_DIR/failed_items.csv"

  {
    echo "metric,value"
    echo "media_files,$(db "SELECT COUNT(*) FROM media_files;")"
    echo "audit_ok,$(db "SELECT COUNT(*) FROM audit_status WHERE probe_ok=1;")"
    echo "missing_files,$(db "SELECT COUNT(*) FROM audit_status WHERE exists_flag=0;")"
    echo "plan_eligible,$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;")"
    echo "plan_skipped,$(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=0;")"
  } >"$audit_csv"

  {
    echo "skip_reason,count"
    db -separator ',' "SELECT COALESCE(skip_reason,'eligible') AS reason, COUNT(*) FROM conversion_plan GROUP BY reason ORDER BY COUNT(*) DESC;"
  } >"$plan_csv"

  {
    echo "path,eligible,status,reason,container,target_container,original_audio_language,video_codecs,audio_codecs,subtitle_codecs,audio_languages,subtitle_languages"
    db -separator ',' "
SELECT
  REPLACE(m.path, ',', ';') AS path,
  cp.eligible,
  CASE WHEN cp.eligible=1 THEN 'eligible' ELSE COALESCE(NULLIF(cp.skip_reason,''),'unknown_skip') END AS status,
  CASE WHEN cp.eligible=1 THEN COALESCE(NULLIF(cp.reason,''),'needs_transcode') ELSE COALESCE(NULLIF(cp.skip_reason,''),'unknown_skip') END AS reason,
  COALESCE(m.container, '') AS container,
  COALESCE(cp.target_container, '') AS target_container,
  COALESCE(
    NULLIF((SELECT language FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='audio' AND ps.default_flag=1 AND COALESCE(ps.language,'')<>'' ORDER BY ps.stream_index LIMIT 1), ''),
    NULLIF((SELECT language FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='audio' AND COALESCE(ps.language,'')<>'' ORDER BY ps.stream_index LIMIT 1), ''),
    'und'
  ) AS original_audio_language,
  COALESCE((SELECT GROUP_CONCAT(codec, '+') FROM (SELECT DISTINCT codec FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='video' AND COALESCE(codec,'')<>'' ORDER BY codec)), '') AS video_codecs,
  COALESCE((SELECT GROUP_CONCAT(codec, '+') FROM (SELECT DISTINCT codec FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='audio' AND COALESCE(codec,'')<>'' ORDER BY codec)), '') AS audio_codecs,
  COALESCE((SELECT GROUP_CONCAT(codec, '+') FROM (SELECT DISTINCT codec FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='subtitle' AND COALESCE(codec,'')<>'' ORDER BY codec)), '') AS subtitle_codecs,
  COALESCE((SELECT GROUP_CONCAT(language, '+') FROM (SELECT DISTINCT language FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='audio' AND COALESCE(language,'')<>'' ORDER BY language)), '') AS audio_languages,
  COALESCE((SELECT GROUP_CONCAT(language, '+') FROM (SELECT DISTINCT language FROM probe_streams ps WHERE ps.media_id=m.id AND ps.stream_type='subtitle' AND COALESCE(language,'')<>'' ORDER BY language)), '') AS subtitle_languages
FROM conversion_plan cp
JOIN media_files m ON m.id=cp.media_id
ORDER BY status, m.path;
"
  } >"$plan_items_csv"

  {
    echo "path,status,error"
    db -separator ',' "
SELECT m.path, cr.status, REPLACE(COALESCE(cr.error,''), ',', ';')
FROM conversion_runs cr
JOIN media_files m ON m.id=cr.media_id
WHERE cr.status IN ('failed','attempt_limit_reached')
ORDER BY cr.id DESC;
"
  } >"$failed_csv"

  {
    echo "# Library Codec Manager Report"
    echo
    echo "Generated: $(date '+%Y-%m-%d %H:%M:%S')"
    echo
    echo "## Counts"
    echo "- Media files: $(db "SELECT COUNT(*) FROM media_files;")"
    echo "- Audit ok: $(db "SELECT COUNT(*) FROM audit_status WHERE probe_ok=1;")"
    echo "- Missing files: $(db "SELECT COUNT(*) FROM audit_status WHERE exists_flag=0;")"
    echo "- Eligible for convert: $(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;")"
    echo "  - Audio-only (priority 1): $(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1 AND priority=1;")"
    echo "  - Video transcode (priority 10): $(db "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1 AND priority=10;")"
    echo "- Completed swaps: $(db "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped';")"
    echo "- Failures: $(db "SELECT COUNT(*) FROM conversion_runs WHERE status='failed';")"
    echo "- Attempt limit reached: $(db "SELECT COUNT(DISTINCT media_id) FROM conversion_runs WHERE status='attempt_limit_reached';")"
    echo
    echo "## Top Skip Reasons"
    db -separator '|' "SELECT COALESCE(skip_reason,'eligible'), COUNT(*) FROM conversion_plan GROUP BY skip_reason ORDER BY COUNT(*) DESC LIMIT 10;" | awk -F'|' '{printf("- %s: %s\n",$1,$2)}'
  } >"$report_md"

  log "info" "Report written: $report_md"
  log "info" "CSV outputs: $audit_csv, $plan_csv, $plan_items_csv, $failed_csv"
}

verify_transcoded_file() {
  local src="$1" dst="$2"
  local src_dur dst_dur dur_delta v_ok a_ok

  src_dur="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$src" 2>/dev/null | awk '{printf("%.3f",$1)}' || echo 0)"
  dst_dur="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$dst" 2>/dev/null | awk '{printf("%.3f",$1)}' || echo 0)"
  dur_delta="$(awk -v a="$src_dur" -v b="$dst_dur" 'BEGIN{d=a-b; if(d<0)d=-d; printf "%.3f", d}')"

  v_ok="$(ffprobe -v error -select_streams v -show_entries stream=codec_name -of csv=p=0 "$dst" 2>/dev/null | awk '{if($1!="h264") bad=1} END{if(bad) print 0; else print 1}')"
  a_ok="$(ffprobe -v error -select_streams a -show_entries stream=codec_name,channels -of csv=p=0 "$dst" 2>/dev/null | awk -F',' '{if($1!="aac" || $2>2) bad=1} END{if(NR==0||bad) print 0; else print 1}')"

  if awk -v d="$dur_delta" 'BEGIN{exit (d<=2.5)?0:1}'; then
    :
  else
    echo "duration_delta_too_high:$dur_delta"
    return 1
  fi

  [[ "$v_ok" -eq 1 ]] || { echo "video_codec_validation_failed"; return 1; }
  [[ "$a_ok" -eq 1 ]] || { echo "audio_codec_validation_failed"; return 1; }

  # Subtitle stream count check — catch silent drops (e.g. ASS/PGS in MP4)
  local src_subs dst_subs
  src_subs="$(ffprobe -v error -select_streams s -show_entries stream=index \
    -of csv=p=0 "$src" </dev/null 2>/dev/null | wc -l)"
  dst_subs="$(ffprobe -v error -select_streams s -show_entries stream=index \
    -of csv=p=0 "$dst" </dev/null 2>/dev/null | wc -l)"
  if [[ "$src_subs" -gt 0 && "$dst_subs" -lt "$src_subs" ]]; then
    echo "subtitle_stream_loss:src=${src_subs},dst=${dst_subs}"
    return 1
  fi

  return 0
}

audio_streams_already_compliant() {
  local src="$1"
  ffprobe -v error -select_streams a -show_entries stream=codec_name,channels,sample_rate -of csv=p=0 "$src" 2>/dev/null \
    | awk -F',' '
        BEGIN { n=0; ok=1 }
        {
          n++
          gsub(/[[:space:]]+/, "", $1)
          if ($1 != "aac" || $2 > 2 || $3 != 48000) ok=0
        }
        END { exit (n > 0 && ok) ? 0 : 1 }
      '
}

select_audio_streams_for_conversion() {
  local src="$1"
  local profile_lang_set="${2:-}"     # pre-expanded space-separated set (from expand_lang_codes_inline)
  local orig_lang_override="${3:-}"   # 3-letter code from Bazarr metadata
  local rows="" orig_lang="und"
  local -A seen=()

  rows="$(ffprobe -v error -show_entries stream=index,codec_type:stream_tags=language:stream_disposition=default -of json "$src" 2>/dev/null \
    | jq -r '
        .streams[]?
        | select(.codec_type=="audio")
        | [
            (.index | tostring),
            ((.tags.language // "") | ascii_downcase | if .=="" then "und" else . end),
            ((.disposition.default // 0) | tostring)
          ]
        | @tsv
      ')"

  [[ -n "$rows" ]] || return 1

  # Determine original language: prefer Bazarr metadata override, then default-flag, then first non-und.
  if [[ -n "$orig_lang_override" ]]; then
    orig_lang="$orig_lang_override"
  else
    while IFS=$'\t' read -r idx lang def; do
      [[ -z "$idx" ]] && continue
      if [[ "$def" == "1" && "$lang" != "und" ]]; then
        orig_lang="$lang"
        break
      fi
    done <<<"$rows"
    if [[ "$orig_lang" == "und" ]]; then
      while IFS=$'\t' read -r idx lang def; do
        [[ -z "$idx" ]] && continue
        if [[ "$lang" != "und" ]]; then
          orig_lang="$lang"
          break
        fi
      done <<<"$rows"
    fi
  fi

  printf '__ORIG__\t%s\n' "$orig_lang"

  # Profile-aware selection: keep streams matching profile OR original language.
  if [[ -n "$profile_lang_set" ]]; then
    local orig_expanded=""
    [[ -n "$orig_lang_override" ]] && orig_expanded="$(expand_lang_codes_inline "$orig_lang_override")"
    while IFS=$'\t' read -r idx lang def; do
      [[ -z "$idx" ]] && continue
      local keep=0
      if lang_in_set_inline "$lang" "$profile_lang_set"; then
        keep=1
      elif [[ -n "$orig_expanded" ]] && lang_in_set_inline "$lang" "$orig_expanded"; then
        keep=1
      fi
      if [[ "$keep" -eq 1 && -z "${seen[$idx]:-}" ]]; then
        seen["$idx"]=1
        printf '%s\t%s\n' "$idx" "$lang"
      fi
    done <<<"$rows"
  else
    # Legacy fallback: hardcoded language list (no profile available).
    if [[ "$orig_lang" == "und" ]]; then
      while IFS=$'\t' read -r idx lang def; do
        [[ -z "$idx" ]] && continue
        if [[ "$def" == "1" || "$lang" == "eng" || "$lang" == "spa" || "$lang" == "ita" ]]; then
          if [[ -z "${seen[$idx]:-}" ]]; then
            seen["$idx"]=1
            printf '%s\t%s\n' "$idx" "$lang"
          fi
        fi
      done <<<"$rows"
    else
      while IFS=$'\t' read -r idx lang def; do
        [[ -z "$idx" ]] && continue
        if [[ "$lang" == "$orig_lang" || "$lang" == "eng" || "$lang" == "spa" || "$lang" == "ita" ]]; then
          if [[ -z "${seen[$idx]:-}" ]]; then
            seen["$idx"]=1
            printf '%s\t%s\n' "$idx" "$lang"
          fi
        fi
      done <<<"$rows"
    fi
  fi

  # Safety fallback: always select at least one stream.
  if [[ "${#seen[@]}" -eq 0 ]]; then
    while IFS=$'\t' read -r idx lang def; do
      [[ -z "$idx" ]] && continue
      printf '%s\t%s\n' "$idx" "$lang"
      break
    done <<<"$rows"
  fi
}

video_streams_already_compliant() {
  local src="$1"
  ffprobe -v error -select_streams v -show_entries stream=codec_name,pix_fmt -of csv=p=0 "$src" 2>/dev/null \
    | awk -F',' '
        BEGIN { n=0; ok=1 }
        {
          n++
          gsub(/[[:space:]]+/, "", $1)
          gsub(/[[:space:]]+/, "", $2)
          if ($1 != "h264" || $2 != "yuv420p") ok=0
        }
        END { exit (n > 0 && ok) ? 0 : 1 }
      '
}

build_backup_path() {
  local src="$1"
  local ts_day rel
  ts_day="$(date '+%Y%m%d')"
  rel="${src#/}"
  printf '%s/%s/%s' "$BACKUP_DIR" "$ts_day" "$rel"
}

build_temp_output_path() {
  local run_id="$1" media_id="$2" src="$3" tmp_ext="$4"
  local run_dir src_name stem safe_stem
  run_dir="${TMP_DIR}/${run_id}"
  mkdir -p "$run_dir"

  src_name="$(basename "$src")"
  stem="${src_name%.*}"
  safe_stem="$(printf '%s' "$stem" | tr -cs 'A-Za-z0-9._-' '_')"
  [[ -z "$safe_stem" ]] && safe_stem="media_${media_id}"

  printf '%s/%s.media_%s.%s' "$run_dir" "$safe_stem" "$media_id" "$tmp_ext"
}

run_convert_for_media() {
  local run_id="$1" media_id="$2" attempt_no="$3" src container
  src="$(db "SELECT path FROM media_files WHERE id=$media_id LIMIT 1;")"
  container="$(db "SELECT target_container FROM conversion_plan WHERE media_id=$media_id LIMIT 1;")"
  if [[ -z "$container" ]]; then
    container="$(db "SELECT container FROM media_files WHERE id=$media_id LIMIT 1;")"
  fi

  [[ -f "$src" ]] || {
    db "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'failed',${attempt_no:-1},'source_missing');"
    insert_event "error" "convert" "$media_id" "Source missing at convert time" "{\"path\":\"$(sql_quote "$src")\"}"
    return
  }

  local dst_tmp src_dir tmp_ext
  src_dir="$(dirname "$src")"
  tmp_ext="$container"
  [[ -z "$tmp_ext" ]] && tmp_ext="mkv"
  dst_tmp="$(build_temp_output_path "$run_id" "$media_id" "$src" "$tmp_ext")"

  # Hard safety rule: never write temp transcode outputs in the source media directory.
  case "$dst_tmp" in
    "$src_dir"/*)
      db "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'failed',${attempt_no:-1},'temp_path_in_media_dir_blocked');"
      insert_event "error" "convert" "$media_id" "Blocked unsafe temp path in media directory" "{\"src\":\"$(sql_quote "$src")\",\"tmp\":\"$(sql_quote "$dst_tmp")\"}"
      return
      ;;
  esac

  db "INSERT INTO conversion_runs(run_id,media_id,start_ts,status,attempt) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,'running',${attempt_no:-1});"

  # Resolve Bazarr profile languages + true original language for audio stream selection.
  local media_type_ref bazarr_ref_id_ref profile_langs="" profile_lang_set="" orig_lang_override=""
  IFS=$'\t' read -r media_type_ref bazarr_ref_id_ref < <(
    db -separator $'\t' "SELECT media_type, bazarr_ref_id FROM media_files WHERE id=$media_id LIMIT 1;"
  ) || true

  if [[ -n "$bazarr_ref_id_ref" && "$bazarr_ref_id_ref" != "NULL" && -f "$BAZARR_DB" ]]; then
    profile_langs="$(resolve_profile_langs_by_id "$media_type_ref" "$bazarr_ref_id_ref" "$BAZARR_DB")" || true
    if [[ -n "$profile_langs" ]]; then
      profile_lang_set="$(expand_lang_codes_inline "$profile_langs")"
    fi
    orig_lang_override="$(resolve_original_lang_by_id "$media_type_ref" "$bazarr_ref_id_ref" "$BAZARR_DB")" || true
    log "debug" "Profile langs media_id=$media_id type=$media_type_ref ref=$bazarr_ref_id_ref profile=$profile_langs orig_override=$orig_lang_override"
  fi

  local ff_cmd=() audio_copy=0 video_copy=0 orig_lang="und"
  local -a audio_map_args=() selected_audio_desc=()
  while IFS=$'\t' read -r aidx alang; do
    [[ -z "$aidx" ]] && continue
    if [[ "$aidx" == "__ORIG__" ]]; then
      orig_lang="$alang"
      continue
    fi
    audio_map_args+=( -map "0:${aidx}" )
    selected_audio_desc+=( "${aidx}:${alang}" )
  done < <(select_audio_streams_for_conversion "$src" "$profile_lang_set" "$orig_lang_override")

  if [[ "${#audio_map_args[@]}" -eq 0 ]]; then
    db "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'failed',${attempt_no:-1},'no_audio_stream_selected');"
    insert_event "error" "convert" "$media_id" "No audio stream selected for conversion" "{\"src\":\"$(sql_quote "$src")\"}"
    return
  fi

  if video_streams_already_compliant "$src"; then
    video_copy=1
  fi
  if audio_streams_already_compliant "$src"; then
    audio_copy=1
  fi

  ff_cmd=(ffmpeg -hide_banner -nostdin -y -i "$src" -map 0:v?)
  ff_cmd+=( "${audio_map_args[@]}" )
  ff_cmd+=( -map 0:s? )
  if [[ "$video_copy" -eq 1 ]]; then
    ff_cmd+=( -c:v copy )
  else
    ff_cmd+=( -c:v libx264 -preset "$TARGET_PRESET" -crf "$TARGET_CRF" -pix_fmt "$TARGET_PIX_FMT" )
  fi
  if [[ "$audio_copy" -eq 1 ]]; then
    ff_cmd+=( -c:a copy )
  else
    ff_cmd+=( -c:a aac -ac 2 -ar "$TARGET_SAMPLE_RATE" -b:a "$TARGET_AUDIO_BITRATE" )
  fi
  ff_cmd+=( -c:s copy )
  if [[ "$tmp_ext" == "mp4" ]]; then
    ff_cmd+=( -movflags +faststart )
  fi
  ff_cmd+=( "$dst_tmp" )

  log "info" "Audio selection media_id=$media_id profile=${profile_langs:-none} original_lang=$orig_lang selected=$(IFS=,; echo "${selected_audio_desc[*]}")"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "info" "DRY-RUN convert media_id=$media_id src=$src dst=$dst_tmp"
    db "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='planned',error='dry_run' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
    return
  fi

  if ! "${ff_cmd[@]}" >>"$LOG_PATH" 2>&1; then
    db "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='failed',error='ffmpeg_failed' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
    rm -f "$dst_tmp" || true
    insert_event "error" "convert" "$media_id" "ffmpeg failed" "{\"src\":\"$(sql_quote "$src")\"}"
    return
  fi

  local verify_err=""
  if ! verify_err="$(verify_transcoded_file "$src" "$dst_tmp" 2>&1)"; then
    db "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='failed',error='$(sql_quote "$verify_err")' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
    rm -f "$dst_tmp" || true
    insert_event "error" "convert" "$media_id" "verification failed" "{\"error\":\"$(sql_quote "$verify_err")\"}"
    return
  fi

  local backup_path old_hash new_hash
  backup_path="$(build_backup_path "$src")"
  mkdir -p "$(dirname "$backup_path")"

  old_hash="$(sha256sum "$src" | awk '{print $1}')"
  new_hash="$(sha256sum "$dst_tmp" | awk '{print $1}')"

  # swap with rollback safety
  if mv "$src" "$backup_path"; then
    if mv "$dst_tmp" "$src"; then
      db <<SQL
UPDATE conversion_runs
SET end_ts=CURRENT_TIMESTAMP,status='swapped',error=''
WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';

INSERT INTO artifacts(media_id,new_path,backup_path,verify_hash_old,verify_hash_new,verify_probe_ok,swapped_ts)
VALUES($media_id,'$(sql_quote "$src")','$(sql_quote "$backup_path")','$(sql_quote "$old_hash")','$(sql_quote "$new_hash")',1,CURRENT_TIMESTAMP);
SQL
      insert_event "info" "convert" "$media_id" "swap completed" "{\"src\":\"$(sql_quote "$src")\",\"backup\":\"$(sql_quote "$backup_path")\"}"
      emby_refresh_item "$src" || log "warn" "Emby refresh failed for media_id=$media_id (non-fatal)"
      arr_rescan_for_media "$media_type_ref" "$bazarr_ref_id_ref" || log "warn" "Arr rescan failed for media_id=$media_id (non-fatal)"
      bazarr_rescan_for_media "$media_type_ref" "$bazarr_ref_id_ref" || log "warn" "Bazarr rescan failed for media_id=$media_id (non-fatal)"
      update_bazarr_audio_language "$media_type_ref" "$bazarr_ref_id_ref" "$(IFS=,; echo "${selected_audio_desc[*]}")" || true
    else
      mv "$backup_path" "$src" || true
      db "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='rolled_back',error='swap_failed_rolled_back' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
      rm -f "$dst_tmp" || true
      insert_event "error" "convert" "$media_id" "swap failed and rolled back" "{}"
    fi
  else
    db "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='failed',error='backup_move_failed' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
    rm -f "$dst_tmp" || true
    insert_event "error" "convert" "$media_id" "failed to move original to backup" "{}"
  fi
}

convert_cmd() {
  acquire_convert_lock || return 0

  # Recover stale rows left as "running" after interrupted sessions/processes.
  local recovered
  recovered="$(db "
UPDATE conversion_runs
SET end_ts=CURRENT_TIMESTAMP,
    status='failed',
    error=CASE
      WHEN COALESCE(error,'')='' THEN 'stale_running_recovered'
      ELSE error
    END
WHERE status='running' AND end_ts IS NULL;
SELECT changes();
")"
  if [[ "${recovered:-0}" -gt 0 ]]; then
    log "warn" "Recovered stale running conversion rows: $recovered"
  fi

  local run_id
  run_id="run_$(date '+%Y%m%d_%H%M%S')"
  log "info" "Starting convert run_id=$run_id dry_run=$DRY_RUN max_attempts=$MAX_ATTEMPTS"

  local limit_clause=""
  if [[ "$BATCH_SIZE" -gt 0 ]]; then
    limit_clause=" LIMIT $BATCH_SIZE"
  fi

  local processed=0
  while IFS=$'\t' read -r media_id attempt_count; do
    [[ -z "$media_id" ]] && continue
    local attempt_no
    attempt_no=$((attempt_count + 1))
    if (( attempt_no > MAX_ATTEMPTS )); then
      local media_path
      media_path="$(db "SELECT path FROM media_files WHERE id=$media_id LIMIT 1;")"
      db "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'attempt_limit_reached',$attempt_count,'attempts_exceeded_${MAX_ATTEMPTS}');"
      insert_event "warn" "convert" "$media_id" "attempt limit reached; skipping media" "{\"path\":\"$(sql_quote "$media_path")\",\"attempts\":$attempt_count,\"max_attempts\":$MAX_ATTEMPTS}"
      notify_discord_attempt_limit "$media_id" "$media_path" "$attempt_count" "$MAX_ATTEMPTS"
      log "warn" "Skipping media_id=$media_id because attempts=$attempt_count exceed max_attempts=$MAX_ATTEMPTS"
      continue
    fi
    processed=$((processed + 1))
    log "info" "Converting media_id=$media_id attempt=$attempt_no/$MAX_ATTEMPTS"
    run_convert_for_media "$run_id" "$media_id" "$attempt_no"
  done < <(db -separator $'\t' "
SELECT cp.media_id,
       COALESCE((
         SELECT COUNT(*)
         FROM conversion_runs cr2
         WHERE cr2.media_id=cp.media_id
           AND cr2.status NOT IN ('planned','attempt_limit_reached')
       ),0) AS attempt_count
FROM conversion_plan cp
LEFT JOIN (
  SELECT media_id, MAX(id) AS max_id
  FROM conversion_runs
  GROUP BY media_id
) rmax ON rmax.media_id=cp.media_id
LEFT JOIN conversion_runs cr ON cr.id=rmax.max_id
WHERE cp.eligible=1
  AND COALESCE(cr.status,'') NOT IN ('swapped','running','attempt_limit_reached')
ORDER BY cp.priority, cp.media_id${limit_clause};
")

  log "info" "Convert run complete run_id=$run_id processed=$processed"
}

prune_backups_cmd() {
  local days="$RETENTION_DAYS"
  log "info" "Pruning backups older than ${days} days under $BACKUP_DIR"
  if [[ "$DRY_RUN" -eq 1 ]]; then
    find "$BACKUP_DIR" -type f -mtime +"$days" -print | sed -n '1,200p'
    log "info" "DRY-RUN prune listed candidates only"
    return
  fi

  find "$BACKUP_DIR" -type f -mtime +"$days" -print -delete | while IFS= read -r p; do
    insert_event "info" "prune" "" "backup_deleted" "{\"path\":\"$(sql_quote "$p")\"}"
  done

  find "$BACKUP_DIR" -type d -empty -delete || true
  log "info" "Prune backups complete"
}

daily_status_cmd() {
  notify_discord_daily_status
}

# Fast-path enqueue for a single newly-imported file.
# Probes the file, upserts into media_files + probe_streams, then
# creates/updates a conversion_plan row with priority=0 (highest).
# Non-fatal on all errors so the import hook is never blocked.
enqueue_import_cmd() {
  if [[ -z "$IMPORT_FILE" ]]; then
    log "error" "enqueue-import: --file is required"
    return 1
  fi
  if [[ -z "$IMPORT_MEDIA_TYPE" ]]; then
    log "error" "enqueue-import: --media-type is required"
    return 1
  fi
  if [[ ! -f "$IMPORT_FILE" ]]; then
    log "warn" "enqueue-import: file not found (may not be settled yet): $IMPORT_FILE"
    return 0
  fi
  load_streaming_candidates
  load_stale_candidates

  local path="$IMPORT_FILE"
  local media_type="$IMPORT_MEDIA_TYPE"
  local ref_id="${IMPORT_REF_ID:-NULL}"
  local container size_bytes mtime

  container="${path##*.}"
  container="$(printf '%s' "$container" | tr '[:upper:]' '[:lower:]')"
  size_bytes="$(stat -c '%s' "$path" 2>/dev/null || echo 0)"
  mtime="$(stat -c '%Y' "$path" 2>/dev/null || echo 0)"

  log "info" "enqueue-import: probing $path (type=$media_type ref=$ref_id)"

  # Upsert media_files row
  upsert_media_file "$media_type" "$ref_id" "$path" "$size_bytes" "$mtime" "$container"
  local media_id
  media_id="$(media_id_for_path "$path")"
  if [[ -z "$media_id" ]]; then
    log "error" "enqueue-import: failed to get media_id for $path"
    return 1
  fi

  # Probe the file
  local tmp_json tmp_err
  tmp_json="$(mktemp)"
  tmp_err="$(mktemp)"
  if ffprobe -v error -print_format json -show_streams -show_format "$path" >"$tmp_json" 2>"$tmp_err" </dev/null; then
    clear_probe_streams "$media_id"
    insert_probe_streams_from_json "$media_id" "$tmp_json"
    upsert_audit_status "$media_id" 1 1 ""
  else
    local err
    err="$(tr -d '\n' <"$tmp_err" | cut -c1-4000)"
    clear_probe_streams "$media_id"
    upsert_audit_status "$media_id" 1 0 "$err"
    log "warn" "enqueue-import: ffprobe failed for $path: $err"
    rm -f "$tmp_json" "$tmp_err"
    return 0
  fi
  rm -f "$tmp_json" "$tmp_err"

  # Evaluate plan eligibility (same logic as plan_cmd, single row)
  local max_w max_h has_hdr h264_v total_v good_a total_a
  max_w="$(db "SELECT COALESCE(MAX(CASE WHEN stream_type='video' THEN width ELSE 0 END),0) FROM probe_streams WHERE media_id=$media_id;")"
  max_h="$(db "SELECT COALESCE(MAX(CASE WHEN stream_type='video' THEN height ELSE 0 END),0) FROM probe_streams WHERE media_id=$media_id;")"
  has_hdr="$(db "SELECT COALESCE(MAX(is_hdr),0) FROM probe_streams WHERE media_id=$media_id AND stream_type='video';")"
  h264_v="$(db "SELECT COALESCE(SUM(CASE WHEN stream_type='video' AND codec='h264' THEN 1 ELSE 0 END),0) FROM probe_streams WHERE media_id=$media_id;")"
  total_v="$(db "SELECT COALESCE(SUM(CASE WHEN stream_type='video' THEN 1 ELSE 0 END),0) FROM probe_streams WHERE media_id=$media_id;")"
  good_a="$(db "SELECT COALESCE(SUM(CASE WHEN stream_type='audio' AND codec='aac' AND channels<=2 THEN 1 ELSE 0 END),0) FROM probe_streams WHERE media_id=$media_id;")"
  total_a="$(db "SELECT COALESCE(SUM(CASE WHEN stream_type='audio' THEN 1 ELSE 0 END),0) FROM probe_streams WHERE media_id=$media_id;")"

  local eligible=0 reason="" skip_reason="" priority=99
  local target_container="$container"
  local container_ok=0
  if [[ "$container" == "mp4" || "$container" == "mkv" ]]; then
    container_ok=1
  else
    target_container="$DEFAULT_TARGET_CONTAINER"
  fi

  if [[ "$has_hdr" -eq 1 ]]; then
    skip_reason="hdr_skipped"
  elif [[ "$max_w" -ge 3840 || "$max_h" -ge 2160 ]]; then
    skip_reason="uhd_skipped"
  elif is_streaming_candidate_inline "$path"; then
    skip_reason="streaming_candidate"
    priority=99
    log "info" "enqueue-import: streaming candidate, skipping: $path"
  elif is_stale_candidate_inline "$path"; then
    skip_reason="stale_candidate"
    priority=99
    log "info" "enqueue-import: stale candidate, skipping: $path"
  elif [[ "$container_ok" -eq 1 && "$total_v" -gt 0 && "$h264_v" -eq "$total_v" && "$total_a" -gt 0 && "$good_a" -eq "$total_a" ]]; then
    skip_reason="already_compliant"
  else
    eligible=1
    priority=0
    if [[ "$total_v" -gt 0 && "$h264_v" -eq "$total_v" ]]; then
      reason="audio_only"
    else
      reason="needs_transcode"
    fi
  fi

  # Check if already has a conversion_plan row
  local existing_priority existing_status
  existing_priority="$(db "SELECT priority FROM conversion_plan WHERE media_id=$media_id LIMIT 1;" 2>/dev/null)"
  existing_status="$(db "SELECT COALESCE((SELECT status FROM conversion_runs WHERE media_id=$media_id ORDER BY id DESC LIMIT 1),'')")"

  # Skip if already converted
  if [[ "$existing_status" == "swapped" ]]; then
    log "info" "enqueue-import: already converted (status=swapped), skipping: $path"
    return 0
  fi

  if [[ -n "$existing_priority" ]]; then
    if [[ "$eligible" -eq 1 && "$existing_priority" -gt 0 ]]; then
      # Upgrade priority to 0
      db "UPDATE conversion_plan SET priority=0, eligible=1, reason='$(sql_quote "$reason")', plan_ts=CURRENT_TIMESTAMP WHERE media_id=$media_id;"
      log "info" "enqueue-import: upgraded priority $existing_priority→0 for $path (reason=$reason)"
    elif [[ "$eligible" -eq 1 && "$existing_priority" -eq 0 ]]; then
      log "info" "enqueue-import: already at priority=0, skipping: $path"
    else
      # Update plan row with latest evaluation
      db <<SQL
UPDATE conversion_plan SET eligible=$eligible, priority=$priority, reason='$(sql_quote "$reason")',
  skip_reason='$(sql_quote "$skip_reason")', plan_ts=CURRENT_TIMESTAMP WHERE media_id=$media_id;
SQL
      log "info" "enqueue-import: updated plan (eligible=$eligible skip=$skip_reason): $path"
    fi
  else
    # Insert new plan row
    db <<SQL
INSERT INTO conversion_plan(media_id,plan_ts,eligible,priority,reason,target_video,target_audio,target_container,skip_reason)
VALUES(
  $media_id,
  CURRENT_TIMESTAMP,
  $eligible,
  $priority,
  '$(sql_quote "$reason")',
  '$TARGET_VIDEO_CODEC',
  '$TARGET_AUDIO_CODEC',
  '$(sql_quote "$target_container")',
  '$(sql_quote "$skip_reason")'
);
SQL
    log "info" "enqueue-import: enqueued priority=$priority eligible=$eligible reason=${reason:-$skip_reason}: $path"
  fi
}

main() {
  parse_args "$@"
  ensure_state_dirs
  require_cmds
  init_db

  case "$COMMAND" in
    audit)
      audit_cmd
      ;;
    plan)
      plan_cmd
      ;;
    report)
      report_cmd
      ;;
    daily-status)
      daily_status_cmd
      ;;
    convert)
      convert_cmd
      ;;
    resume)
      convert_cmd
      ;;
    prune-backups)
      prune_backups_cmd
      ;;
    enqueue-import)
      enqueue_import_cmd
      ;;
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
