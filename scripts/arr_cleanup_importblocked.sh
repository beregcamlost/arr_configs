#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/tmp/arr_cleanup_importblocked.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another instance is already running"; exit 0; }

# Cleans completed import-blocked items that were already imported.
# This removes stale queue entries and deletes matching items from the download client.

# Usage: arr_cleanup_importblocked.sh [--dry-run]

DRY_RUN=false
for arg in "$@"; do
  [[ "$arg" == "--dry-run" ]] && DRY_RUN=true
done

RADARR_URL="${RADARR_URL:-http://127.0.0.1:7878/radarr}"
SONARR_URL="${SONARR_URL:-http://127.0.0.1:8989/sonarr}"
RADARR_KEY="${RADARR_KEY:?RADARR_KEY env var required}"
SONARR_KEY="${SONARR_KEY:?SONARR_KEY env var required}"
TRANSMISSION_URL="${TRANSMISSION_URL:?TRANSMISSION_URL env var required}"
TRANSMISSION_USER="${TRANSMISSION_USER:?TRANSMISSION_USER env var required}"
TRANSMISSION_PASS="${TRANSMISSION_PASS:?TRANSMISSION_PASS env var required}"
TRANSMISSION_LABELS="${TRANSMISSION_LABELS:-sonarr,radarr}"
COMPLETED_DIR="${COMPLETED_DIR:-/APPBOX_DATA/apps/transmission.vhscave.appboxes.co/torrents/completed}"
DISCORD_WEBHOOK_URL="${DISCORD_WEBHOOK_URL:-}"

# Cached Transmission session ID — populated on first 409, reused for subsequent calls
TRANSMISSION_SESSION_ID=""

# Temp files for transmission operations
_TMP_TRANSMISSION_LIST="$(mktemp /tmp/transmission_list.XXXXXX.json)"
_TMP_TRANSMISSION_RM="$(mktemp /tmp/transmission_rm.XXXXXX.json)"
# Shared active-hashes file built as a side effect of cleanup_app calls
_TMP_ACTIVE_HASHES="$(mktemp /tmp/active_hashes.XXXXXX.txt)"
trap 'rm -f "$_TMP_TRANSMISSION_LIST" "$_TMP_TRANSMISSION_RM" "$_TMP_ACTIVE_HASHES"' EXIT

# Summary counters
total_candidates=0
total_removed=0
exe_removed=0
transmission_removed=0
disk_files_removed=0
disk_bytes_freed=0

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s %s\n' "$(ts)" "$*"; }

# transmission_rpc PAYLOAD OUTPUT_FILE
# Makes a Transmission RPC call, retrying once on 409 to pick up the session ID.
# Caches the session ID in TRANSMISSION_SESSION_ID for subsequent calls.
# Prints the HTTP status code.
transmission_rpc() {
  local payload="$1" output_file="$2"
  local http_code
  local rpc_headers_file
  rpc_headers_file="$(mktemp)"

  http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" \
    -H 'Content-Type: application/json' \
    ${TRANSMISSION_SESSION_ID:+-H "X-Transmission-Session-Id: $TRANSMISSION_SESSION_ID"} \
    -d "$payload" -D "$rpc_headers_file" -o "$output_file" -w '%{http_code}' \
    "$TRANSMISSION_URL" || true)"

  if [[ "$http_code" == "409" ]]; then
    TRANSMISSION_SESSION_ID="$(awk -F': ' 'tolower($1)=="x-transmission-session-id"{gsub("\r","",$2); print $2}' "$rpc_headers_file" | tail -n1)"
    http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" \
      -H 'Content-Type: application/json' \
      -H "X-Transmission-Session-Id: $TRANSMISSION_SESSION_ID" \
      -d "$payload" -o "$output_file" -w '%{http_code}' \
      "$TRANSMISSION_URL" || true)"
  fi

  rm -f "$rpc_headers_file"
  printf '%s' "$http_code"
}

# cleanup_app APP BASE_URL API_KEY
# Fetches the queue, removes stale import-blocked entries, and appends all
# active download hashes to _TMP_ACTIVE_HASHES as a side effect.
cleanup_app() {
  local app="$1" base_url="$2" api_key="$3"
  local tmp_json tmp_ids
  tmp_json="$(mktemp)"
  tmp_ids="$(mktemp)"

  if ! curl -sS "${base_url}/api/v3/queue/details?apikey=${api_key}&page=1&pageSize=1000&sortDirection=descending" >"$tmp_json"; then
    log "[$app] queue fetch failed"
    rm -f "$tmp_json" "$tmp_ids"
    return 0
  fi

  # Extract import-blocked candidate IDs — covers "already imported", "series title mismatch",
  # and any other importBlocked reason where files were deleted from disk
  jq -r '
    .[]
    | select(.status=="completed")
    | select((.trackedDownloadState // "") == "importBlocked")
    | .id
  ' "$tmp_json" >"$tmp_ids"

  # Append all active download hashes to the shared file (used by cleanup_transmission)
  jq -r '.[] | .downloadId // empty' "$tmp_json" | tr '[:upper:]' '[:lower:]' >>"$_TMP_ACTIVE_HASHES"

  local count
  count="$(wc -l <"$tmp_ids" | tr -d ' ')"
  log "[$app] candidates=${count}"
  total_candidates=$(( total_candidates + count ))

  if [[ "$count" -gt 0 ]]; then
    while IFS= read -r qid; do
      [[ -z "$qid" ]] && continue
      if [[ "$DRY_RUN" == true ]]; then
        log "[$app] [dry-run] would remove queue id=${qid}"
      else
        curl -sS -X DELETE "${base_url}/api/v3/queue/${qid}?apikey=${api_key}&removeFromClient=true&blocklist=false" >/dev/null || true
      fi
    done <"$tmp_ids"
    if [[ "$DRY_RUN" != true ]]; then
      log "[$app] removed=${count}"
      total_removed=$(( total_removed + count ))
    fi
  fi

  rm -f "$tmp_json" "$tmp_ids"
}

# cleanup_exe_pending APP BASE_URL API_KEY
# Fetches the queue and removes importPending entries whose title ends with a
# known executable extension (.exe .msi .bat .scr .com .vbs .ps1 .cmd).
# Uses blocklist=true so the item is blacklisted and won't be re-grabbed.
cleanup_exe_pending() {
  local app="$1" base_url="$2" api_key="$3"
  local tmp_json
  tmp_json="$(mktemp)"

  if ! curl -sS "${base_url}/api/v3/queue/details?apikey=${api_key}&page=1&pageSize=1000&sortDirection=descending" >"$tmp_json"; then
    log "[$app][exe] queue fetch failed"
    rm -f "$tmp_json"
    return 0
  fi

  # Build a TSV of id<TAB>title for importPending entries with executable extensions.
  # The regex is applied case-insensitively via test(pattern;"i").
  local tmp_matches
  tmp_matches="$(mktemp)"
  jq -r '
    .[]
    | select((.trackedDownloadState // "") == "importPending")
    | select(
        (.title // "") | test(
          "\\.(exe|msi|bat|scr|com|vbs|ps1|cmd)$"; "i"
        )
      )
    | [(.id | tostring), (.title // "")]
    | @tsv
  ' "$tmp_json" >"$tmp_matches"

  local count
  count="$(wc -l <"$tmp_matches" | tr -d ' ')"

  if [[ "$count" -gt 0 ]]; then
    log "[$app][exe] found ${count} importPending executable item(s)"
    while IFS=$'\t' read -r qid qtitle; do
      [[ -z "$qid" ]] && continue
      if [[ "$DRY_RUN" == true ]]; then
        log "[$app][exe] [dry-run] would blocklist queue id=${qid} title=${qtitle}"
      else
        curl -sS -X DELETE \
          "${base_url}/api/v3/queue/${qid}?apikey=${api_key}&removeFromClient=true&blocklist=true" \
          >/dev/null </dev/null || true
        log "[$app][exe] blocklisted queue id=${qid} title=${qtitle}"
        exe_removed=$(( exe_removed + 1 ))
        total_removed=$(( total_removed + 1 ))
      fi
    done <"$tmp_matches"
  fi

  rm -f "$tmp_json" "$tmp_matches"
}

cleanup_transmission() {
  local labels_json payload
  # Active hashes are already populated by the cleanup_app calls above
  sort -u -o "$_TMP_ACTIVE_HASHES" "$_TMP_ACTIVE_HASHES"

  labels_json="$(printf '%s' "$TRANSMISSION_LABELS" | jq -R 'split(",") | map(gsub("^\\s+|\\s+$";"")) | map(select(length>0))')"
  payload="$(jq -cn --argjson labels "$labels_json" '{method:"torrent-get",arguments:{fields:["id","hashString","name","labels","isFinished","percentDone","status"],format:"objects"}}')"

  local http_code
  http_code="$(transmission_rpc "$payload" "$_TMP_TRANSMISSION_LIST")"

  if [[ "$http_code" != "200" ]]; then
    log "[transmission] list failed http_code=${http_code}"
    return 0
  fi

  jq -r --argjson labels "$labels_json" '
    .arguments.torrents[]
    | select((.isFinished == true) or (.percentDone == 1))
    | select([(.labels // [])[]] | any(. as $lbl | $labels | index($lbl)))
    | [(.id|tostring), (.hashString|ascii_downcase), (.name // "")]
    | @tsv
  ' "$_TMP_TRANSMISSION_LIST" | while IFS=$'\t' read -r tid thash tname; do
    [[ -z "$tid" || -z "$thash" ]] && continue
    if ! grep -Fxqi "$thash" "$_TMP_ACTIVE_HASHES"; then
      local rm_payload rm_code
      rm_payload="$(jq -cn --argjson ids "[$tid]" '{method:"torrent-remove",arguments:{ids:$ids,"delete-local-data":false}}')"
      if [[ "$DRY_RUN" == true ]]; then
        log "[transmission] [dry-run] would remove id=${tid} hash=${thash} name=${tname}"
      else
        rm_code="$(transmission_rpc "$rm_payload" "$_TMP_TRANSMISSION_RM")"
        if [[ "$rm_code" == "200" ]]; then
          log "[transmission] removed id=${tid} hash=${thash} name=${tname}"
          transmission_removed=$(( transmission_removed + 1 ))
        else
          log "[transmission] remove failed id=${tid} http_code=${rm_code}"
        fi
      fi
    fi
  done
}

cleanup_completed_disk() {
  # Remove orphaned files/dirs from completed download folders that aren't:
  #   1) Referenced by any active Transmission torrent, AND
  #   2) Pending import in Sonarr/Radarr queue
  # Runs AFTER cleanup_app + cleanup_transmission so active hashes are populated.

  local active_names_file pending_names_file
  active_names_file="$(mktemp /tmp/active_names.XXXXXX.txt)"
  pending_names_file="$(mktemp /tmp/pending_names.XXXXXX.txt)"

  trap 'rm -f "$active_names_file" "$pending_names_file"' RETURN

  # Get active torrent names from Transmission (already fetched in cleanup_transmission)
  if [[ -s "$_TMP_TRANSMISSION_LIST" ]]; then
    if ! jq -e '.arguments.torrents' "$_TMP_TRANSMISSION_LIST" >/dev/null 2>&1; then
      log "[disk] WARN: Transmission list invalid JSON — aborting disk cleanup"
      return 0
    fi
    jq -r '.arguments.torrents[].name // empty' "$_TMP_TRANSMISSION_LIST" >"$active_names_file"
  fi

  # Get pending import names from Sonarr and Radarr queues — anything in the queue
  # is either downloading, waiting for import, or has import issues. Don't delete these.
  # SAFETY: if either API call fails, abort — we can't guarantee the pending list is complete.
  local tmp_queue
  tmp_queue="$(mktemp /tmp/arr_queue.XXXXXX.json)"

  if ! curl -sS --fail "${SONARR_URL}/api/v3/queue?apikey=${SONARR_KEY}&pageSize=1000&includeUnknownSeriesItems=true" >"$tmp_queue" 2>/dev/null; then
    log "[disk] WARN: Sonarr queue fetch failed — aborting disk cleanup"
    rm -f "$tmp_queue"
    return 0
  fi
  if ! jq -e '.records' "$tmp_queue" >/dev/null 2>&1; then
    log "[disk] WARN: Sonarr queue response invalid — aborting disk cleanup"
    rm -f "$tmp_queue"
    return 0
  fi
  jq -r '.records[]? | (.title // empty), (.outputPath // empty | split("/")[-1])' \
    "$tmp_queue" >>"$pending_names_file"

  if ! curl -sS --fail "${RADARR_URL}/api/v3/queue?apikey=${RADARR_KEY}&pageSize=1000&includeUnknownMovieItems=true" >"$tmp_queue" 2>/dev/null; then
    log "[disk] WARN: Radarr queue fetch failed — aborting disk cleanup"
    rm -f "$tmp_queue"
    return 0
  fi
  if ! jq -e '.records' "$tmp_queue" >/dev/null 2>&1; then
    log "[disk] WARN: Radarr queue response invalid — aborting disk cleanup"
    rm -f "$tmp_queue"
    return 0
  fi
  jq -r '.records[]? | (.title // empty), (.outputPath // empty | split("/")[-1])' \
    "$tmp_queue" >>"$pending_names_file"

  sort -u -o "$pending_names_file" "$pending_names_file"
  rm -f "$tmp_queue"

  local category
  for category in sonarr radarr; do
    local completed_path="${COMPLETED_DIR}/${category}"
    [[ -d "$completed_path" ]] || continue

    while IFS= read -r entry; do
      [[ -z "$entry" ]] && continue
      local entry_name entry_size
      entry_name="$(basename "$entry")"

      # Skip if torrent name matches an active torrent
      if grep -Fxq "$entry_name" "$active_names_file" 2>/dev/null; then
        continue
      fi

      # Skip if name matches a pending arr queue entry (downloading or awaiting import)
      if grep -Fxq "$entry_name" "$pending_names_file" 2>/dev/null; then
        log "[disk:${category}] skipping (pending import): ${entry_name}"
        continue
      fi

      # Safety: skip entries less than 1 hour old (give arr time to import)
      # Check media files first, fall back to directory mtime if no media files
      local newest_mtime
      newest_mtime="$(find "$entry" -type f \( -name '*.mkv' -o -name '*.mp4' -o -name '*.avi' \) -printf '%T@\n' 2>/dev/null | sort -rn | head -1)" || newest_mtime=""
      if [[ -z "$newest_mtime" ]]; then
        newest_mtime="$(stat -c '%Y' "$entry" 2>/dev/null)" || newest_mtime=""
      fi
      if [[ -n "$newest_mtime" ]]; then
        local now age_secs
        now="$(date +%s)"
        age_secs="$(( now - ${newest_mtime%%.*} ))"
        if [[ "$age_secs" -lt 3600 ]]; then
          log "[disk:${category}] skipping (too new, ${age_secs}s old): ${entry_name}"
          continue
        fi
      fi

      entry_size="$(du -sb "$entry" 2>/dev/null | awk '{print $1}')" || entry_size=0

      if [[ "$DRY_RUN" == true ]]; then
        log "[disk:${category}] [dry-run] would remove: ${entry_name} ($(( entry_size / 1048576 ))MB)"
      else
        if rm -rf "$entry"; then
          disk_files_removed=$(( disk_files_removed + 1 ))
          disk_bytes_freed=$(( disk_bytes_freed + entry_size ))
          log "[disk:${category}] removed: ${entry_name} ($(( entry_size / 1048576 ))MB)"
        else
          log "[disk:${category}] WARN: failed to remove: ${entry_name}"
        fi
      fi
    done < <(find "$completed_path" -mindepth 1 -maxdepth 1 2>/dev/null)
  done

  if [[ "$disk_bytes_freed" -gt 0 || "$disk_files_removed" -gt 0 ]]; then
    log "[disk] cleaned ${disk_files_removed} orphan(s), freed $(( disk_bytes_freed / 1048576 ))MB"
  fi
}

send_discord_summary() {
  [[ -z "${DISCORD_WEBHOOK_URL:-}" ]] && return 0

  local color title desc
  if [[ "$DRY_RUN" == true ]]; then
    color=15844367  # yellow
    title="🧹 Arr Cleanup — Dry Run"
  else
    color=3066993   # green
    title="🧹 Arr Cleanup — Complete"
  fi

  desc="Processed **$total_candidates** candidates"
  if [[ "$DRY_RUN" == true ]]; then
    desc+=" _(dry run — no changes made)_"
  fi

  local payload
  payload="$(jq -nc \
    --arg title "$title" \
    --arg desc "$desc" \
    --argjson color "$color" \
    --arg candidates "$total_candidates" \
    --arg removed "$total_removed" \
    --arg exe_removed "$exe_removed" \
    --arg tx_removed "$transmission_removed" \
    --arg disk_removed "$disk_files_removed" \
    --arg disk_freed "$(( disk_bytes_freed / 1048576 ))MB" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      fields: [
        {name: "📊 Candidates",           value: $candidates,  inline: true},
        {name: "🗑️ Queue Removed",        value: $removed,     inline: true},
        {name: "🚫 Exe Blocklisted",       value: $exe_removed, inline: true},
        {name: "📡 Transmission Removed",  value: $tx_removed,  inline: true},
        {name: "💾 Disk Cleaned",          value: ($disk_removed + " (" + $disk_freed + ")"), inline: true}
      ],
      footer: {text: "Import Blocked Cleanup"},
      timestamp: $ts
    }]}')"

  curl -sS -m 20 --connect-timeout 8 --retry 2 --retry-delay 1 --retry-all-errors \
    -X POST -H 'Content-Type: application/json' \
    -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null || true
}

log "arr_cleanup_importblocked start (dry_run=$DRY_RUN)"

cleanup_app "radarr" "$RADARR_URL" "$RADARR_KEY"
cleanup_app "sonarr" "$SONARR_URL" "$SONARR_KEY"
cleanup_exe_pending "radarr" "$RADARR_URL" "$RADARR_KEY"
cleanup_exe_pending "sonarr" "$SONARR_URL" "$SONARR_KEY"
cleanup_transmission
cleanup_completed_disk

log "arr_cleanup_importblocked done — candidates=${total_candidates} removed=${total_removed} exe_removed=${exe_removed} transmission_removed=${transmission_removed} disk_cleaned=${disk_files_removed} disk_freed=$(( disk_bytes_freed / 1048576 ))MB"

if [[ "$total_removed" -gt 0 || "$exe_removed" -gt 0 || "$transmission_removed" -gt 0 || "$disk_files_removed" -gt 0 ]]; then
  send_discord_summary
fi
