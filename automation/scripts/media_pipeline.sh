#!/usr/bin/env bash
# media_pipeline.sh — Unified media pipeline orchestrator
#
# Replaces 10 high-frequency cron jobs with a single coordinated run.
# Priority order: quick jobs → subtitle pipeline → translation
# Codec conversion runs independently via its own cron (library_codec_manager.sh).
#
# Cron entries (two lanes — crons are currently STOPPED, kept for reference):
#   */5  * * * * /usr/bin/flock -n /tmp/media_pipeline_fast.lock /bin/bash /config/berenstuff/automation/scripts/media_pipeline.sh --lane fast >> /config/berenstuff/automation/logs/media_pipeline.log 2>&1
#   */30 * * * * /usr/bin/flock -n /tmp/media_pipeline_slow.lock /bin/bash /config/berenstuff/automation/scripts/media_pipeline.sh --lane slow >> /config/berenstuff/automation/logs/media_pipeline.log 2>&1
#
# Usage: media_pipeline.sh [--lane fast|slow|all] [--dry-run]
#   --lane fast   Steps 1-3 only: grab-monitor, zombie_reaper, import_cleanup  (every 5 min)
#   --lane slow   Steps 4-7 only: dedupe, recovery, sqm, translation           (every 30 min)
#   --lane all    All 7 steps (default; useful for manual full runs)
#
# DB coordination: pipeline_state table in codec state DB
# Lane locks:     /tmp/media_pipeline_fast.lock  (fast lane flock)
#                 /tmp/media_pipeline_slow.lock  (slow lane flock)
# Concurrency:    fast and slow lanes use separate subsystem rows — they do NOT
#                 block each other.  Two concurrent slow-lane runs are prevented
#                 by the flock on /tmp/media_pipeline_slow.lock (held by cron).

set -uo pipefail
# NOTE: -e intentionally omitted — each step handles its own exit code so a
# single failure does not abort the full pipeline run.

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly LOG="/config/berenstuff/automation/logs/media_pipeline.log"
readonly ENV_FILE="/config/berenstuff/.env"

readonly CANONICAL_DIR="/config/berenstuff/automation/scripts"
# SCRIPTS_DIR removed: automation/scripts/ is the single source of truth (Phase 4b)

readonly CODEC_STATE_DB="/APPBOX_DATA/storage/.transcode-state-media/library_codec_state.db"
readonly MEDIA_PATH_PREFIX="/APPBOX_DATA/storage/media"
readonly SQLITE_TIMEOUT_MS=15000   # used as .timeout dot-command (no output leak)

# ── Timeouts (seconds) ────────────────────────────────────────────────────────
readonly TIMEOUT_QUICK=60
readonly TIMEOUT_TRANSLATION="${TIMEOUT_TRANSLATION:-600}"

# ── State ─────────────────────────────────────────────────────────────────────
PIPELINE_START_TS=""
ACTIVE_SUBSYSTEM=""
DRY_RUN=0
LANE="all"

# ── Argument parsing ──────────────────────────────────────────────────────────
_usage() {
  printf 'Usage: %s [--lane fast|slow|all] [--dry-run]\n' "$(basename "$0")" >&2
  printf '  --lane fast   Steps 1-3: grab-monitor, zombie_reaper, import_cleanup\n' >&2
  printf '  --lane slow   Steps 4-7: dedupe, recovery, sqm, translation\n' >&2
  printf '  --lane all    All 7 steps (default; for manual full runs)\n' >&2
  printf '  --dry-run     Log what would run without executing\n' >&2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --lane)
      shift
      [[ $# -eq 0 ]] && { printf 'ERROR: --lane requires an argument\n' >&2; _usage; exit 1; }
      LANE="$1"
      ;;
    --dry-run) DRY_RUN=1 ;;
    --help|-h) _usage; exit 0 ;;
    *)
      printf 'ERROR: Unknown argument: %s\n' "$1" >&2
      _usage
      exit 1
      ;;
  esac
  shift
done

case "$LANE" in
  fast|slow|all) ;;
  *)
    printf 'ERROR: Invalid lane "%s" — must be fast, slow, or all\n' "$LANE" >&2
    _usage
    exit 1
    ;;
esac

readonly DRY_RUN LANE

# ── Logging ───────────────────────────────────────────────────────────────────
log() {
  printf '%s [media_pipeline] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"
}

log_step() {
  local step="$1" status="$2"
  shift 2
  local extra="${*:-}"
  if [[ -n "$extra" ]]; then
    log "step=${step} status=${status} ${extra}"
  else
    log "step=${step} status=${status}"
  fi
}

# ── Environment (atomic load) ─────────────────────────────────────────────────
# shellcheck source=lib_env.sh
source "${SCRIPT_DIR}/lib_env.sh"
[[ -f "$ENV_FILE" ]] && load_env "$ENV_FILE"

# ── Metrics (fail-soft: sourced after env so DB path is defined) ───────────────
# shellcheck source=lib_metrics.sh
source "${SCRIPT_DIR}/lib_metrics.sh" || true

# ── Load guard (from lib_subtitle_common.sh) ──────────────────────────────────
load_guard() {
  local label="${1:-media_pipeline}"
  local load_1min thresh_raw threshold
  load_1min="$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)"
  thresh_raw="${LOAD_GUARD_THRESHOLD:-}"
  if [[ -n "$thresh_raw" ]]; then
    threshold="$thresh_raw"
  else
    local ncpu
    ncpu="$(nproc 2>/dev/null || echo 1)"
    threshold=$(( ncpu * 3 ))
    (( threshold < 20 )) && threshold=20
  fi
  local load_int="${load_1min%%.*}"
  if [[ "$load_int" -ge "$threshold" ]]; then
    log_step "$label" "skipped" "reason=load_guard load=${load_1min} threshold=${threshold}"
    return 1
  fi
  return 0
}

# ── DB helpers ────────────────────────────────────────────────────────────────
_db() {
  # .timeout is a dot-command (no output), unlike PRAGMA busy_timeout which leaks a row
  sqlite3 -cmd ".timeout ${SQLITE_TIMEOUT_MS}" "$CODEC_STATE_DB" "$@" </dev/null 2>/dev/null
}

pipeline_ensure_table() {
  _db "CREATE TABLE IF NOT EXISTS pipeline_state (
    subsystem        TEXT PRIMARY KEY,
    state            TEXT DEFAULT 'idle',
    pid              INTEGER,
    started_at       TEXT,
    current_file     TEXT,
    last_run_ts      TEXT DEFAULT CURRENT_TIMESTAMP,
    last_duration_sec INTEGER,
    last_exit_code   INTEGER,
    error_msg        TEXT,
    updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
  );" 2>/dev/null || true
}

pipeline_mark_running() {
  local subsystem="$1"
  local current_file="${2:-}"
  _db "INSERT INTO pipeline_state (subsystem, state, pid, started_at, current_file, updated_at)
       VALUES ('${subsystem}', 'running', $$, datetime('now'), '${current_file}', datetime('now'))
       ON CONFLICT(subsystem) DO UPDATE SET
         state        = 'running',
         pid          = $$,
         started_at   = datetime('now'),
         current_file = '${current_file}',
         updated_at   = datetime('now');" 2>/dev/null || true
}

pipeline_mark_done() {
  local subsystem="$1"
  local exit_code="$2"
  local start_ts="$3"
  local duration_sec
  local now_ts
  now_ts="$(date '+%s')"
  duration_sec=$(( now_ts - start_ts ))

  local new_state="idle"
  local error_msg=""
  if [[ "$exit_code" -ne 0 ]]; then
    new_state="error"
    error_msg="exit_code=${exit_code}"
  fi

  _db "UPDATE pipeline_state SET
         state             = '${new_state}',
         pid               = NULL,
         last_run_ts       = datetime('now'),
         last_duration_sec = ${duration_sec},
         last_exit_code    = ${exit_code},
         error_msg         = '${error_msg}',
         updated_at        = datetime('now')
       WHERE subsystem = '${subsystem}';" 2>/dev/null || true
}

pipeline_is_running() {
  # Returns 0 (true) if subsystem is marked running AND pid is still alive.
  # Returns 1 (idle/dead) otherwise.
  local subsystem="$1"
  local row pid state
  row="$(_db "SELECT state, pid FROM pipeline_state WHERE subsystem='${subsystem}';" 2>/dev/null)" || return 1
  [[ -z "$row" ]] && return 1
  state="${row%%|*}"
  pid="${row##*|}"
  [[ "$state" != "running" ]] && return 1
  [[ -z "$pid" || "$pid" == "NULL" ]] && return 1
  # Check if pid is still alive
  kill -0 "$pid" 2>/dev/null && return 0
  return 1
}

pipeline_recover_stale() {
  # Mark any 'running' entries with dead pids as 'error' (unclean exit).
  local rows
  rows="$(_db "SELECT subsystem, pid FROM pipeline_state WHERE state='running';" 2>/dev/null)" || return 0
  [[ -z "$rows" ]] && return 0

  while IFS='|' read -r subsystem pid; do
    [[ -z "$subsystem" ]] && continue
    if [[ -z "$pid" || "$pid" == "NULL" ]] || ! kill -0 "$pid" 2>/dev/null; then
      log "recover subsystem=${subsystem} pid=${pid:-null} status=stale → marking error"
      _db "UPDATE pipeline_state SET
             state     = 'error',
             pid       = NULL,
             error_msg = 'recovered_stale_pid_${pid:-null}',
             updated_at = datetime('now')
           WHERE subsystem = '${subsystem}';" 2>/dev/null || true
    fi
  done <<< "$rows"
}

# ── Lane-level concurrency guard ──────────────────────────────────────────────
# Each lane has a sentinel row in pipeline_state so that a manual `--lane all`
# invocation can detect and skip a lane that is still running concurrently.
# The cron-level flock (separate lock files per lane) is the primary guard;
# this DB sentinel is a belt-and-suspenders check for manual runs.
LANE_SUBSYSTEM=""
lane_mark_running() {
  [[ -z "$LANE_SUBSYSTEM" ]] && return 0
  pipeline_mark_running "$LANE_SUBSYSTEM"
}
lane_mark_done() {
  local exit_code="${1:-0}"
  [[ -z "$LANE_SUBSYSTEM" ]] && return 0
  pipeline_mark_done "$LANE_SUBSYSTEM" "$exit_code" "${PIPELINE_START_TS:-$(date '+%s')}"
}
lane_is_running() {
  [[ -z "$LANE_SUBSYSTEM" ]] && return 1
  pipeline_is_running "$LANE_SUBSYSTEM"
}

# ── Signal handling ───────────────────────────────────────────────────────────
cleanup() {
  # Capture exit code BEFORE any 'local' declaration — 'local' resets $? to 0.
  _CLEANUP_EXIT=$?
  local exit_code="$_CLEANUP_EXIT"
  if [[ -n "$ACTIVE_SUBSYSTEM" ]]; then
    local now_ts
    now_ts="$(date '+%s')"
    pipeline_mark_done "$ACTIVE_SUBSYSTEM" "130" "$now_ts"
    log "step=${ACTIVE_SUBSYSTEM} status=interrupted signal=SIGTERM/SIGINT"
  fi
  lane_mark_done "$exit_code"
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

# ── Step runner ───────────────────────────────────────────────────────────────
# run_step <subsystem> <timeout_sec> <cmd> [args...]
# - Checks if already running (DB guard)
# - Marks running, runs with timeout, marks done
# - On timeout or error: logs and continues — never aborts pipeline
run_step() {
  local subsystem="$1"
  local timeout_sec="$2"
  shift 2
  local cmd=("$@")

  # Skip if already running (from a prior invocation still in progress)
  if pipeline_is_running "$subsystem"; then
    log_step "$subsystem" "skipped" "reason=already_running"
    return 0
  fi

  pipeline_mark_running "$subsystem"
  ACTIVE_SUBSYSTEM="$subsystem"
  local step_start
  step_start="$(date '+%s')"
  log_step "$subsystem" "start"

  local exit_code=0
  if [[ "$DRY_RUN" -eq 1 ]]; then
    log_step "$subsystem" "dry-run" "cmd=${cmd[*]}"
  else
    timeout "$timeout_sec" "${cmd[@]}" >> "$LOG" 2>&1 || exit_code=$?
    if [[ "$exit_code" -eq 124 ]]; then
      log_step "$subsystem" "timeout" "timeout=${timeout_sec}s"
    fi
  fi

  pipeline_mark_done "$subsystem" "$exit_code" "$step_start"
  ACTIVE_SUBSYSTEM=""

  local duration
  duration=$(( $(date '+%s') - step_start ))
  log_step "$subsystem" "done" "duration=${duration}s exit=${exit_code}"
  return 0
}

# ── Pipeline steps ────────────────────────────────────────────────────────────

run_grab_monitor() {
  run_step "grab_monitor" "$TIMEOUT_QUICK" \
    /bin/bash "${CANONICAL_DIR}/grab-monitor.sh"
}

run_zombie_reaper() {
  run_step "zombie_reaper" "$TIMEOUT_QUICK" \
    /bin/bash "${CANONICAL_DIR}/emby_zombie_reaper.sh"
}

run_import_cleanup() {
  run_step "import_cleanup" "$TIMEOUT_QUICK" \
    /bin/bash "${CANONICAL_DIR}/arr_cleanup_importblocked.sh"
}

run_subtitle_dedupe() {
  run_step "subtitle_dedupe" 0 \
    /bin/bash "${CANONICAL_DIR}/subtitles/library_subtitle_dedupe.sh" \
      --path-prefix  "$MEDIA_PATH_PREFIX" \
      --state-dir    "/APPBOX_DATA/storage/.subtitle-dedupe-state" \
      --bazarr-db    "${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}" \
      --bazarr-url   "${BAZARR_URL:-http://127.0.0.1:6767/bazarr}" \
      --since        10 \
      --log          "/config/berenstuff/automation/logs/library_subtitle_dedupe.log"
}

run_subtitle_recovery() {
  run_step "subtitle_recovery" 0 \
    /bin/bash "${CANONICAL_DIR}/subtitles/bazarr_subtitle_recovery.sh" \
      --bazarr-url   "${BAZARR_URL:-http://127.0.0.1:6767/bazarr}" \
      --bazarr-db    "${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}" \
      --radarr-url   "${RADARR_URL:-http://127.0.0.1:7878/radarr}" \
      --sonarr-url   "${SONARR_URL:-http://127.0.0.1:8989/sonarr}" \
      --state-dir    "/APPBOX_DATA/storage/.subtitle-recovery-state" \
      --since        30 \
      --log          "/config/berenstuff/automation/logs/bazarr_subtitle_recovery.log"
}

run_subtitle_quality() {
  # timeout=0 → no timeout; pipeline flock prevents re-entry
  run_step "subtitle_quality" 0 \
    /bin/bash "${CANONICAL_DIR}/subtitles/subtitle_quality_manager.sh" \
      auto-maintain \
      --since            15 \
      --keep-profile-langs \
      --path-prefix      "$MEDIA_PATH_PREFIX"
}

run_translation() {
  local _since="${TRANSLATOR_SINCE_MINUTES:-10}"
  local _max_files="${TRANSLATOR_MAX_FILES_PER_RUN:-3}"
  local _bazarr_db="${BAZARR_DB:-/opt/bazarr/data/db/bazarr.db}"
  run_step "translation" "${TIMEOUT_TRANSLATION}" \
    /bin/bash -c "cd /config/berenstuff && PYTHONPATH=${CANONICAL_DIR} python3 ${CANONICAL_DIR}/translation/translator.py translate --since ${_since} --max-files ${_max_files} --bazarr-db ${_bazarr_db} </dev/null"
}

# ── Lane runners ──────────────────────────────────────────────────────────────

run_fast_lane() {
  LANE_SUBSYSTEM="fast_lane"
  if lane_is_running; then
    log "lane=fast status=skipped reason=already_running"
    return 0
  fi
  lane_mark_running
  log "=== fast lane start (steps 1-3) ==="

  local _fast_run_id
  _fast_run_id="$(metrics_run_start "fast_lane" 2>/dev/null)" || _fast_run_id=""

  run_grab_monitor
  run_zombie_reaper
  run_import_cleanup

  lane_mark_done 0
  [[ -n "$_fast_run_id" ]] && metrics_run_end "$_fast_run_id" 0 2>/dev/null || true
  log "=== fast lane done ==="
}

run_slow_lane() {
  LANE_SUBSYSTEM="slow_lane"
  if lane_is_running; then
    log "lane=slow status=skipped reason=already_running"
    return 0
  fi
  lane_mark_running
  log "=== slow lane start (steps 4-7) ==="

  local _slow_run_id
  _slow_run_id="$(metrics_run_start "slow_lane" 2>/dev/null)" || _slow_run_id=""

  log "=== load gate check ==="
  if ! load_guard "slow_lane"; then
    lane_mark_done 0
    [[ -n "$_slow_run_id" ]] && metrics_run_end "$_slow_run_id" 0 2>/dev/null || true
    log "=== slow lane done (load_guard skipped heavy work) ==="
    return 0
  fi
  log "=== heavy work continuing ==="

  run_subtitle_dedupe
  run_subtitle_recovery
  run_subtitle_quality
  run_translation

  lane_mark_done 0
  [[ -n "$_slow_run_id" ]] && metrics_run_end "$_slow_run_id" 0 2>/dev/null || true
  log "=== slow lane done ==="
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
  PIPELINE_START_TS="$(date '+%s')"
  log "=== pipeline start lane=${LANE} ==="

  # Reload env atomically (belt-and-suspenders: also sourced at top level above)
  [[ -f "$ENV_FILE" ]] && load_env "$ENV_FILE"

  # Initialise DB table
  pipeline_ensure_table

  # Recover any stale entries from a previous unclean exit
  pipeline_recover_stale

  case "$LANE" in
    fast)
      run_fast_lane
      ;;
    slow)
      run_slow_lane
      ;;
    all)
      # ── Phase 1: Quick jobs ───────────────────────────────────────────────
      LANE_SUBSYSTEM="fast_lane"
      lane_mark_running
      run_grab_monitor
      run_zombie_reaper
      run_import_cleanup
      lane_mark_done 0

      # ── Load gate ─────────────────────────────────────────────────────────
      log "=== load gate check ==="
      if ! load_guard "media_pipeline"; then
        local total_duration
        total_duration=$(( $(date '+%s') - PIPELINE_START_TS ))
        log "=== pipeline done (quick_only) total_duration=${total_duration}s ==="
        exit 0
      fi
      log "=== heavy work continuing ==="

      # ── Phase 2: Subtitle pipeline ────────────────────────────────────────
      LANE_SUBSYSTEM="slow_lane"
      lane_mark_running
      run_subtitle_dedupe
      run_subtitle_recovery
      run_subtitle_quality

      # ── Phase 3: Translation ──────────────────────────────────────────────
      run_translation
      lane_mark_done 0
      ;;
  esac

  local total_duration
  total_duration=$(( $(date '+%s') - PIPELINE_START_TS ))
  log "=== pipeline done lane=${LANE} total_duration=${total_duration}s ==="
}

main "$@"
