#!/usr/bin/env bash
# lib_metrics.sh — Lightweight metrics helper for the mubuntu pipeline
#
# Source this library, then call its functions.
# All writes use WAL mode + busy_timeout so they fail-soft if the DB is
# momentarily locked — they never abort the calling script.
#
# Usage:
#   source /config/berenstuff/automation/scripts/lib_metrics.sh
#   RUN_ID=$(metrics_run_start "fast_lane")
#   metrics_run_end "$RUN_ID" 0 12 0 '{"step":"dedupe"}'
#
# Guard against double-sourcing
[[ -n "${_LIB_METRICS_LOADED:-}" ]] && return 0
readonly _LIB_METRICS_LOADED=1

readonly _METRICS_DB="/APPBOX_DATA/storage/.metrics-state/pipeline_metrics.db"
readonly _METRICS_TIMEOUT_MS=5000

# ── Internal helper ────────────────────────────────────────────────────────────
# _metrics_db <sql>  — run SQL, swallow errors, return exit code
_metrics_db() {
    sqlite3 \
        -cmd ".timeout ${_METRICS_TIMEOUT_MS}" \
        "${_METRICS_DB}" \
        "$1" </dev/null 2>/dev/null
}

# ── Ensure DB + schema exist (idempotent) ─────────────────────────────────────
_metrics_ensure_schema() {
    # Pipe SQL to sqlite3 to avoid here-doc + stdin redirect conflict (SC2261)
    printf '%s\n' \
        "PRAGMA journal_mode=WAL;" \
        "CREATE TABLE IF NOT EXISTS subsystem_runs (" \
        "  id INTEGER PRIMARY KEY AUTOINCREMENT," \
        "  subsystem TEXT NOT NULL," \
        "  started_at INTEGER NOT NULL," \
        "  finished_at INTEGER," \
        "  exit_code INTEGER," \
        "  files_processed INTEGER DEFAULT 0," \
        "  files_failed INTEGER DEFAULT 0," \
        "  metadata TEXT" \
        ");" \
        "CREATE INDEX IF NOT EXISTS idx_subsystem_runs_subsystem_time" \
        "  ON subsystem_runs(subsystem, started_at);" \
        "CREATE TABLE IF NOT EXISTS daily_aggregates (" \
        "  date TEXT NOT NULL," \
        "  subsystem TEXT NOT NULL," \
        "  total_runs INTEGER NOT NULL," \
        "  successful_runs INTEGER NOT NULL," \
        "  failed_runs INTEGER NOT NULL," \
        "  total_files_processed INTEGER NOT NULL," \
        "  avg_duration_sec REAL," \
        "  PRIMARY KEY (date, subsystem)" \
        ");" \
    | sqlite3 -cmd ".timeout ${_METRICS_TIMEOUT_MS}" "${_METRICS_DB}" 2>/dev/null || true
}

# Call once at source time (fail-soft: no set -e dependency)
_metrics_ensure_schema

# ── metrics_run_start <subsystem> ─────────────────────────────────────────────
# Inserts a new row for a run that is starting now.
# Outputs the new row id (run_id) to stdout.
# Returns 1 (and prints nothing) if the DB is unavailable.
metrics_run_start() {
    local subsystem="${1:?metrics_run_start requires subsystem name}"
    local now
    now="$(date '+%s')"

    local run_id
    run_id="$(_metrics_db "
        INSERT INTO subsystem_runs (subsystem, started_at)
        VALUES ('${subsystem}', ${now});
        SELECT last_insert_rowid();
    " 2>/dev/null)" || { printf '[lib_metrics] WARNING: could not start metrics row for %s\n' "$subsystem" >&2; return 1; }

    # sqlite3 may emit the WAL pragma result line; grab only the last numeric line
    run_id="$(printf '%s\n' "$run_id" | grep -E '^[0-9]+$' | tail -1)"
    if [[ -z "$run_id" ]]; then
        printf '[lib_metrics] WARNING: empty run_id returned for %s\n' "$subsystem" >&2
        return 1
    fi

    printf '%s\n' "$run_id"
}

# ── metrics_run_end <run_id> <exit_code> [files_processed] [files_failed] [metadata_json] ──
# Updates an existing row with finish time and outcome.
# All arguments after run_id and exit_code are optional (default 0 / NULL).
# Fail-soft: warns on stderr but never exits.
metrics_run_end() {
    local run_id="${1:?metrics_run_end requires run_id}"
    local exit_code="${2:?metrics_run_end requires exit_code}"
    local files_processed="${3:-0}"
    local files_failed="${4:-0}"
    local metadata_json="${5:-}"
    local now
    now="$(date '+%s')"

    # Escape single-quote in metadata JSON (replace ' with '')
    local safe_meta="${metadata_json//\'/\'\'}"
    local meta_sql
    if [[ -n "$safe_meta" ]]; then
        meta_sql="'${safe_meta}'"
    else
        meta_sql="NULL"
    fi

    _metrics_db "
        UPDATE subsystem_runs
        SET finished_at      = ${now},
            exit_code        = ${exit_code},
            files_processed  = ${files_processed},
            files_failed     = ${files_failed},
            metadata         = ${meta_sql}
        WHERE id = ${run_id};
    " || printf '[lib_metrics] WARNING: could not end metrics row %s\n' "$run_id" >&2

    return 0
}

# ── metrics_get_recent <subsystem> <since_seconds> ────────────────────────────
# Outputs TSV rows (id, subsystem, started_at, finished_at, exit_code,
# files_processed, files_failed) for runs in the last <since_seconds> seconds.
metrics_get_recent() {
    local subsystem="${1:?metrics_get_recent requires subsystem}"
    local since_sec="${2:?metrics_get_recent requires since_seconds}"
    local cutoff
    cutoff="$(( $(date '+%s') - since_sec ))"

    _metrics_db "
        SELECT id, subsystem, started_at, finished_at, exit_code,
               files_processed, files_failed
        FROM subsystem_runs
        WHERE subsystem = '${subsystem}'
          AND started_at >= ${cutoff}
        ORDER BY started_at;
    " || printf '[lib_metrics] WARNING: could not query recent runs for %s\n' "$subsystem" >&2

    return 0
}
