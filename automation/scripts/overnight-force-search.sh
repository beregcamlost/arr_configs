#!/usr/bin/env bash
# overnight-force-search.sh — Force-search Bazarr for all missing-es items.
# Runs ON mubuntu (invoked via ssh from WSL watchdog setup).
# Idempotent: re-running just re-fires searches (safe).
set -euo pipefail

readonly LOG_PREFIX="[force-search]"
readonly LOG_FILE="/config/berenstuff/automation/logs/overnight_force_search.log"
readonly BAZARR_URL="http://127.0.0.1:6767/bazarr"
readonly BAZARR_API_KEY="209ddfabdea879b2bbff75b5dce0eccd"
readonly BAZARR_DB="/opt/bazarr/data/db/bazarr.db"
readonly STAGGER_SECS=2

log() { printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*" | tee -a "$LOG_FILE"; }

mkdir -p "$(dirname "$LOG_FILE")"

log "=== overnight force-search starting ==="

# --- Episodes: get distinct sonarrSeriesId ---
mapfile -t SERIES_IDS < <(sqlite3 "$BAZARR_DB" \
  "SELECT DISTINCT sonarrSeriesId FROM table_episodes
   WHERE missing_subtitles LIKE '%es%' AND path IS NOT NULL AND path != ''
   ORDER BY sonarrSeriesId;" 2>/dev/null)

log "Found ${#SERIES_IDS[@]} series with missing-es episodes"

ep_ok=0
ep_fail=0
for sid in "${SERIES_IDS[@]}"; do
    [[ -z "$sid" ]] && continue
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X PATCH "${BAZARR_URL}/api/series" \
        -H "X-API-KEY: ${BAZARR_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "{\"seriesid\": ${sid}, \"action\": \"search-missing\"}")
    if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
        log "  series ${sid} -> HTTP ${http_code} OK"
        (( ep_ok++ )) || true
    else
        log "  series ${sid} -> HTTP ${http_code} FAIL"
        (( ep_fail++ )) || true
    fi
    sleep "$STAGGER_SECS"
done

log "Episodes done: ok=${ep_ok} fail=${ep_fail}"

# --- Movies: get distinct radarrId ---
mapfile -t MOVIE_IDS < <(sqlite3 "$BAZARR_DB" \
  "SELECT DISTINCT radarrId FROM table_movies
   WHERE missing_subtitles LIKE '%es%' AND path IS NOT NULL AND path != ''
   ORDER BY radarrId;" 2>/dev/null)

log "Found ${#MOVIE_IDS[@]} movie(s) with missing-es"

mov_ok=0
mov_fail=0
for mid in "${MOVIE_IDS[@]}"; do
    [[ -z "$mid" ]] && continue
    http_code=$(curl -s -o /dev/null -w "%{http_code}" \
        -X PATCH "${BAZARR_URL}/api/movies" \
        -H "X-API-KEY: ${BAZARR_API_KEY}" \
        -H "Content-Type: application/json" \
        -d "{\"radarrid\": ${mid}, \"action\": \"search-missing\"}")
    if [[ "$http_code" -ge 200 && "$http_code" -lt 300 ]]; then
        log "  movie ${mid} -> HTTP ${http_code} OK"
        (( mov_ok++ )) || true
    else
        log "  movie ${mid} -> HTTP ${http_code} FAIL"
        (( mov_fail++ )) || true
    fi
    sleep "$STAGGER_SECS"
done

log "Movies done: ok=${mov_ok} fail=${mov_fail}"
log "=== force-search complete: series_ok=${ep_ok} series_fail=${ep_fail} movies_ok=${mov_ok} movies_fail=${mov_fail} ==="
