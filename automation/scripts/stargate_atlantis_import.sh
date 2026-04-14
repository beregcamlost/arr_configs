#!/bin/bash
set -euo pipefail

# ---------------------------------------------------------------------------
# stargate_atlantis_import.sh
#
# Polls until the Stargate Atlantis torrent is fully downloaded, then
# triggers a Sonarr DownloadedEpisodesScan and verifies the import.
#
# Usage:
#   SONARR_KEY=<key> ./stargate_atlantis_import.sh
#   Or: source /config/berenstuff/.env && ./stargate_atlantis_import.sh
# ---------------------------------------------------------------------------

readonly TORRENT_HASH="939019E6D7565E289BA9F0F51FB37B8DD9BB0E4C"
readonly COMPLETED_PATH="/APPBOX_DATA/apps/transmission.vhscave.appboxes.co/torrents/completed/sonarr/Stargate.Atlantis.S01-S05.BluRay.10Bit.1080p.DD5.1.H265-d3g"
readonly SERIES_ID=204
readonly EXPECTED_SEASONS=5
readonly POLL_INTERVAL=60
readonly MAX_WAIT_SECONDS=14400  # 4 hours
readonly DOWNLOAD_CLIENT_ID=1
readonly EXPECTED_EPISODES=99    # 5 seasons ~99 episodes
readonly IMPORT_MAX_SECONDS=900   # 15 minutes — 99 episodes can take a while

readonly LOG_PREFIX="[stargate-import]"

log() {
    local ts
    printf -v ts '%(%Y-%m-%d %H:%M:%S)T' -1
    printf '%s %s %s\n' "$ts" "$LOG_PREFIX" "$*"
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Resolve SONARR_KEY
# ---------------------------------------------------------------------------
if [[ -z "${SONARR_KEY:-}" ]]; then
    if [[ -f /config/berenstuff/.env ]]; then
        # shellcheck source=/dev/null
        source /config/berenstuff/.env
    fi
fi

[[ -z "${SONARR_KEY:-}" ]] && die "SONARR_KEY is not set. Export it or ensure /config/berenstuff/.env defines it."

readonly SONARR_API="${SONARR_URL:-http://127.0.0.1:8989/sonarr}/api/v3"

# ---------------------------------------------------------------------------
# Check if download is complete:
#   - completed path exists
#   - exactly EXPECTED_SEASONS Season* subdirectories present
#   - zero .part files anywhere inside
# ---------------------------------------------------------------------------
_last_season_count=0

is_download_complete() {
    [[ -d "$COMPLETED_PATH" ]] || return 1

    local -a season_dirs=()
    local d
    for d in "$COMPLETED_PATH"/[Ss][0-9]* "$COMPLETED_PATH"/[Ss]eason*; do
        [[ -d "$d" ]] && season_dirs+=("$d")
    done
    _last_season_count=${#season_dirs[@]}
    (( _last_season_count >= EXPECTED_SEASONS )) || return 1

    local part_count
    part_count=$(find "$COMPLETED_PATH" -type f -name "*.part" 2>/dev/null | wc -l)
    (( part_count == 0 )) || return 1

    return 0
}

# ---------------------------------------------------------------------------
# Sonarr API helpers — all curl calls use </dev/null to avoid stdin issues
# ---------------------------------------------------------------------------
sonarr_post() {
    local endpoint="$1"
    local body="$2"
    local response

    response=$(curl --silent --show-error --fail \
        --max-time 30 \
        -X POST \
        -H "X-Api-Key: ${SONARR_KEY}" \
        -H "Content-Type: application/json" \
        -d "$body" \
        "${SONARR_API}${endpoint}" </dev/null) || {
        log "WARNING: Sonarr POST ${endpoint} failed"
        return 1
    }

    printf '%s' "$response"
}

sonarr_get() {
    local endpoint="$1"
    local response

    response=$(curl --silent --show-error --fail \
        --max-time 30 \
        -H "X-Api-Key: ${SONARR_KEY}" \
        "${SONARR_API}${endpoint}" </dev/null) || {
        log "WARNING: Sonarr GET ${endpoint} failed"
        return 1
    }

    printf '%s' "$response"
}

sonarr_put() {
    local endpoint="$1"
    local body="$2"
    local response

    response=$(curl --silent --show-error --fail \
        --max-time 30 \
        -X PUT \
        -H "X-Api-Key: ${SONARR_KEY}" \
        -H "Content-Type: application/json" \
        -d "$body" \
        "${SONARR_API}${endpoint}" </dev/null) || {
        log "WARNING: Sonarr PUT ${endpoint} failed"
        return 1
    }

    printf '%s' "$response"
}

# ---------------------------------------------------------------------------
# Trigger the Sonarr DownloadedEpisodesScan
# ---------------------------------------------------------------------------
trigger_import() {
    log "Triggering Sonarr DownloadedEpisodesScan for path: ${COMPLETED_PATH}"

    local body
    body=$(printf '{"name":"DownloadedEpisodesScan","path":"%s"}' "$COMPLETED_PATH")

    local response
    response=$(sonarr_post "/command" "$body") || die "Failed to trigger Sonarr import command."

    log "Sonarr command accepted. Response: ${response}"
}

# ---------------------------------------------------------------------------
# Verify import: compare episode file count before vs after
# ---------------------------------------------------------------------------
get_episode_file_count() {
    local response
    response=$(sonarr_get "/episodefile?seriesId=${SERIES_ID}") || {
        printf '0'
        return
    }

    local count
    count=$(printf '%s' "$response" | jq 'length') || count=0
    printf '%s' "$count"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    log "Starting Stargate Atlantis import watcher."
    log "Torrent hash : ${TORRENT_HASH}"
    log "Watch path   : ${COMPLETED_PATH}"
    log "Sonarr series: ${SERIES_ID}"
    log "Max wait     : $((MAX_WAIT_SECONDS / 3600)) hours"

    local elapsed=0

    # ------------------------------------------------------------------
    # Phase 1: Poll until download is complete
    # ------------------------------------------------------------------
    if is_download_complete; then
        log "Download already complete. Proceeding immediately."
    else
        log "Waiting for download to complete (polling every ${POLL_INTERVAL}s)..."

        while ! is_download_complete; do
            if (( elapsed >= MAX_WAIT_SECONDS )); then
                die "Timeout after $((MAX_WAIT_SECONDS / 3600)) hours — torrent still not complete. Aborting."
            fi

            log "Not ready yet (${_last_season_count}/${EXPECTED_SEASONS} seasons found, elapsed: ${elapsed}s). Sleeping ${POLL_INTERVAL}s..."
            sleep "$POLL_INTERVAL"
            elapsed=$(( elapsed + POLL_INTERVAL ))
        done

        log "Download complete after ${elapsed}s."
    fi

    # ------------------------------------------------------------------
    # Phase 2: Capture baseline episode file count
    # ------------------------------------------------------------------
    local before_count
    before_count=$(get_episode_file_count)
    log "Episode files in Sonarr before import: ${before_count}"

    # ------------------------------------------------------------------
    # Phase 3: Trigger import
    # ------------------------------------------------------------------
    trigger_import

    # ------------------------------------------------------------------
    # Phase 4: Poll until Sonarr finishes importing (up to 15 min)
    # ------------------------------------------------------------------
    log "Waiting for Sonarr to process the import..."
    local import_elapsed=0
    local after_count=0

    while (( import_elapsed < IMPORT_MAX_SECONDS )); do
        sleep 30
        import_elapsed=$(( import_elapsed + 30 ))

        after_count=$(get_episode_file_count)
        log "Import progress: ${after_count}/${EXPECTED_EPISODES} episode files (${import_elapsed}s elapsed)"

        # If we've reached the expected count, import is done
        if (( after_count >= EXPECTED_EPISODES )); then
            log "All ${EXPECTED_EPISODES} episodes imported successfully!"
            break
        fi

        # If count is still growing, keep waiting
        # If it stalled for 2 minutes, Sonarr may need another nudge
        if (( import_elapsed % 120 == 0 && after_count < EXPECTED_EPISODES )); then
            log "Import seems stalled at ${after_count}. Re-triggering scan..."
            trigger_import
        fi
    done

    # ------------------------------------------------------------------
    # Phase 5: Re-enable removeCompletedDownloads
    # ------------------------------------------------------------------
    log "Re-enabling removeCompletedDownloads on Sonarr download client..."
    local dc_json dc_reenabled=false
    dc_json=$(sonarr_get "/downloadclient/${DOWNLOAD_CLIENT_ID}") || {
        log "ERROR: Could not fetch download client config. Re-enable removeCompletedDownloads MANUALLY!"
        log "Sonarr > Settings > Download Clients > Transmission > Completed Download Handling"
    }

    if [[ -n "${dc_json:-}" ]]; then
        local updated_json
        updated_json=$(printf '%s' "$dc_json" | jq '.removeCompletedDownloads = true') || {
            log "ERROR: Failed to parse download client JSON. Re-enable removeCompletedDownloads MANUALLY!"
            log "Sonarr > Settings > Download Clients > Transmission > Completed Download Handling"
            updated_json=""
        }

        if [[ -n "${updated_json:-}" ]]; then
            sonarr_put "/downloadclient/${DOWNLOAD_CLIENT_ID}" "$updated_json" && {
                log "removeCompletedDownloads re-enabled successfully."
                dc_reenabled=true
            } || log "ERROR: Failed to re-enable removeCompletedDownloads. Do it manually in Sonarr settings!"
        fi
    fi

    # ------------------------------------------------------------------
    # Final summary
    # ------------------------------------------------------------------
    log "=========================================="
    log "SUMMARY"
    log "  Before: ${before_count} episode files"
    log "  After:  ${after_count} episode files"
    log "  Imported: $(( after_count - before_count )) new episodes"
    if (( after_count >= EXPECTED_EPISODES )); then
        log "  Status: ALL SEASONS IMPORTED"
    else
        log "  Status: PARTIAL — check Sonarr manually"
        log "  Sonarr Activity: ${SONARR_URL:-http://127.0.0.1:8989/sonarr}/activity/queue"
    fi
    if [[ "$dc_reenabled" == true ]]; then
        log "  removeCompletedDownloads: re-enabled"
    else
        log "  removeCompletedDownloads: NEEDS MANUAL RE-ENABLE"
    fi
    log "=========================================="
}

main "$@"
