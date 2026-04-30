#!/usr/bin/env bash
# intake_webhook_run.sh — Managed launcher for intake_webhook.py
#
# PURPOSE:
#   Sources .env, then runs intake_webhook.py in a restart loop.
#   A flock in the cron entry prevents duplicate instances.
#   On crash, waits RESTART_DELAY_SEC before retrying.
#
# CRON ENTRY (do not enable until Bazarr webhook is wired up — see docs/intake_webhook.md):
#   */5 * * * * /usr/bin/flock -n /tmp/intake_webhook.lock \
#       /bin/bash /config/berenstuff/automation/scripts/subtitles/intake_webhook_run.sh \
#       >> /config/berenstuff/automation/logs/intake_webhook.log 2>&1
#
# MANUAL START (foreground, for testing):
#   bash /config/berenstuff/automation/scripts/subtitles/intake_webhook_run.sh
#
# MANUAL STOP:
#   kill $(cat /tmp/intake_webhook.pid 2>/dev/null)
#
set -euo pipefail

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly LOG_PREFIX="[intake_webhook_run]"
readonly ENV_FILE="/config/berenstuff/.env"
readonly LIB_ENV="/config/berenstuff/automation/scripts/lib_env.sh"
readonly WEBHOOK_SCRIPT="${SCRIPT_DIR}/intake_webhook.py"
readonly PID_FILE="/tmp/intake_webhook.pid"
readonly RESTART_DELAY_SEC=10

log() { printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Guard: only one manager loop at a time (belt-and-suspenders with flock)
# ---------------------------------------------------------------------------
if [[ -f "$PID_FILE" ]]; then
    existing_pid="$(cat "$PID_FILE" 2>/dev/null || echo '')"
    if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
        log "Already running (PID ${existing_pid}) — exiting"
        exit 0
    fi
    rm -f "$PID_FILE"
fi

# Record manager PID
printf '%d\n' "$$" > "$PID_FILE"

cleanup() {
    local exit_code=$?
    rm -f "$PID_FILE"
    exit "$exit_code"
}
trap cleanup EXIT INT TERM

# ---------------------------------------------------------------------------
# Validate prerequisites
# ---------------------------------------------------------------------------
[[ -f "$ENV_FILE" ]] || die "env file not found: $ENV_FILE"
[[ -f "$LIB_ENV" ]] || die "lib_env.sh not found: $LIB_ENV"
[[ -f "$WEBHOOK_SCRIPT" ]] || die "webhook script not found: $WEBHOOK_SCRIPT"

# ---------------------------------------------------------------------------
# Load environment
# ---------------------------------------------------------------------------
# shellcheck source=/dev/null
source "$LIB_ENV"
load_env "$ENV_FILE"

log "Starting intake_webhook.py (INTAKE_WEBHOOK_PORT=${INTAKE_WEBHOOK_PORT:-8765})"

# ---------------------------------------------------------------------------
# Restart loop
# ---------------------------------------------------------------------------
while true; do
    python3 "$WEBHOOK_SCRIPT" &
    child_pid=$!
    log "Launched python3 PID ${child_pid}"

    wait "$child_pid" || true
    exit_code=$?

    if [[ "$exit_code" -eq 0 ]]; then
        log "intake_webhook.py exited cleanly (exit 0) — stopping manager"
        break
    fi

    log "intake_webhook.py exited with code ${exit_code} — restarting in ${RESTART_DELAY_SEC}s"
    sleep "$RESTART_DELAY_SEC"
done
