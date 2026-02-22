#!/usr/bin/env bash
set -euo pipefail

LOCK_FILE="/tmp/arr_cleanup_importblocked.lock"
exec 9>"$LOCK_FILE"
flock -n 9 || { echo "Another instance is already running"; exit 0; }

# Cleans completed import-blocked items that were already imported.
# This removes stale queue entries and deletes matching items from the download client.

RADARR_URL="${RADARR_URL:-http://127.0.0.1:7878/radarr}"
SONARR_URL="${SONARR_URL:-http://127.0.0.1:8989/sonarr}"
RADARR_KEY="${RADARR_KEY:?RADARR_KEY env var required}"
SONARR_KEY="${SONARR_KEY:?SONARR_KEY env var required}"
TRANSMISSION_URL="${TRANSMISSION_URL:?TRANSMISSION_URL env var required}"
TRANSMISSION_USER="${TRANSMISSION_USER:?TRANSMISSION_USER env var required}"
TRANSMISSION_PASS="${TRANSMISSION_PASS:?TRANSMISSION_PASS env var required}"
TRANSMISSION_LABELS="${TRANSMISSION_LABELS:-sonarr,radarr}"

# Temp files for transmission operations
_TMP_TRANSMISSION_LIST="$(mktemp /tmp/transmission_list.XXXXXX.json)"
_TMP_TRANSMISSION_RM="$(mktemp /tmp/transmission_rm.XXXXXX.json)"
trap 'rm -f "$_TMP_TRANSMISSION_LIST" "$_TMP_TRANSMISSION_RM"' EXIT

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { printf '%s %s\n' "$(ts)" "$*"; }

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

  local count
  count="$(wc -l <"$tmp_ids" | tr -d ' ')"
  log "[$app] candidates=${count}"

  if [[ "$count" -gt 0 ]]; then
    while IFS= read -r qid; do
      [[ -z "$qid" ]] && continue
      curl -sS -X DELETE "${base_url}/api/v3/queue/${qid}?apikey=${api_key}&removeFromClient=true&blocklist=false" >/dev/null || true
    done <"$tmp_ids"
    log "[$app] removed=${count}"
  fi

  rm -f "$tmp_json" "$tmp_ids"
}

collect_active_hashes() {
  local base_url="$1" api_key="$2" out_file="$3"
  local tmp_json
  tmp_json="$(mktemp)"

  if ! curl -sS "${base_url}/api/v3/queue/details?apikey=${api_key}&page=1&pageSize=1000&sortDirection=descending" >"$tmp_json"; then
    rm -f "$tmp_json"
    return 0
  fi
  jq -r '.[] | .downloadId // empty' "$tmp_json" | tr '[:upper:]' '[:lower:]' >>"$out_file"

  rm -f "$tmp_json"
}

cleanup_transmission() {
  local active_hashes labels_json payload list_json
  local rpc_session_file rpc_status http_code
  rpc_session_file="$(mktemp)"
  active_hashes="$(mktemp)"

  collect_active_hashes "$RADARR_URL" "$RADARR_KEY" "$active_hashes"
  collect_active_hashes "$SONARR_URL" "$SONARR_KEY" "$active_hashes"
  sort -u -o "$active_hashes" "$active_hashes"

  labels_json="$(printf '%s' "$TRANSMISSION_LABELS" | jq -R 'split(",") | map(gsub("^\\s+|\\s+$";"")) | map(select(length>0))')"
  payload="$(jq -cn --argjson labels "$labels_json" '{method:"torrent-get",arguments:{fields:["id","hashString","name","labels","isFinished","percentDone","status"],format:"objects"}}')"

  http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" -H 'Content-Type: application/json' -d "$payload" -D "$rpc_session_file" -o "$_TMP_TRANSMISSION_LIST" -w '%{http_code}' "$TRANSMISSION_URL" || true)"
  if [[ "$http_code" == "409" ]]; then
    local session_id
    session_id="$(awk -F': ' 'tolower($1)=="x-transmission-session-id"{gsub("\r","",$2); print $2}' "$rpc_session_file" | tail -n1)"
    http_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" -H 'Content-Type: application/json' -H "X-Transmission-Session-Id: ${session_id}" -d "$payload" -o "$_TMP_TRANSMISSION_LIST" -w '%{http_code}' "$TRANSMISSION_URL" || true)"
  fi

  if [[ "$http_code" != "200" ]]; then
    log "[transmission] list failed http_code=${http_code}"
    rm -f "$rpc_session_file" "$active_hashes"
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
    if ! grep -Fxqi "$thash" "$active_hashes"; then
      local rm_payload
      rm_payload="$(jq -cn --argjson ids "[$tid]" '{method:"torrent-remove",arguments:{ids:$ids,"delete-local-data":false}}')"
      local rm_code
      rm_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" -H 'Content-Type: application/json' -d "$rm_payload" -D "$rpc_session_file" -o "$_TMP_TRANSMISSION_RM" -w '%{http_code}' "$TRANSMISSION_URL" || true)"
      if [[ "$rm_code" == "409" ]]; then
        local rm_session_id
        rm_session_id="$(awk -F': ' 'tolower($1)=="x-transmission-session-id"{gsub("\r","",$2); print $2}' "$rpc_session_file" | tail -n1)"
        rm_code="$(curl -sS -u "${TRANSMISSION_USER}:${TRANSMISSION_PASS}" -H 'Content-Type: application/json' -H "X-Transmission-Session-Id: ${rm_session_id}" -d "$rm_payload" -o "$_TMP_TRANSMISSION_RM" -w '%{http_code}' "$TRANSMISSION_URL" || true)"
      fi
      if [[ "$rm_code" == "200" ]]; then
        log "[transmission] removed id=${tid} hash=${thash} name=${tname}"
      else
        log "[transmission] remove failed id=${tid} http_code=${rm_code}"
      fi
    fi
  done

  rm -f "$rpc_session_file" "$active_hashes"
}

log "arr_cleanup_importblocked start"
cleanup_app "radarr" "$RADARR_URL" "$RADARR_KEY"
cleanup_app "sonarr" "$SONARR_URL" "$SONARR_KEY"
cleanup_transmission
log "arr_cleanup_importblocked done"
