#!/usr/bin/env bash
# _post_deembed_reconcile.sh — one-shot reconcile after the de-embed migration.
#
# WHY: the de-embed migration (_deembed_orchestrator.sh) rewrites the library to
# sidecar-only subtitles but only notifies Emby. Radarr/Sonarr keep STALE
# mediainfo (codec/size/embedded-sub flags) and Bazarr never indexed the new
# .es/.en sidecars (so it still thinks subs are "missing" -> wasteful re-work,
# e.g. the debian translation worker re-picking the same file every 5 min).
#
# WHAT: wait until the migration process exits, then do a FULL one-time reconcile:
#   - Radarr : RefreshMovie + RescanMovie   (all movies)
#   - Sonarr : RefreshSeries + RescanSeries (all series)
#   - Bazarr : per-item scan-disk for every movie + show (no whole-lib endpoint)
# Then email Beren.
#
# Idempotent + safe to re-run. Launch DETACHED:
#   setsid bash _post_deembed_reconcile.sh >/dev/null 2>&1 &   (or nohup ... &)
# Created 2026-05-31.
set -uo pipefail

readonly ENV_FILE="/config/berenstuff/.env"
readonly STATE_DIR="/APPBOX_DATA/storage/.transcode-state"
readonly LOG="${STATE_DIR}/post-deembed-reconcile.log"
readonly SENTINEL="${STATE_DIR}/post-deembed-reconcile.done"
readonly SENDMAIL="/config/berenstuff/automation/scripts/bin/sendmail.py"
# Migration process to wait for (override with arg 1). Matched by cmdline too so
# a reused PID running something else cannot fool the wait.
readonly MIG_PID="${1:-57614}"
readonly MIG_MATCH="_deembed_orchestrator"
readonly BAZARR_SLEEP="${BAZARR_SLEEP:-0.2}"   # throttle between Bazarr calls

log() { printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

mkdir -p "$STATE_DIR"
log "=== post-deembed reconcile started (waiting on PID ${MIG_PID} [$MIG_MATCH]) ==="

# ---- 1. Wait for the migration to finish -----------------------------------
while ps -p "$MIG_PID" -o args= 2>/dev/null | grep -q "$MIG_MATCH"; do
  sleep 300
done
log "migration PID ${MIG_PID} no longer running — starting reconcile"

# ---- 2. Load env -----------------------------------------------------------
# shellcheck source=/dev/null
set -a; . "$ENV_FILE"; set +a
: "${RADARR_URL:?}" "${RADARR_KEY:?}" "${SONARR_URL:?}" "${SONARR_KEY:?}"
: "${BAZARR_URL:?}" "${BAZARR_API_KEY:?}" "${BAZARR_DB:?}"

arr_cmd() {  # $1=base url  $2=key  $3=json body
  curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
    -X POST "${1}/api/v3/command" \
    -H "X-Api-Key: ${2}" -H "Content-Type: application/json" -d "${3}"
}

# ---- 3. Radarr + Sonarr: full refresh then rescan --------------------------
log "Radarr  RefreshMovie  -> $(arr_cmd "$RADARR_URL" "$RADARR_KEY" '{"name":"RefreshMovie"}')"
log "Radarr  RescanMovie   -> $(arr_cmd "$RADARR_URL" "$RADARR_KEY" '{"name":"RescanMovie"}')"
log "Sonarr  RefreshSeries -> $(arr_cmd "$SONARR_URL" "$SONARR_KEY" '{"name":"RefreshSeries"}')"
log "Sonarr  RescanSeries  -> $(arr_cmd "$SONARR_URL" "$SONARR_KEY" '{"name":"RescanSeries"}')"

# ---- 4. Bazarr: scan-disk per item -----------------------------------------
bazarr_scan() {  # $1=movies|series  $2=id-param  $3=id
  curl -s -o /dev/null -w "%{http_code}" --max-time 30 -X PATCH \
    -H "X-API-KEY: ${BAZARR_API_KEY}" \
    "${BAZARR_URL}/api/${1}?${2}=${3}&action=scan-disk"
}

m_ok=0; m_err=0
log "Bazarr scan-disk: movies (table_movies.radarrId)"
while IFS= read -r id; do
  [ -z "$id" ] && continue
  code=$(bazarr_scan movies radarrid "$id")
  if [ "$code" = "200" ] || [ "$code" = "204" ]; then m_ok=$((m_ok+1)); else m_err=$((m_err+1)); log "  movie $id -> $code"; fi
  sleep "$BAZARR_SLEEP"
done < <(sqlite3 "$BAZARR_DB" "SELECT radarrId FROM table_movies WHERE radarrId IS NOT NULL;")
log "Bazarr movies done: ok=${m_ok} err=${m_err}"

s_ok=0; s_err=0
log "Bazarr scan-disk: series (table_shows.sonarrSeriesId)"
while IFS= read -r id; do
  [ -z "$id" ] && continue
  code=$(bazarr_scan series seriesid "$id")
  if [ "$code" = "200" ] || [ "$code" = "204" ]; then s_ok=$((s_ok+1)); else s_err=$((s_err+1)); log "  series $id -> $code"; fi
  sleep "$BAZARR_SLEEP"
done < <(sqlite3 "$BAZARR_DB" "SELECT sonarrSeriesId FROM table_shows WHERE sonarrSeriesId IS NOT NULL;")
log "Bazarr series done: ok=${s_ok} err=${s_err}"

# ---- 5. Done + email -------------------------------------------------------
date '+%Y-%m-%d %H:%M:%S' > "$SENTINEL"
log "=== reconcile COMPLETE (movies ok=${m_ok}/err=${m_err}, series ok=${s_ok}/err=${s_err}) ==="

if [ -f "$SENDMAIL" ]; then
  python3 "$SENDMAIL" --to antoniojose.figueroaf@gmail.com \
    --subject "mubuntu: reconcile arr/bazarr TERMINADO (post de-embed)" \
    --body "Reconcile completo tras la migracion de-embed.

Radarr/Sonarr: refresh + rescan de toda la libreria (mediainfo al dia).
Bazarr scan-disk: peliculas ok=${m_ok} err=${m_err}; series ok=${s_ok} err=${s_err}.

Esto tambien cierra el loop del worker (Bazarr ya ve los sidecars .es).
Log: ${LOG}

-- Claudia" >> "$LOG" 2>&1 || log "sendmail failed (non-fatal)"
fi
