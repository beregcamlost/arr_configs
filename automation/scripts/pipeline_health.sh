#!/usr/bin/env bash
# pipeline_health.sh — Mubuntu pipeline health monitor
#
# PURPOSE:
#   Runs every 15 min (via cron) and checks the health of the mubuntu Emby
#   automation pipeline across seven dimensions: orchestrator freshness, state-DB
#   reachability, Ollama endpoint liveness, stale flock files, oversized log
#   files, disk-space headroom, and intake webhook liveness.  Severity is graded
#   OK / WARN / ALARM.
#   On ALARM (or with --force-discord) a summary is posted to Discord.
#   On consecutive WARN the summary is also posted (tracked via state file).
#   Exit 0 = all OK, 1 = any WARN, 2 = any ALARM.
#
# MANUAL INVOCATION:
#   bash pipeline_health.sh               # normal run (Discord only on ALARM)
#   bash pipeline_health.sh --force-discord  # always post to Discord (testing)
#   bash pipeline_health.sh --no-discord  # never post to Discord (dry-run)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly LOG_PREFIX="[pipeline_health]"
readonly STATE_FILE="/tmp/pipeline_health.state"
readonly ENV_FILE="/config/berenstuff/.env"

# State DBs to health-check
# Phase 6 I-A: consolidated pipeline.db replaces individual state DBs
readonly PIPELINE_DB_PATH="${PIPELINE_DB:-/APPBOX_DATA/storage/pipeline.db}"
readonly -a STATE_DBS=(
    "${PIPELINE_DB_PATH}"
    "/APPBOX_DATA/storage/.bazarr-snapshot/bazarr.db"
    "/opt/bazarr/data/db/bazarr.db"
)

readonly LOCK_DIR="/tmp/sub-translate-locks"
readonly LOG_DIR="/config/berenstuff/automation/logs"
readonly STALE_LOCK_HOURS=1
readonly LOG_SIZE_LIMIT_MB=100
readonly DISK_FREE_PCT_WARN=10
readonly PIPELINE_FRESHNESS_MIN=15
readonly INTAKE_WEBHOOK_HEALTH_URL="http://127.0.0.1:${INTAKE_WEBHOOK_PORT:-8765}/health"

# Parse flags
FORCE_DISCORD=false
NO_DISCORD=false
for arg in "$@"; do
    case "$arg" in
        --force-discord) FORCE_DISCORD=true ;;
        --no-discord)    NO_DISCORD=true ;;
    esac
done

log() { printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*" >&2; }

# Metrics helper (fail-soft)
# shellcheck source=lib_metrics.sh
source "${SCRIPT_DIR}/lib_metrics.sh" || true

# ---------------------------------------------------------------------------
# Atomic .env source: copy → source → delete to avoid partial reads
# ---------------------------------------------------------------------------
load_env() {
    local tmp_env
    tmp_env="$(mktemp /tmp/.env.XXXXXX)"
    cp "$ENV_FILE" "$tmp_env"
    # shellcheck source=/dev/null
    source "$tmp_env"
    rm -f "$tmp_env"
}

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
OVERALL_SEVERITY="OK"   # OK / WARN / ALARM
declare -a REPORT_LINES=()

record() {
    local severity="$1"
    local message="$2"
    REPORT_LINES+=("${severity}: ${message}")
    if [[ "$severity" == "ALARM" ]]; then
        OVERALL_SEVERITY="ALARM"
    elif [[ "$severity" == "WARN" && "$OVERALL_SEVERITY" != "ALARM" ]]; then
        OVERALL_SEVERITY="WARN"
    fi
    printf '[%s] %s\n' "$severity" "$message"
}

# ---------------------------------------------------------------------------
# Check 1: Orchestrator freshness
# ---------------------------------------------------------------------------
check_planner_freshness() {
    # El planner y el audit escriben su resultado en manager.log, NO en los cron-*.log
    # (esos solo capturan stderr, por eso llevan meses vacios y parecian muertos).
    # Aqui se mira la fuente real: si el audit o el plan llevan mas de 36 h sin
    # completar, la cola dejo de alimentarse y nadie se entera.
    local mlog="/APPBOX_DATA/storage/.transcode-state-media/manager.log"
    if [[ ! -f "$mlog" ]]; then
        record "WARN" "Planner: manager.log no existe en ${mlog}"
        return
    fi
    local now_epoch stage line ts age_h
    now_epoch="$(date +%s)"
    for stage in "Audit completed" "Plan completed"; do
        line="$(grep -a "\[info\] ${stage}" "$mlog" | tail -1 || true)"
        if [[ -z "$line" ]]; then
            record "WARN" "Planner: nunca se vio '${stage}' en manager.log"
            continue
        fi
        ts="$(printf '%s' "$line" | awk '{print $1" "$2}')"
        local ts_epoch
        ts_epoch="$(date -d "$ts" +%s 2>/dev/null || echo 0)"
        if [[ "$ts_epoch" -eq 0 ]]; then
            record "WARN" "Planner: no pude interpretar la fecha de '${stage}': ${ts}"
            continue
        fi
        age_h=$(( (now_epoch - ts_epoch) / 3600 ))
        if [[ "$age_h" -gt 36 ]]; then
            record "ALARM" "Planner: '${stage}' hace ${age_h} h (ultimo: ${ts}) — la cola no se esta alimentando"
        else
            record "OK" "Planner: '${stage}' hace ${age_h} h"
        fi
    done
}

check_sqlite_backups() {
    # El backup corre cada 3 dias. Durante mucho tiempo NO respaldo nada por un doble
    # flock sobre el mismo lockfile y el log solo decia "Already running", asi que la
    # unica senal fiable es la fecha del .bkp en disco, no el log.
    local cur="/config/berenstuff/arr-backups/current"
    if [[ ! -d "$cur" ]]; then
        record "ALARM" "Backups: no existe ${cur} — nunca se ha respaldado"
        return
    fi
    local newest age_h count
    newest="$(find "$cur" -name '*.bkp' -printf '%T@
' 2>/dev/null | sort -rn | head -1)"
    count="$(find "$cur" -name '*.bkp' 2>/dev/null | wc -l)"
    if [[ -z "$newest" ]]; then
        record "ALARM" "Backups: no hay ningun .bkp en ${cur}"
        return
    fi
    age_h=$(( ( $(date +%s) - ${newest%.*} ) / 3600 ))
    if [[ "$age_h" -gt 96 ]]; then
        record "ALARM" "Backups: el mas reciente tiene ${age_h} h (el cron corre cada 72 h)"
    else
        record "OK" "Backups: ${count} DBs, el mas reciente hace ${age_h} h"
    fi
}

check_conversion_queue() {
    # Una cola que no baja es tan mala senal como un job muerto: avisa cuando el item
    # mas viejo lleva demasiado esperando, que es lo que un usuario notaria en Emby.
    local db="/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
    [[ -f "$db" ]] || { record "WARN" "Cola: no existe ${db}"; return; }
    local pending oldest_h
    pending="$(sqlite3 -cmd '.timeout 5000' "file:${db}?mode=ro"         "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1;" 2>/dev/null || echo "")"
    [[ -z "$pending" ]] && { record "WARN" "Cola: no pude leer conversion_plan"; return; }
    if [[ "$pending" -eq 0 ]]; then
        record "OK" "Cola de conversion vacia"
        return
    fi
    oldest_h="$(sqlite3 -cmd '.timeout 5000' "file:${db}?mode=ro"         "SELECT CAST((julianday('now') - julianday(MIN(plan_ts))) * 24 AS INTEGER) FROM conversion_plan WHERE eligible=1;" 2>/dev/null || echo 0)"
    if [[ "${oldest_h:-0}" -gt 48 ]]; then
        record "ALARM" "Cola: ${pending} pendientes, el mas viejo lleva ${oldest_h} h sin procesarse"
    else
        record "OK" "Cola: ${pending} pendientes, el mas viejo lleva ${oldest_h:-0} h"
    fi
}

check_orchestrator_freshness() {
    # 2026-06-17: pipeline_state went vestigial after the flock-lane refactor
    # (last write 2026-05-01). Freshness is now derived from media_pipeline.log,
    # which the fast lane appends to every 5 min.
    local logf="${LOG_DIR}/media_pipeline.log"
    if [[ ! -f "$logf" ]]; then
        record "WARN" "media_pipeline.log missing: $logf"
        return
    fi
    local last_epoch now_epoch age_min
    last_epoch="$(stat -c %Y "$logf" 2>/dev/null || echo 0)"
    now_epoch="$(date +%s)"
    age_min=$(( (now_epoch - last_epoch) / 60 ))
    if (( age_min > PIPELINE_FRESHNESS_MIN )); then
        record "WARN" "Pipeline stale: media_pipeline.log idle ${age_min}m (threshold ${PIPELINE_FRESHNESS_MIN}m)"
    else
        record "OK" "Pipeline fresh: media_pipeline.log written ${age_min}m ago"
    fi
}

# ---------------------------------------------------------------------------
# Check 2: State DBs reachable
# ---------------------------------------------------------------------------
check_state_dbs() {
    local any_failed=false
    for db in "${STATE_DBS[@]}"; do
        if [[ ! -f "$db" ]]; then
            record "ALARM" "DB missing: $db"
            any_failed=true
            continue
        fi
        local result=""
        local attempt
        # Retry with a busy-timeout so a transient writer lock (media_pipeline
        # mid-write) does not trip a false ALARM. A lock is contention, not
        # corruption -> WARN (Discord only fires on consecutive WARN).
        for attempt in 1 2 3; do
            result="$(sqlite3 -cmd ".timeout 8000" "$db" "PRAGMA quick_check;" 2>&1 || true)"
            if [[ "$result" == "ok" ]]; then
                break
            fi
            if [[ "$result" != *"is locked"* && "$result" != *"is busy"* ]]; then
                break
            fi
            sleep 2
        done
        if [[ "$result" != "ok" ]]; then
            if [[ "$result" == *"is locked"* || "$result" == *"is busy"* ]]; then
                record "WARN" "DB busy (transient lock), quick_check skipped ($db): $result"
            else
                record "ALARM" "DB check failed ($db): $result"
            fi
            any_failed=true
        fi
    done
    if [[ "$any_failed" == "false" ]]; then
        record "OK" "All ${#STATE_DBS[@]} state DBs reachable and healthy"
    fi
}

# ---------------------------------------------------------------------------
# Check 3: Ollama endpoints
# ---------------------------------------------------------------------------
check_gpu3090_activity() {
    # La 3090 vive en berentendo, que se apaga y a veces esta ocupada jugando. Que no
    # este disponible NO es una falla — lo es que haya trabajo pesado esperandola desde
    # hace dias sin que nadie lo note. Se mide por claims, que es como participa de
    # verdad en el pipeline (claimed_by='gpu3090@<epoch>').
    local db="/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
    [[ -f "$db" ]] || { record "WARN" "3090: no existe ${db}"; return; }
    local claimed heavy
    claimed="$(sqlite3 -cmd '.timeout 5000' "file:${db}?mode=ro"         "SELECT COUNT(*) FROM conversion_plan WHERE claimed_by LIKE 'gpu3090@%';" 2>/dev/null || echo "")"
    heavy="$(sqlite3 -cmd '.timeout 5000' "file:${db}?mode=ro"         "SELECT COUNT(*) FROM conversion_plan WHERE eligible=1 AND priority>=10 AND (claimed_by IS NULL OR claimed_by='');" 2>/dev/null || echo "")"
    if [[ -z "$claimed" || -z "$heavy" ]]; then
        record "WARN" "3090: no pude leer conversion_plan"
        return
    fi
    if [[ "$heavy" -gt 0 && "$claimed" -eq 0 ]]; then
        record "WARN" "3090: ${heavy} transcodes pesados esperando y ningun claim activo — berentendo lleva rato apagada"
    else
        record "OK" "3090: ${claimed} en proceso, ${heavy} transcodes pesados en espera"
    fi
}

check_ollama_endpoints() {
    # OJO (2026-08-21): antes este bucle chequeaba "WSL-GPU" y "Debian-CPU" como si
    # fueran dos servicios, pero OLLAMA_BASE_URL y DEBIAN_OLLAMA_URL apuntan a LA MISMA
    # maquina (la de debian). El "OK: Ollama WSL-GPU UP" que se reporto durante
    # meses era el shim de debian respondiendo dos veces. La 3090 no es alcanzable por
    # HTTP desde mubuntu — es ella quien viene a buscar trabajo — asi que su salud se
    # mide por actividad de claims en check_gpu3090_activity, no por un ping.
    local debian_url="${DEBIAN_OLLAMA_URL:-${OLLAMA_BASE_URL:-}}"

    for pair in "Debian-CPU:${debian_url}"; do
        local label="${pair%%:*}"
        local url="${pair#*:}"
        if [[ -z "$url" ]]; then
            record "WARN" "Ollama ${label}: URL not set in .env"
            continue
        fi
        if curl -fsS --max-time 3 "${url}/api/tags" > /dev/null 2>&1; then
            record "OK" "Ollama ${label} (${url}): UP"
        else
            record "ALARM" "Ollama ${label} (${url}): DOWN"
        fi
    done
}

# ---------------------------------------------------------------------------
# Check 4: Stale flock files
# ---------------------------------------------------------------------------
check_stale_flocks() {
    if [[ ! -d "$LOCK_DIR" ]]; then
        record "OK" "No flock lock dir at $LOCK_DIR"
        return
    fi

    local -a stale=()
    while IFS= read -r -d '' f; do
        stale+=("$f")
    done < <(find "$LOCK_DIR" -maxdepth 1 -type f -mmin "+$((STALE_LOCK_HOURS * 60))" -print0 2>/dev/null)

    if (( ${#stale[@]} > 0 )); then
        record "WARN" "Stale flock files (>${STALE_LOCK_HOURS}h): ${stale[*]}"
    else
        record "OK" "No stale flock files in $LOCK_DIR"
    fi
}

# ---------------------------------------------------------------------------
# Check 5: Log file sizes
# ---------------------------------------------------------------------------
check_log_sizes() {
    local limit_bytes=$(( LOG_SIZE_LIMIT_MB * 1024 * 1024 ))
    local -a oversized=()
    while IFS= read -r -d '' f; do
        local size
        size="$(stat -c%s "$f" 2>/dev/null || echo 0)"
        if (( size > limit_bytes )); then
            oversized+=("$(basename "$f"):$(( size / 1024 / 1024 ))MB")
        fi
    done < <(find "$LOG_DIR" -maxdepth 1 -name "*.log" -type f -print0 2>/dev/null)

    if (( ${#oversized[@]} > 0 )); then
        record "WARN" "Oversized logs (>${LOG_SIZE_LIMIT_MB}MB): ${oversized[*]}"
    else
        record "OK" "All logs under ${LOG_SIZE_LIMIT_MB}MB"
    fi
}

# ---------------------------------------------------------------------------
# Check 6: Disk space
# ---------------------------------------------------------------------------

check_disk_space() {
    local -a mounts=("/APPBOX_DATA" "/config")
    for mount in "${mounts[@]}"; do
        if ! mountpoint -q "$mount" 2>/dev/null && [[ ! -d "$mount" ]]; then
            record "WARN" "Disk mount missing: $mount"
            continue
        fi
        local pct_used
        pct_used="$(df "$mount" 2>/dev/null | awk 'NR==2{gsub(/%/,"",$5); print $5}')"
        local pct_free=$(( 100 - ${pct_used:-100} ))
        if (( pct_free < DISK_FREE_PCT_WARN )); then
            record "ALARM" "Disk ${mount}: only ${pct_free}% free (used ${pct_used}%)"
        else
            record "OK" "Disk ${mount}: ${pct_free}% free"
        fi
    done
}

# ---------------------------------------------------------------------------
# Check 7: Intake webhook liveness
# ---------------------------------------------------------------------------
check_intake_webhook() {
    # Only warn if the PID file exists (i.e., the receiver has been deployed
    # and is expected to be running).  This avoids false alarms before the
    # cron entry is enabled.
    local pid_file="/tmp/intake_webhook.pid"
    if [[ ! -f "$pid_file" ]]; then
        # Receiver has never been started — skip silently
        return
    fi

    if curl -fsS --max-time 3 "${INTAKE_WEBHOOK_HEALTH_URL}" > /dev/null 2>&1; then
        record "OK" "Intake webhook (${INTAKE_WEBHOOK_HEALTH_URL}): UP"
    else
        record "WARN" "Intake webhook (${INTAKE_WEBHOOK_HEALTH_URL}): DOWN (pid file present but not responding)"
    fi
}

# ---------------------------------------------------------------------------
# Discord notification
# ---------------------------------------------------------------------------
post_discord() {
    local webhook_url="${DISCORD_WEBHOOK_URL:-}"
    if [[ -z "$webhook_url" ]]; then
        log "DISCORD_WEBHOOK_URL not set — skipping Discord post"
        return
    fi

    local timestamp
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

    local body
    body="$(printf '🚨 mubuntu pipeline health: %s\n' "$OVERALL_SEVERITY")"
    for line in "${REPORT_LINES[@]}"; do
        body+="$(printf '\n- %s' "$line")"
    done
    body+="$(printf '\nTime: %s' "$timestamp")"

    # Discord limit: 2000 chars per message. Truncate body if over 1950 chars.
    if (( ${#body} > 1950 )); then
        body="${body:0:1950}
...(truncated)"
    fi

    local payload
    payload="$(printf '{"content": %s}' "$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")"

    local http_code
    http_code="$(curl -s -o /dev/null -w '%{http_code}' -X POST -H "Content-Type: application/json" \
        -d "$payload" "$webhook_url" 2>/dev/null)" || { log "Discord post: curl error"; return; }
    if [[ "$http_code" == "204" ]] || [[ "$http_code" == "200" ]]; then
        log "Discord post OK (${http_code})"
    else
        log "Discord post failed (HTTP ${http_code})"
    fi
}

# ---------------------------------------------------------------------------
# Consecutive-WARN tracking
# ---------------------------------------------------------------------------
should_post_warn() {
    local prev_severity="OK"
    if [[ -f "$STATE_FILE" ]]; then
        prev_severity="$(cat "$STATE_FILE" 2>/dev/null || echo OK)"
    fi
    printf '%s\n' "$OVERALL_SEVERITY" > "$STATE_FILE"
    # Post if this is the second consecutive WARN
    [[ "$prev_severity" == "WARN" && "$OVERALL_SEVERITY" == "WARN" ]]
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
main() {
    load_env

    local _health_run_id
    _health_run_id="$(metrics_run_start "health" 2>/dev/null)" || _health_run_id=""

    check_orchestrator_freshness
    check_planner_freshness
    check_sqlite_backups
    check_conversion_queue
    check_state_dbs
    check_ollama_endpoints
    check_gpu3090_activity
    check_stale_flocks
    check_log_sizes
    check_disk_space
    check_intake_webhook

    log "Overall severity: $OVERALL_SEVERITY"

    # Map severity to exit code for metrics
    local _health_exit_code
    case "$OVERALL_SEVERITY" in
        OK)    _health_exit_code=0 ;;
        WARN)  _health_exit_code=1 ;;
        ALARM) _health_exit_code=2 ;;
        *)     _health_exit_code=3 ;;
    esac
    if [[ -n "$_health_run_id" ]]; then
        metrics_run_end "$_health_run_id" "$_health_exit_code" \
            0 0 "{\"severity\":\"${OVERALL_SEVERITY}\"}" 2>/dev/null || true
    fi

    if [[ "$NO_DISCORD" == "true" ]]; then
        : # skip Discord unconditionally
    elif [[ "$FORCE_DISCORD" == "true" ]]; then
        post_discord
    elif [[ "$OVERALL_SEVERITY" == "ALARM" ]]; then
        post_discord
    elif [[ "$OVERALL_SEVERITY" == "WARN" ]]; then
        if should_post_warn; then
            post_discord
        fi
    else
        # OK — reset consecutive-warn state
        printf 'OK\n' > "$STATE_FILE"
    fi

    case "$OVERALL_SEVERITY" in
        OK)    exit 0 ;;
        WARN)  exit 1 ;;
        ALARM) exit 2 ;;
    esac
}

main "$@"
