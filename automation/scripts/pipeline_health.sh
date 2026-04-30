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
readonly -a STATE_DBS=(
    "/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
    "/APPBOX_DATA/storage/.streaming-checker-state/streaming_state.db"
    "/APPBOX_DATA/storage/.translation-state/translation_state.db"
    "/APPBOX_DATA/storage/.subtitle-quality-state/subtitle_quality_state.db"
    "/APPBOX_DATA/storage/.subtitle-dedupe-state/subtitle_dedupe.db"
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
check_orchestrator_freshness() {
    local db="/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
    if [[ ! -f "$db" ]]; then
        record "ALARM" "Orchestrator DB missing: $db"
        return
    fi

    local last_run
    last_run="$(sqlite3 "$db" \
        "SELECT MAX(last_run_ts) FROM pipeline_state WHERE last_run_ts IS NOT NULL;" 2>/dev/null || true)"

    if [[ -z "$last_run" ]]; then
        record "WARN" "pipeline_state: no last_run_ts found in DB"
        return
    fi

    local last_epoch
    last_epoch="$(date -d "$last_run" +%s 2>/dev/null || echo 0)"
    local now_epoch
    now_epoch="$(date +%s)"
    local age_min=$(( (now_epoch - last_epoch) / 60 ))

    if (( age_min > PIPELINE_FRESHNESS_MIN )); then
        record "WARN" "Orchestrator stale: last run was ${age_min}m ago (threshold ${PIPELINE_FRESHNESS_MIN}m)"
    else
        record "OK" "Orchestrator fresh: last run ${age_min}m ago"
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
        local result
        result="$(sqlite3 "$db" "PRAGMA quick_check;" 2>&1 || true)"
        if [[ "$result" != "ok" ]]; then
            record "ALARM" "DB check failed ($db): $result"
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
check_ollama_endpoints() {
    local wsl_url="${OLLAMA_BASE_URL:-}"
    local debian_url="${DEBIAN_OLLAMA_URL:-}"

    for pair in "WSL-GPU:${wsl_url}" "Debian-CPU:${debian_url}"; do
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

    local payload
    payload="$(printf '{"content": %s}' "$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")"

    curl -fsS -X POST -H "Content-Type: application/json" \
        -d "$payload" "$webhook_url" > /dev/null 2>&1 || log "Discord post failed"
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
    check_state_dbs
    check_ollama_endpoints
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
