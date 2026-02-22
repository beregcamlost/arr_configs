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
transmission_removed=0

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

  # Extract import-blocked / already-imported candidate IDs
  jq -r '
    .[]
    | select(.status=="completed")
    | select((.trackedDownloadState // "") == "importBlocked")
    | select(
        ([.statusMessages[]? | ((.title // "") + " " + ((.messages // []) | join(" ")))] | join(" | "))
        | test("already imported"; "i")
      )
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

  desc="📊 **Candidates:** $total_candidates"$'\n'
  desc+="🗑️ **Queue removed:** $total_removed"$'\n'
  desc+="📡 **Transmission removed:** $transmission_removed"
  if [[ "$DRY_RUN" == true ]]; then
    desc+=$'\n\n'"_⚠️ Dry run — no changes made_"
  fi

  local payload
  payload="$(jq -nc \
    --arg title "$title" \
    --arg desc "$desc" \
    --argjson color "$color" \
    --arg ts "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{embeds: [{
      title: $title,
      description: $desc,
      color: $color,
      footer: {text: "Import Blocked Cleanup"},
      timestamp: $ts
    }]}')"

  curl -sS -X POST -H 'Content-Type: application/json' \
    -d "$payload" "$DISCORD_WEBHOOK_URL" >/dev/null || true
}

log "arr_cleanup_importblocked start${DRY_RUN:+ [dry-run]}"

cleanup_app "radarr" "$RADARR_URL" "$RADARR_KEY"
cleanup_app "sonarr" "$SONARR_URL" "$SONARR_KEY"
cleanup_transmission

log "arr_cleanup_importblocked done — candidates=${total_candidates} removed=${total_removed} transmission_removed=${transmission_removed}"

if [[ "$total_removed" -gt 0 || "$transmission_removed" -gt 0 ]]; then
  send_discord_summary
fi
