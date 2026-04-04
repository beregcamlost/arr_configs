#!/usr/bin/env bash
set -euo pipefail

# grab-monitor.sh — Monitors recent Sonarr/Radarr grabs (last 5 minutes) and removes
# any download whose release contains a language outside the allowed set for that item.
#
# Allowed languages per item = {English, Spanish, Spanish Latino, originalLanguage}
# Language IDs: English=1, Spanish=3, Spanish Latino: Sonarr=34, Radarr=37
#
# Note on blacklisting: we intentionally skip the Sonarr/Radarr blacklist API to avoid
# complexity with the bulk-blacklist endpoint. Removing the torrent from Transmission is
# sufficient — Sonarr/Radarr will detect the missing file and trigger a new search,
# picking a different release via its own quality/language filters (Custom Formats).
#
# Usage: grab-monitor.sh [--dry-run]

# ── Lock ─────────────────────────────────────────────────────────────────────
readonly LOCK_FILE="/tmp/grab-monitor.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another instance is already running"; exit 0; }

# ── Source environment ────────────────────────────────────────────────────────
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ENV_FILE="${SCRIPT_DIR}/../../.env"
# shellcheck source=/dev/null
[[ -f "$ENV_FILE" ]] && source "$ENV_FILE"

# ── Config ────────────────────────────────────────────────────────────────────
readonly STATE_DIR="/APPBOX_DATA/storage/.grab-monitor-state"
readonly STATE_DB="${STATE_DIR}/seen.db"
readonly LOG_FILE="${STATE_DIR}/grab-monitor.log"
readonly LOOK_BACK_SECONDS=300   # 5 minutes

# Language IDs — always allowed regardless of original language
readonly LANG_ENGLISH=1
readonly LANG_SPANISH=3
readonly LANG_SPANISH_LATINO_SONARR=34
readonly LANG_SPANISH_LATINO_RADARR=37

# ── Argument parsing ──────────────────────────────────────────────────────────
DRY_RUN=false
for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done
readonly DRY_RUN

# ── Required env vars ─────────────────────────────────────────────────────────
SONARR_URL="${SONARR_URL:-http://127.0.0.1:8989/sonarr}"
RADARR_URL="${RADARR_URL:-http://127.0.0.1:7878/radarr}"
SONARR_KEY="${SONARR_KEY:?SONARR_KEY env var required}"
RADARR_KEY="${RADARR_KEY:?RADARR_KEY env var required}"
TRANSMISSION_URL="${TRANSMISSION_URL:?TRANSMISSION_URL env var required}"
TRANSMISSION_USER="${TRANSMISSION_USER:?TRANSMISSION_USER env var required}"
TRANSMISSION_PASS="${TRANSMISSION_PASS:?TRANSMISSION_PASS env var required}"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

# ── Transmission session cache ────────────────────────────────────────────────
TRANSMISSION_SESSION_ID=""

# ── Temp files ────────────────────────────────────────────────────────────────
_TMP_RESPONSE="$(mktemp /tmp/grab-monitor-resp.XXXXXX.json)"
_TMP_TRANS_OUT="$(mktemp /tmp/grab-monitor-trans.XXXXXX.json)"
_TMP_ITEM_JSON=""
trap 'rm -f "$_TMP_RESPONSE" "$_TMP_TRANS_OUT" ${_TMP_ITEM_JSON:+"$_TMP_ITEM_JSON"}' EXIT

# ── Logging ───────────────────────────────────────────────────────────────────
ts()  { date '+%Y-%m-%d %H:%M:%S'; }
log() {
  printf '%s [grab-monitor] %s\n' "$(ts)" "$*" | tee -a "$LOG_FILE" >&2
}
die() { log "ERROR: $*"; exit 1; }

# ── State DB ──────────────────────────────────────────────────────────────────
init_db() {
  mkdir -p "$STATE_DIR"
  sqlite3 -cmd ".timeout 30000" "$STATE_DB" \
    "CREATE TABLE IF NOT EXISTS seen_grabs (
       history_id   INTEGER PRIMARY KEY,
       app          TEXT    NOT NULL,
       title        TEXT,
       processed_at INTEGER NOT NULL
     );" </dev/null
}

is_seen() {
  local history_id="$1"
  local count
  count="$(sqlite3 -cmd ".timeout 30000" "$STATE_DB" \
    "SELECT COUNT(*) FROM seen_grabs WHERE history_id=CAST('${history_id}' AS INTEGER);" </dev/null)"
  [[ "$count" -gt 0 ]]
}

mark_seen() {
  local history_id="$1" app="$2" title="$3"
  local safe_title
  safe_title="${title//\'/\'\'}"   # escape single quotes
  sqlite3 -cmd ".timeout 30000" "$STATE_DB" \
    "INSERT OR IGNORE INTO seen_grabs (history_id, app, title, processed_at)
     VALUES (CAST('${history_id}' AS INTEGER), '${app}', '${safe_title}', strftime('%s','now'));" </dev/null
}

purge_old_seen() {
  sqlite3 -cmd ".timeout 30000" "$STATE_DB" \
    "DELETE FROM seen_grabs WHERE processed_at < strftime('%s','now') - 86400;" </dev/null
}

# ── Transmission RPC ──────────────────────────────────────────────────────────
# transmission_rpc PAYLOAD OUTPUT_FILE → prints HTTP status code
transmission_rpc() {
  local payload="$1" output_file="$2"
  local http_code rpc_headers_file
  rpc_headers_file="$(mktemp)"

  http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" \
    -H 'Content-Type: application/json' \
    ${TRANSMISSION_SESSION_ID:+-H "X-Transmission-Session-Id: $TRANSMISSION_SESSION_ID"} \
    -d "$payload" -D "$rpc_headers_file" -o "$output_file" -w '%{http_code}' \
    "$TRANSMISSION_URL" </dev/null || true)"

  if [[ "$http_code" == "409" ]]; then
    TRANSMISSION_SESSION_ID="$(awk -F': ' \
      'tolower($1)=="x-transmission-session-id"{gsub("\r","",$2); print $2}' \
      "$rpc_headers_file" | tail -n1)"
    http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" \
      -H 'Content-Type: application/json' \
      -H "X-Transmission-Session-Id: $TRANSMISSION_SESSION_ID" \
      -d "$payload" -o "$output_file" -w '%{http_code}' \
      "$TRANSMISSION_URL" </dev/null || true)"
  fi

  rm -f "$rpc_headers_file"
  printf '%s' "$http_code"
}

# remove_from_transmission HASH
# Sends a torrent-remove RPC (delete-local-data=false).
remove_from_transmission() {
  local hash="$1"
  local payload http_code
  # Transmission accepts hash strings directly in the ids array
  payload="$(jq -cn --arg h "$hash" \
    '{method:"torrent-remove",arguments:{ids:[$h],"delete-local-data":false}}')"
  http_code="$(transmission_rpc "$payload" "$_TMP_TRANS_OUT")"
  if [[ "$http_code" != "200" ]]; then
    log "  [transmission] remove failed for hash=${hash} http_code=${http_code}"
    return 1
  fi
  local rpc_result
  rpc_result="$(jq -r '.result // "error"' "$_TMP_TRANS_OUT" 2>/dev/null || echo "error")"
  if [[ "$rpc_result" != "success" ]]; then
    log "  [transmission] remove RPC: result=${rpc_result} (torrent may not have been in queue)"
  else
    log "  [transmission] removed torrent hash=${hash}"
  fi
}

# ── Discord notification ──────────────────────────────────────────────────────
# send_discord APP TITLE RELEASE DETECTED_LANGS VIOLATION_LANGS
send_discord() {
  local app="$1" title="$2" release="$3" detected="$4" violation="$5"
  [[ -z "$DISCORD_WEBHOOK_URL" ]] && return 0

  local action
  if [[ "$DRY_RUN" == true ]]; then
    action="[dry-run] Would remove from Transmission"
  else
    action="Removed from Transmission"
  fi

  local ts_iso
  ts_iso="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

  local payload
  payload="$(jq -cn \
    --arg title   "$title" \
    --arg app     "$app" \
    --arg release "$release" \
    --arg detected "$detected" \
    --arg violation "$violation" \
    --arg action  "$action" \
    --arg ts      "$ts_iso" \
    '{
      embeds: [{
        title: "🚫 Grab Blocked — Language Violation",
        color: 15158332,
        fields: [
          {name: "Series/Movie", value: $title,     inline: true},
          {name: "App",          value: $app,        inline: true},
          {name: "Release",      value: $release,    inline: false},
          {name: "Detected Languages", value: $detected,  inline: true},
          {name: "Violation",    value: $violation,  inline: true},
          {name: "Action",       value: $action,     inline: false}
        ],
        footer: {text: "Grab Monitor"},
        timestamp: $ts
      }]
    }')"

  curl -sS -X POST -m 10 --connect-timeout 5 \
    -H 'Content-Type: application/json' \
    -d "$payload" \
    "$DISCORD_WEBHOOK_URL" </dev/null >/dev/null || true
}

# ── Per-app processing ────────────────────────────────────────────────────────
# process_app APP BASE_URL API_KEY SPANISH_LATINO_ID
process_app() {
  local app="$1" base_url="$2" api_key="$3" lat_id="$4"
  local now cutoff
  now="$(date +%s)"
  cutoff=$(( now - LOOK_BACK_SECONDS ))

  log "[$app] fetching recent history (last ${LOOK_BACK_SECONDS}s)..."

  # Fetch grabbed history — pre-load to file to avoid stdin consumption in loops
  if ! curl -sS \
      "${base_url}/api/v3/history?eventType=grabbed&pageSize=50&sortKey=date&sortDirection=descending&apikey=${api_key}" \
      -o "$_TMP_RESPONSE" </dev/null; then
    log "[$app] history fetch failed"
    return 0
  fi

  # Extract array of records within the lookback window as a newline-delimited JSON objects file.
  # Each line: history_id<TAB>series_or_movie_id<TAB>title<TAB>source_title<TAB>date_epoch<TAB>languages_json<TAB>torrent_hash
  local tmp_records
  tmp_records="$(mktemp)"

  jq -r --argjson cutoff "$cutoff" '
    .records[]
    | select(
        (.date | if . then (sub("\\..*Z$";"Z") | strptime("%Y-%m-%dT%H:%M:%SZ") | mktime) else 0 end) >= $cutoff
      )
    | [
        (.id           | tostring),
        ((.seriesId // .movieId // 0) | tostring),
        (.series.title // .movie.title // "Unknown"),
        (.sourceTitle // ""),
        ((.date | if . then (sub("\\..*Z$";"Z") | strptime("%Y-%m-%dT%H:%M:%SZ") | mktime) else 0 end) | tostring),
        ((.data.languages // []) | tojson),
        (.data.torrentInfoHash // "")
      ]
    | @tsv
  ' "$_TMP_RESPONSE" >"$tmp_records" 2>/dev/null || true

  local count
  count="$(wc -l <"$tmp_records" | tr -d ' ')"
  log "[$app] recent grabs in window: ${count}"

  local rec_count
  rec_count="$(jq '.records | length' "$_TMP_RESPONSE" 2>/dev/null || echo 0)"
  if [[ "$rec_count" -ge 50 ]]; then
    log "[$app] WARNING: hit pageSize=50 limit — some recent grabs may have been missed"
  fi

  if [[ "$count" -eq 0 ]]; then
    rm -f "$tmp_records"
    return 0
  fi

  # Load records into arrays to avoid stdin consumption in loop
  local -a rec_ids=()
  local -a rec_item_ids=()
  local -a rec_titles=()
  local -a rec_source_titles=()
  local -a rec_langs=()
  local -a rec_hashes=()

  while IFS=$'\t' read -r r_id r_item_id r_title r_source r_epoch r_langs r_hash; do
    rec_ids+=("$r_id")
    rec_item_ids+=("$r_item_id")
    rec_titles+=("$r_title")
    rec_source_titles+=("$r_source")
    rec_langs+=("$r_langs")
    rec_hashes+=("$r_hash")
  done <"$tmp_records"
  rm -f "$tmp_records"

  local tmp_item_json
  tmp_item_json="$(mktemp)"
  _TMP_ITEM_JSON="$tmp_item_json"

  local i
  for i in "${!rec_ids[@]}"; do
    local history_id="${rec_ids[$i]}"
    local item_id="${rec_item_ids[$i]}"
    local title="${rec_titles[$i]}"
    local source_title="${rec_source_titles[$i]}"
    local langs_json="${rec_langs[$i]}"
    local torrent_hash="${rec_hashes[$i]}"

    # Skip if already processed
    if is_seen "$history_id"; then
      log "[$app] skip history_id=${history_id} (already seen)"
      continue
    fi

    # Skip if languages absent or empty — Custom Formats handle it
    local lang_count
    lang_count="$(printf '%s' "$langs_json" | jq 'length')"
    if [[ "$lang_count" -eq 0 ]]; then
      log "[$app] skip history_id=${history_id} title='${title}' — no language data"
      mark_seen "$history_id" "$app" "$title"
      continue
    fi

    # Fetch original language for this series/movie
    local orig_lang_id=0
    local item_endpoint
    if [[ "$app" == "Sonarr" ]]; then
      item_endpoint="${base_url}/api/v3/series/${item_id}?apikey=${api_key}"
    else
      item_endpoint="${base_url}/api/v3/movie/${item_id}?apikey=${api_key}"
    fi

    if curl -sS "$item_endpoint" -o "$tmp_item_json" </dev/null 2>/dev/null; then
      orig_lang_id="$(jq -r '.originalLanguage.id // 0' "$tmp_item_json" 2>/dev/null || echo 0)"
    fi

    # Build allowed set: English + Spanish + Spanish Latino (app-specific) + originalLang
    local -a allowed_ids=("$LANG_ENGLISH" "$LANG_SPANISH" "$lat_id")
    if [[ "$orig_lang_id" -gt 0 ]]; then
      allowed_ids+=("$orig_lang_id")
    fi

    # Check each language in the release against allowed set
    # Extract: array of {id, name} objects
    local -a violation_names=()
    local -a all_lang_names=()

    # Pre-load language names and IDs from langs_json into indexed arrays
    local tmp_lang_tsv
    tmp_lang_tsv="$(mktemp)"
    printf '%s' "$langs_json" | jq -r '.[] | [(.id | tostring), (.name // "Unknown")] | @tsv' \
      >"$tmp_lang_tsv" 2>/dev/null || true

    local -a lang_id_arr=()
    local -a lang_name_arr=()
    while IFS=$'\t' read -r lid lname; do
      lang_id_arr+=("$lid")
      lang_name_arr+=("$lname")
      all_lang_names+=("$lname")
    done <"$tmp_lang_tsv"
    rm -f "$tmp_lang_tsv"

    # For each detected language, check if it's in the allowed set
    local j
    for j in "${!lang_id_arr[@]}"; do
      local lid="${lang_id_arr[$j]}"
      local lname="${lang_name_arr[$j]}"
      local allowed=false
      local aid
      for aid in "${allowed_ids[@]}"; do
        if [[ "$lid" -eq "$aid" ]]; then
          allowed=true
          break
        fi
      done
      if [[ "$allowed" == false ]]; then
        violation_names+=("$lname")
      fi
    done

    # Determine original language name for display
    local orig_lang_name="Unknown"
    if [[ "$orig_lang_id" -gt 0 ]]; then
      orig_lang_name="$(jq -r '.originalLanguage.name // "Unknown"' "$tmp_item_json" 2>/dev/null || echo "Unknown")"
    fi

    local detected_str
    detected_str="$(IFS=', '; printf '%s' "${all_lang_names[*]:-none}")"
    local hash_lc
    hash_lc="$(printf '%s' "$torrent_hash" | tr '[:upper:]' '[:lower:]')"

    if [[ "${#violation_names[@]}" -gt 0 ]]; then
      local violation_str
      violation_str="$(IFS=', '; printf '%s' "${violation_names[*]}")"
      log "[$app] VIOLATION history_id=${history_id} title='${title}' source='${source_title}' langs='${detected_str}' violators='${violation_str}'"

      if [[ "$DRY_RUN" == true ]]; then
        log "[$app] [dry-run] would remove torrent hash='${hash_lc}' from Transmission"
        log "[$app] [dry-run] would send Discord notification"
      else
        if [[ -n "$hash_lc" ]]; then
          remove_from_transmission "$hash_lc" || true
        else
          log "[$app] WARNING: no torrent hash for history_id=${history_id}, cannot remove"
        fi
        send_discord "$app" "${title} (${orig_lang_name})" "$source_title" "$detected_str" "$violation_str"
      fi
    else
      log "[$app] OK history_id=${history_id} title='${title}' langs='${detected_str}'"
    fi

    # Always mark as seen (dry-run or not) to avoid re-processing
    mark_seen "$history_id" "$app" "$title"

    # Reset per-iteration arrays
    lang_id_arr=()
    lang_name_arr=()
    all_lang_names=()
    violation_names=()
  done

  rm -f "$tmp_item_json"
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  log "Starting (dry_run=${DRY_RUN})"

  init_db
  purge_old_seen

  process_app "Sonarr" "$SONARR_URL" "$SONARR_KEY" "$LANG_SPANISH_LATINO_SONARR"
  process_app "Radarr" "$RADARR_URL" "$RADARR_KEY" "$LANG_SPANISH_LATINO_RADARR"

  log "Done."
}

main "$@"
