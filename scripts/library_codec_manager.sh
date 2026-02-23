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
DISCORD_WEBHOOK_AUDIT_DONE="${DISCORD_WEBHOOK_AUDIT_DONE:-}"
DISCORD_WEBHOOK_STATUS="${DISCORD_WEBHOOK_STATUS:-$DISCORD_WEBHOOK_AUDIT_DONE}"
DEFAULT_TARGET_CONTAINER="mp4"
MAX_ATTEMPTS_DEFAULT=30
MAX_ATTEMPTS="$MAX_ATTEMPTS_DEFAULT"
RETENTION_DAYS="$RETENTION_DAYS_DEFAULT"

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

notify_discord_audit_done() {
  local processed="$1"
  local probe_ok="$2"
  local missing="$3"
  local probe_fail="$4"
  local elapsed="$5"

  [[ -z "${DISCORD_WEBHOOK_AUDIT_DONE:-}" ]] && return 0

  local payload
  payload="$(jq -nc \
    --arg processed "$processed" \
    --arg ok "$probe_ok" \
    --arg missing "$missing" \
    --arg fail "$probe_fail" \
    --arg elapsed "${elapsed}s" \
    --arg db "$DB_PATH" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: "🔍 Codec Manager — Audit Complete",
      description: (
        "✅ **Probe OK:** " + $ok + " · ❓ **Missing:** " + $missing + " · ❌ **Probe Fail:** " + $fail
      ),
      color: 3066993,
      fields: [
        {name: "📊 Processed", value: $processed, inline: true},
        {name: "⏱️ Elapsed", value: $elapsed, inline: true},
        {name: "🗃️ State DB", value: ("`" + $db + "`")}
      ],
      footer: {text: "Codec Manager Audit"},
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

  local media_count eligible_count swapped_total failed_total running_now attempt_limited_total
  local swapped_24h failed_24h recovered_24h attempt_limited_24h last_run
  media_count="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM media_files;")"
  eligible_count="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;")"
  swapped_total="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped';")"
  failed_total="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='failed';")"
  attempt_limited_total="$(sqlite3 "$DB_PATH" "SELECT COUNT(DISTINCT media_id) FROM conversion_runs WHERE status='attempt_limit_reached';")"
  running_now="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='running' AND end_ts IS NULL;")"
  swapped_24h="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  failed_24h="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='failed' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  attempt_limited_24h="$(sqlite3 "$DB_PATH" "SELECT COUNT(DISTINCT media_id) FROM conversion_runs WHERE status='attempt_limit_reached' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  recovered_24h="$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='failed' AND error='stale_running_recovered' AND COALESCE(end_ts,start_ts) >= datetime('now','-1 day');")"
  last_run="$(sqlite3 "$DB_PATH" "SELECT COALESCE((SELECT run_id || ' [' || status || '] @ ' || COALESCE(end_ts,start_ts) FROM conversion_runs ORDER BY id DESC LIMIT 1),'none');")"

  local payload
  payload="$(jq -nc \
    --arg now "$(date '+%Y-%m-%d %H:%M:%S %Z')" \
    --arg db "$DB_PATH" \
    --arg m "$media_count" \
    --arg e "$eligible_count" \
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
      description: (
        "🗃️ **Media tracked:** " + $m + " · 📋 **Eligible:** " + $e + "\n\n" +
        "**All Time**\n" +
        "✅ Swapped: " + $st + " · ❌ Failed: " + $ft + " · ⚠️ Attempt limited: " + $at + "\n\n" +
        "**Last 24h**\n" +
        "✅ Swapped: " + $s24 + " · ❌ Failed: " + $f24 + " · ⚠️ Attempt limited: " + $a24 + " · 🔧 Recovered: " + $rec24 + "\n\n" +
        "🔄 **Running now:** " + $r + "\n" +
        "📌 **Last run:** `" + $lr + "`"
      ),
      color: 3447003,
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
    --arg db "$DB_PATH" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: "⚠️ Codec Manager — Attempt Limit Reached",
      description: (
        "🆔 **Media ID:** " + $media_id + "\n" +
        "🔄 **Attempts:** " + $attempts + " / " + $max + "\n" +
        "📁 `" + $path + "`\n\n" +
        "_Skipped for now — other files continue processing._"
      ),
      color: 15105570,
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

init_db() {
  sqlite3 "$DB_PATH" <<'SQL'
PRAGMA journal_mode=WAL;
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
}

insert_event() {
  local sev="$1" scope="$2" media_id="$3" msg="$4" ctx="$5"
  local msg_q ctx_q
  msg_q="$(sql_quote "$msg")"
  ctx_q="$(sql_quote "$ctx")"
  sqlite3 "$DB_PATH" "INSERT INTO events(severity,scope,media_id,message,context_json) VALUES('$(sql_quote "$sev")','$(sql_quote "$scope")',${media_id:-NULL},'$msg_q','$ctx_q');"
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

  sqlite3 "$DB_PATH" <<SQL
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
  sqlite3 "$DB_PATH" "SELECT id FROM media_files WHERE path='$qpath' LIMIT 1;"
}

upsert_audit_status() {
  local media_id="$1" exists_flag="$2" probe_ok="$3" probe_error="$4"
  local qerr
  qerr="$(sql_quote "$probe_error")"
  sqlite3 "$DB_PATH" <<SQL
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
  sqlite3 "$DB_PATH" "DELETE FROM probe_streams WHERE media_id=$media_id;"
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
  jq -r '.streams[]? | [
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
  ] | join("|")' "$json_file" | while IFS='|' read -r idx stype codec profile pix_fmt width height fps channels sample_rate lang forced dflag hdr; do
    [[ -z "$idx" ]] && continue
    sqlite3 "$DB_PATH" <<SQL
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
);
SQL
  done
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
    stored_mtime="$(sqlite3 "$DB_PATH" "SELECT mtime FROM media_files WHERE id=$media_id LIMIT 1;" 2>/dev/null)"
    if [[ "$stored_mtime" == "$mtime" && -n "$stored_mtime" ]]; then
      has_probes="$(sqlite3 "$DB_PATH" "SELECT 1 FROM probe_streams WHERE media_id=$media_id LIMIT 1;" 2>/dev/null)"
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
  notify_discord_audit_done "$total" "$ok" "$missing" "$probe_fail" "$elapsed"
}

plan_cmd() {
  log "info" "Building conversion plan"

  sqlite3 "$DB_PATH" "DELETE FROM conversion_plan;"

  local where_limit=""
  if [[ "$LIMIT" -gt 0 ]]; then
    where_limit=" LIMIT $LIMIT"
  fi

  sqlite3 -separator $'\t' "$DB_PATH" "
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

    if [[ "$exists_flag" -ne 1 ]]; then
      skip_reason="missing_file"
    elif [[ "$probe_ok" -ne 1 ]]; then
      skip_reason="probe_failed"
    elif [[ "$has_hdr" -eq 1 ]]; then
      skip_reason="hdr_skipped"
    elif [[ "$max_w" -ge 3840 || "$max_h" -ge 2160 ]]; then
      skip_reason="uhd_skipped"
    else
      if [[ "$container_ok" -eq 1 && "$total_v" -gt 0 && "$h264_v" -eq "$total_v" && "$total_a" -gt 0 && "$good_a" -eq "$total_a" ]]; then
        skip_reason="already_compliant"
      else
        eligible=1
        reason="needs_transcode"
      fi
    fi

    sqlite3 "$DB_PATH" <<SQL
INSERT INTO conversion_plan(media_id,plan_ts,eligible,reason,target_video,target_audio,target_container,skip_reason)
VALUES(
  $media_id,
  CURRENT_TIMESTAMP,
  $eligible,
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
    echo "media_files,$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM media_files;")"
    echo "audit_ok,$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM audit_status WHERE probe_ok=1;")"
    echo "missing_files,$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM audit_status WHERE exists_flag=0;")"
    echo "plan_eligible,$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;")"
    echo "plan_skipped,$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_plan WHERE eligible=0;")"
  } >"$audit_csv"

  {
    echo "skip_reason,count"
    sqlite3 -separator ',' "$DB_PATH" "SELECT COALESCE(skip_reason,'eligible') AS reason, COUNT(*) FROM conversion_plan GROUP BY reason ORDER BY COUNT(*) DESC;"
  } >"$plan_csv"

  {
    echo "path,eligible,status,reason,container,target_container,original_audio_language,video_codecs,audio_codecs,subtitle_codecs,audio_languages,subtitle_languages"
    sqlite3 -separator ',' "$DB_PATH" "
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
    sqlite3 -separator ',' "$DB_PATH" "
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
    echo "- Media files: $(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM media_files;")"
    echo "- Audit ok: $(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM audit_status WHERE probe_ok=1;")"
    echo "- Missing files: $(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM audit_status WHERE exists_flag=0;")"
    echo "- Eligible for convert: $(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;")"
    echo "- Completed swaps: $(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='swapped';")"
    echo "- Failures: $(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM conversion_runs WHERE status='failed';")"
    echo "- Attempt limit reached: $(sqlite3 "$DB_PATH" "SELECT COUNT(DISTINCT media_id) FROM conversion_runs WHERE status='attempt_limit_reached';")"
    echo
    echo "## Top Skip Reasons"
    sqlite3 -separator '|' "$DB_PATH" "SELECT COALESCE(skip_reason,'eligible'), COUNT(*) FROM conversion_plan GROUP BY skip_reason ORDER BY COUNT(*) DESC LIMIT 10;" | awk -F'|' '{printf("- %s: %s\n",$1,$2)}'
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

  printf '__ORIG__\t%s\n' "$orig_lang"

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

  # Ensure at least one audio stream is selected.
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
  src="$(sqlite3 "$DB_PATH" "SELECT path FROM media_files WHERE id=$media_id LIMIT 1;")"
  container="$(sqlite3 "$DB_PATH" "SELECT target_container FROM conversion_plan WHERE media_id=$media_id LIMIT 1;")"
  if [[ -z "$container" ]]; then
    container="$(sqlite3 "$DB_PATH" "SELECT container FROM media_files WHERE id=$media_id LIMIT 1;")"
  fi

  [[ -f "$src" ]] || {
    sqlite3 "$DB_PATH" "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'failed',${attempt_no:-1},'source_missing');"
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
      sqlite3 "$DB_PATH" "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'failed',${attempt_no:-1},'temp_path_in_media_dir_blocked');"
      insert_event "error" "convert" "$media_id" "Blocked unsafe temp path in media directory" "{\"src\":\"$(sql_quote "$src")\",\"tmp\":\"$(sql_quote "$dst_tmp")\"}"
      return
      ;;
  esac

  sqlite3 "$DB_PATH" "INSERT INTO conversion_runs(run_id,media_id,start_ts,status,attempt) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,'running',${attempt_no:-1});"

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
  done < <(select_audio_streams_for_conversion "$src")

  if [[ "${#audio_map_args[@]}" -eq 0 ]]; then
    sqlite3 "$DB_PATH" "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'failed',${attempt_no:-1},'no_audio_stream_selected');"
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

  log "info" "Audio selection media_id=$media_id original_lang=$orig_lang selected=$(IFS=,; echo "${selected_audio_desc[*]}")"

  if [[ "$DRY_RUN" -eq 1 ]]; then
    log "info" "DRY-RUN convert media_id=$media_id src=$src dst=$dst_tmp"
    sqlite3 "$DB_PATH" "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='planned',error='dry_run' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
    return
  fi

  if ! "${ff_cmd[@]}" >>"$LOG_PATH" 2>&1; then
    sqlite3 "$DB_PATH" "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='failed',error='ffmpeg_failed' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
    rm -f "$dst_tmp" || true
    insert_event "error" "convert" "$media_id" "ffmpeg failed" "{\"src\":\"$(sql_quote "$src")\"}"
    return
  fi

  local verify_err=""
  if ! verify_err="$(verify_transcoded_file "$src" "$dst_tmp" 2>&1)"; then
    sqlite3 "$DB_PATH" "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='failed',error='$(sql_quote "$verify_err")' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
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
      sqlite3 "$DB_PATH" <<SQL
UPDATE conversion_runs
SET end_ts=CURRENT_TIMESTAMP,status='swapped',error=''
WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';

INSERT INTO artifacts(media_id,new_path,backup_path,verify_hash_old,verify_hash_new,verify_probe_ok,swapped_ts)
VALUES($media_id,'$(sql_quote "$src")','$(sql_quote "$backup_path")','$(sql_quote "$old_hash")','$(sql_quote "$new_hash")',1,CURRENT_TIMESTAMP);
SQL
      insert_event "info" "convert" "$media_id" "swap completed" "{\"src\":\"$(sql_quote "$src")\",\"backup\":\"$(sql_quote "$backup_path")\"}"
    else
      mv "$backup_path" "$src" || true
      sqlite3 "$DB_PATH" "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='rolled_back',error='swap_failed_rolled_back' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
      rm -f "$dst_tmp" || true
      insert_event "error" "convert" "$media_id" "swap failed and rolled back" "{}"
    fi
  else
    sqlite3 "$DB_PATH" "UPDATE conversion_runs SET end_ts=CURRENT_TIMESTAMP,status='failed',error='backup_move_failed' WHERE run_id='$(sql_quote "$run_id")' AND media_id=$media_id AND status='running';"
    rm -f "$dst_tmp" || true
    insert_event "error" "convert" "$media_id" "failed to move original to backup" "{}"
  fi
}

convert_cmd() {
  acquire_convert_lock || return 0

  # Recover stale rows left as "running" after interrupted sessions/processes.
  local recovered
  recovered="$(sqlite3 "$DB_PATH" "
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
      media_path="$(sqlite3 "$DB_PATH" "SELECT path FROM media_files WHERE id=$media_id LIMIT 1;")"
      sqlite3 "$DB_PATH" "INSERT INTO conversion_runs(run_id,media_id,start_ts,end_ts,status,attempt,error) VALUES('$(sql_quote "$run_id")',$media_id,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'attempt_limit_reached',$attempt_count,'attempts_exceeded_${MAX_ATTEMPTS}');"
      insert_event "warn" "convert" "$media_id" "attempt limit reached; skipping media" "{\"path\":\"$(sql_quote "$media_path")\",\"attempts\":$attempt_count,\"max_attempts\":$MAX_ATTEMPTS}"
      notify_discord_attempt_limit "$media_id" "$media_path" "$attempt_count" "$MAX_ATTEMPTS"
      log "warn" "Skipping media_id=$media_id because attempts=$attempt_count exceed max_attempts=$MAX_ATTEMPTS"
      continue
    fi
    processed=$((processed + 1))
    log "info" "Converting media_id=$media_id attempt=$attempt_no/$MAX_ATTEMPTS"
    run_convert_for_media "$run_id" "$media_id" "$attempt_no"
  done < <(sqlite3 -separator $'\t' "$DB_PATH" "
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
ORDER BY cp.media_id${limit_clause};
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
    *)
      usage
      exit 1
      ;;
  esac
}

main "$@"
