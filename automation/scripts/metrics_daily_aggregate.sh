#!/usr/bin/env bash
# metrics_daily_aggregate.sh — Compute yesterday's per-subsystem metrics and
# INSERT / REPLACE rows into daily_aggregates.
#
# Runs once per day (01:00 via cron).
# Cron entry (staged in jobs.yml — NOT active):
#   0 1 * * * /bin/bash /config/berenstuff/automation/scripts/metrics_daily_aggregate.sh >> /config/berenstuff/automation/logs/metrics_aggregate.log 2>&1
#
set -euo pipefail

readonly LOG_PREFIX="[metrics_aggregate]"
readonly METRICS_DB="/APPBOX_DATA/storage/.metrics-state/pipeline_metrics.db"
readonly METRICS_TIMEOUT_MS=5000

log() { printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ── Fail-soft DB wrapper ──────────────────────────────────────────────────────
_db() {
    sqlite3 -cmd ".timeout ${METRICS_TIMEOUT_MS}" "${METRICS_DB}" "$@" </dev/null
}

# ── Ensure the DB and tables exist ───────────────────────────────────────────
_ensure_schema() {
    # Pipe SQL to avoid here-doc + stdin redirect conflict (SC2261)
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
    | sqlite3 -cmd ".timeout ${METRICS_TIMEOUT_MS}" "${METRICS_DB}"
}

main() {
    log "Starting daily aggregate run"

    [[ -f "${METRICS_DB}" ]] || die "Metrics DB not found: ${METRICS_DB}"

    _ensure_schema

    # Yesterday in YYYY-MM-DD and unix epoch boundaries
    local yesterday_date yesterday_start yesterday_end
    yesterday_date="$(date -d 'yesterday' '+%Y-%m-%d' 2>/dev/null || date -v-1d '+%Y-%m-%d')"
    # Start of yesterday (00:00:00) and end (23:59:59) as unix epoch
    # date -d works on GNU/Linux; this host is Linux so it's safe
    yesterday_start="$(date -d "${yesterday_date} 00:00:00" '+%s')"
    yesterday_end="$(date -d "${yesterday_date} 23:59:59" '+%s')"

    log "Aggregating date=${yesterday_date} epoch=[${yesterday_start}, ${yesterday_end}]"

    # Query distinct subsystems that had runs yesterday
    local subsystems
    subsystems="$(_db "
        SELECT DISTINCT subsystem
        FROM subsystem_runs
        WHERE started_at >= ${yesterday_start}
          AND started_at <= ${yesterday_end};
    ")" || { log "No rows found (DB may be empty) — nothing to aggregate"; exit 0; }

    if [[ -z "$subsystems" ]]; then
        log "No runs recorded for ${yesterday_date} — nothing to aggregate"
        exit 0
    fi

    local row_count=0
    while IFS= read -r subsystem; do
        [[ -z "$subsystem" ]] && continue

        # Compute aggregates for this subsystem+date
        # finished_at IS NOT NULL = run completed; exit_code = 0 = success
        local agg_result
        agg_result="$(_db "
            SELECT
              COUNT(*) AS total_runs,
              SUM(CASE WHEN exit_code = 0 THEN 1 ELSE 0 END) AS successful_runs,
              SUM(CASE WHEN exit_code != 0 AND exit_code IS NOT NULL THEN 1 ELSE 0 END) AS failed_runs,
              COALESCE(SUM(files_processed), 0) AS total_files_processed,
              AVG(CASE
                WHEN finished_at IS NOT NULL AND started_at IS NOT NULL
                THEN finished_at - started_at
                ELSE NULL
              END) AS avg_duration_sec
            FROM subsystem_runs
            WHERE subsystem = '${subsystem}'
              AND started_at >= ${yesterday_start}
              AND started_at <= ${yesterday_end};
        " 2>/dev/null)"

        # Parse pipe-separated values from sqlite3 output
        local total_runs successful_runs failed_runs total_files avg_dur
        IFS='|' read -r total_runs successful_runs failed_runs total_files avg_dur \
            <<< "$agg_result"

        # Sanitize nulls
        total_runs="${total_runs:-0}"
        successful_runs="${successful_runs:-0}"
        failed_runs="${failed_runs:-0}"
        total_files="${total_files:-0}"
        avg_dur="${avg_dur:-}"  # may be empty if no finished runs

        local avg_dur_sql
        if [[ -n "$avg_dur" ]]; then
            avg_dur_sql="${avg_dur}"
        else
            avg_dur_sql="NULL"
        fi

        _db "
            INSERT OR REPLACE INTO daily_aggregates
              (date, subsystem, total_runs, successful_runs, failed_runs,
               total_files_processed, avg_duration_sec)
            VALUES
              ('${yesterday_date}', '${subsystem}',
               ${total_runs}, ${successful_runs}, ${failed_runs},
               ${total_files}, ${avg_dur_sql});
        " || { log "WARN: failed to insert aggregate for subsystem=${subsystem}"; continue; }

        log "  ${subsystem}: total=${total_runs} ok=${successful_runs} fail=${failed_runs} files=${total_files} avg_dur=${avg_dur:-N/A}s"
        row_count=$(( row_count + 1 ))
    done <<< "$subsystems"

    log "Done — ${row_count} subsystem(s) aggregated for ${yesterday_date}"
}

main "$@"
