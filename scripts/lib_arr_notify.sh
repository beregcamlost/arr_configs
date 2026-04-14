#!/usr/bin/env bash
# lib_arr_notify.sh — Shared Arr/Emby notification helpers
#
# Provides functions to notify Sonarr, Radarr, Bazarr, and Emby after a
# script modifies media files (codec conversion, subtitle extraction, etc.).
#
# Usage:
#   source "${BASH_SOURCE[0]%/*}/../lib_arr_notify.sh"   # from a subdirectory
#   source "/path/to/automation/scripts/lib_arr_notify.sh"
#
# Requirements:
#   - A `log` function must already be defined by the sourcing script.
#   - Environment variables are read from the already-sourced .env:
#       EMBY_URL, EMBY_API_KEY
#       SONARR_URL, SONARR_KEY
#       RADARR_URL, RADARR_KEY
#       BAZARR_URL, BAZARR_API_KEY
#   - arr_rescan_for_media / bazarr_rescan_for_media also read:
#       BAZARR_DB (global, set by codec manager) or fall back to default path

[[ -n "${_LIB_ARR_NOTIFY_LOADED:-}" ]] && return 0
_LIB_ARR_NOTIFY_LOADED=1

# ---------------------------------------------------------------------------
# emby_refresh_item — search Emby for a media item and trigger metadata refresh
# $1=file_path  (full path to the media file)
# Non-fatal: always returns 0; caller should use || log "WARN: ..."
# ---------------------------------------------------------------------------
emby_refresh_item() {
  local file_path="$1"
  local emby_url="${EMBY_URL:-}" emby_key="${EMBY_API_KEY:-}"
  [[ -z "$emby_url" || -z "$emby_key" ]] && return 0

  local search_name item_id
  search_name="$(basename "${file_path%.*}" | sed 's/ - S[0-9]*E[0-9]*.*//' | sed 's/ ([0-9]*)$//')"

  # URL-encode via jq (handles single quotes and all special characters safely)
  item_id="$(curl -fsS "${emby_url}/Items?api_key=${emby_key}&SearchTerm=$(printf '%s' "$search_name" | jq -sRr @uri)&Recursive=true&Limit=10" \
    </dev/null 2>/dev/null \
    | jq -r --arg path "$file_path" '.Items[] | select(.Path == $path) | .Id' 2>/dev/null \
    | head -1)" || true

  if [[ -z "$item_id" ]]; then
    # Fallback: search by parent folder name (useful for movies)
    local parent_name
    parent_name="$(basename "${file_path%/*}")"
    item_id="$(curl -fsS "${emby_url}/Items?api_key=${emby_key}&SearchTerm=$(printf '%s' "$parent_name" | jq -sRr @uri)&Recursive=true&Limit=10" \
      </dev/null 2>/dev/null \
      | jq -r --arg path "$file_path" '.Items[] | select(.Path == $path) | .Id' 2>/dev/null \
      | head -1)" || true
  fi

  if [[ -n "$item_id" ]]; then
    curl -fsS -X POST \
      "${emby_url}/Items/${item_id}/Refresh?api_key=${emby_key}&Recursive=true&MetadataRefreshMode=Default&ImageRefreshMode=Default" \
      </dev/null >/dev/null 2>&1 || true
    log "EMBY_REFRESH item=$item_id path=$(basename "$file_path")"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# arr_rescan_for_media — trigger Sonarr RescanSeries or Radarr RescanMovie
# $1=media_type  ("series"|"movie")
# $2=bazarr_ref_id  (sonarrEpisodeId for series, radarrId for movies)
# Reads BAZARR_DB global (or falls back to default) for series→seriesId lookup.
# Non-fatal: curl failures are suppressed.
# ---------------------------------------------------------------------------
arr_rescan_for_media() {
  local media_type="$1" ref_id="$2"
  [[ -z "$ref_id" || "$ref_id" == "NULL" ]] && return 0

  local _bazarr_db="${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}"

  if [[ "$media_type" == "series" ]]; then
    local sonarr_url="${SONARR_URL:-http://127.0.0.1:8989/sonarr}"
    local sonarr_key="${SONARR_KEY:-}"
    [[ -z "$sonarr_key" ]] && return 0
    # Resolve sonarrSeriesId from episode ref ID via Bazarr DB
    local series_id
    series_id="$(sqlite3 -cmd ".timeout 5000" "$_bazarr_db" \
      "SELECT sonarrSeriesId FROM table_episodes WHERE sonarrEpisodeId = $ref_id LIMIT 1;" \
      2>/dev/null)" || true
    [[ -z "$series_id" ]] && return 0
    curl -fsS -X POST "${sonarr_url}/api/v3/command" \
      -H "X-Api-Key: ${sonarr_key}" \
      -H "Content-Type: application/json" \
      -d "{\"name\":\"RescanSeries\",\"seriesId\":${series_id}}" \
      </dev/null >/dev/null 2>&1 || true
    log "debug" "Sonarr RescanSeries id=$series_id triggered"
  else
    local radarr_url="${RADARR_URL:-http://127.0.0.1:7878/radarr}"
    local radarr_key="${RADARR_KEY:-}"
    [[ -z "$radarr_key" ]] && return 0
    # ref_id IS the radarrId for movies
    curl -fsS -X POST "${radarr_url}/api/v3/command" \
      -H "X-Api-Key: ${radarr_key}" \
      -H "Content-Type: application/json" \
      -d "{\"name\":\"RescanMovie\",\"movieId\":${ref_id}}" \
      </dev/null >/dev/null 2>&1 || true
    log "debug" "Radarr RescanMovie id=$ref_id triggered"
  fi
}

# ---------------------------------------------------------------------------
# bazarr_rescan_for_media — trigger Bazarr scan-disk for a media item
# $1=media_type  ("series"|"movie")
# $2=bazarr_ref_id  (sonarrEpisodeId for series, radarrId for movies)
# Reads BAZARR_DB global (or falls back to default) for series→seriesId lookup.
# Non-fatal: curl failures are suppressed.
# ---------------------------------------------------------------------------
bazarr_rescan_for_media() {
  local media_type="$1" ref_id="$2"
  local bazarr_url="${BAZARR_URL:-http://127.0.0.1:6767/bazarr}"
  local bazarr_key="${BAZARR_API_KEY:-}"
  [[ -z "$bazarr_key" || -z "$ref_id" || "$ref_id" == "NULL" ]] && return 0

  local _bazarr_db="${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}"
  local endpoint http_code

  if [[ "$media_type" == "series" ]]; then
    local series_id
    series_id="$(sqlite3 -cmd ".timeout 5000" "$_bazarr_db" \
      "SELECT sonarrSeriesId FROM table_episodes WHERE sonarrEpisodeId = $ref_id LIMIT 1;" \
      2>/dev/null)" || true
    [[ -z "$series_id" ]] && return 0
    endpoint="${bazarr_url}/api/series?seriesid=${series_id}&action=scan-disk"
  else
    endpoint="${bazarr_url}/api/movies?radarrid=${ref_id}&action=scan-disk"
  fi

  http_code="$(curl -s -o /dev/null -w '%{http_code}' -X PATCH \
    -H "X-API-KEY: ${bazarr_key}" "$endpoint" \
    </dev/null 2>/dev/null)" || true
  log "debug" "Bazarr scan-disk type=$media_type ref=$ref_id http=$http_code"
}
