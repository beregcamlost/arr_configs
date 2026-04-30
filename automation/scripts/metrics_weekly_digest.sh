#!/usr/bin/env bash
# metrics_weekly_digest.sh — Compose and POST a weekly pipeline digest to Discord.
#
# Reads daily_aggregates for the past 7 days, computes rollup stats, generates
# a markdown table, auto-generates highlights and issues, and POSTs to
# DISCORD_WEBHOOK_URL (from .env).
#
# Cron entry (staged in jobs.yml — NOT active):
#   0 9 * * 0 /bin/bash /config/berenstuff/automation/scripts/metrics_weekly_digest.sh >> /config/berenstuff/automation/logs/metrics_weekly.log 2>&1
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly LOG_PREFIX="[metrics_weekly]"
readonly METRICS_DB="/APPBOX_DATA/storage/.metrics-state/pipeline_metrics.db"
readonly ENV_FILE="/config/berenstuff/.env"
readonly METRICS_TIMEOUT_MS=5000

log() { printf '%s %s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$LOG_PREFIX" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

# ── Load env ──────────────────────────────────────────────────────────────────
# shellcheck source=lib_env.sh
source "${SCRIPT_DIR}/lib_env.sh"
[[ -f "$ENV_FILE" ]] && load_env "$ENV_FILE"

# ── Fail-soft DB wrapper ──────────────────────────────────────────────────────
_db() {
    sqlite3 -cmd ".timeout ${METRICS_TIMEOUT_MS}" "${METRICS_DB}" "$@" </dev/null 2>/dev/null
}

# ── Date helpers ──────────────────────────────────────────────────────────────
_week_end_date() {
    # Last Saturday (end of the reporting week = yesterday on Sunday cron)
    date -d 'yesterday' '+%Y-%m-%d' 2>/dev/null || date -v-1d '+%Y-%m-%d'
}

_week_start_date() {
    date -d '7 days ago' '+%Y-%m-%d' 2>/dev/null || date -v-7d '+%Y-%m-%d'
}

# ── Format helpers ────────────────────────────────────────────────────────────
_fmt_dur() {
    # Format seconds (possibly fractional) as human-readable
    local raw="${1:-}"
    if [[ -z "$raw" || "$raw" == "NULL" ]]; then
        printf '%s' '-'
        return
    fi
    local secs="${raw%%.*}"
    if (( secs < 60 )); then
        printf '%ds' "$secs"
    elif (( secs < 3600 )); then
        printf '%dm%ds' "$(( secs / 60 ))" "$(( secs % 60 ))"
    else
        printf '%dh%dm' "$(( secs / 3600 ))" "$(( (secs % 3600) / 60 ))"
    fi
}

_pct() {
    local num="${1:-0}" denom="${2:-0}"
    if [[ "$denom" -eq 0 ]]; then
        printf '%s' '-'
    else
        # Use awk for floating point
        awk -v n="$num" -v d="$denom" 'BEGIN { printf "%.1f%%", (n/d)*100 }'
    fi
}

# ── Query 7-day rollup per subsystem ─────────────────────────────────────────
_query_week() {
    local week_start="$1"
    _db "
        SELECT
          subsystem,
          SUM(total_runs)            AS runs,
          SUM(successful_runs)       AS ok_runs,
          SUM(failed_runs)           AS fail_runs,
          SUM(total_files_processed) AS files,
          AVG(avg_duration_sec)      AS avg_dur
        FROM daily_aggregates
        WHERE date >= '${week_start}'
        GROUP BY subsystem
        ORDER BY subsystem;
    "
}

# ── Build the markdown table ──────────────────────────────────────────────────
_build_table() {
    local week_start="$1"
    local rows
    rows="$(_query_week "$week_start")"

    if [[ -z "$rows" ]]; then
        printf 'No data for this week yet.\n'
        return
    fi

    printf '| Subsystem | Runs | Success Rate | Files | Avg Duration |\n'
    printf '|-----------|------|--------------|-------|:------------:|\n'

    while IFS='|' read -r subsystem runs ok_runs _fail_runs files avg_dur; do
        [[ -z "$subsystem" ]] && continue
        local pct dur
        pct="$(_pct "${ok_runs:-0}" "${runs:-0}")"
        dur="$(_fmt_dur "${avg_dur:-}")"
        printf '| %-20s | %-4s | %-12s | %-5s | %-12s |\n' \
            "$subsystem" "${runs:-0}" "$pct" "${files:-0}" "$dur"
    done <<< "$rows"
}

# ── Auto-generate highlights ──────────────────────────────────────────────────
_build_highlights() {
    local week_start="$1"
    local highlights=()

    # Subsystem with most files processed
    local top_files
    top_files="$(_db "
        SELECT subsystem, SUM(total_files_processed) AS f
        FROM daily_aggregates
        WHERE date >= '${week_start}'
        GROUP BY subsystem
        ORDER BY f DESC
        LIMIT 1;
    " 2>/dev/null)"
    if [[ -n "$top_files" ]]; then
        local top_sub top_cnt
        IFS='|' read -r top_sub top_cnt <<< "$top_files"
        if [[ "${top_cnt:-0}" -gt 0 ]]; then
            highlights+=("- \`${top_sub}\` processed the most files: **${top_cnt}**")
        fi
    fi

    # Perfect success rate subsystems
    local perfect
    perfect="$(_db "
        SELECT subsystem
        FROM daily_aggregates
        WHERE date >= '${week_start}'
        GROUP BY subsystem
        HAVING SUM(total_runs) > 0
           AND SUM(failed_runs) = 0
           AND SUM(successful_runs) = SUM(total_runs);
    " 2>/dev/null | tr '\n' ',')"
    if [[ -n "$perfect" ]]; then
        perfect="${perfect%,}"
        highlights+=("- 100% success rate this week: \`${perfect//,/\`, \`}\`")
    fi

    if [[ ${#highlights[@]} -eq 0 ]]; then
        highlights+=("- No standout highlights this week")
    fi

    printf '%s\n' "${highlights[@]}"
}

# ── Auto-generate issues ──────────────────────────────────────────────────────
_build_issues() {
    local week_start="$1"
    local issues=()

    # Subsystems with any failures
    local failed_rows
    failed_rows="$(_db "
        SELECT subsystem, SUM(failed_runs) AS fails
        FROM daily_aggregates
        WHERE date >= '${week_start}'
        GROUP BY subsystem
        HAVING fails > 0
        ORDER BY fails DESC;
    " 2>/dev/null)"

    if [[ -n "$failed_rows" ]]; then
        while IFS='|' read -r sub fail_cnt; do
            [[ -z "$sub" ]] && continue
            issues+=("- \`${sub}\` had **${fail_cnt}** failed run(s) this week")
        done <<< "$failed_rows"
    fi

    # Low success rate (<90%) subsystems with meaningful volume
    local low_success
    low_success="$(_db "
        SELECT subsystem,
               SUM(successful_runs) AS ok,
               SUM(total_runs) AS total
        FROM daily_aggregates
        WHERE date >= '${week_start}'
        GROUP BY subsystem
        HAVING total >= 5
           AND CAST(ok AS REAL) / total < 0.90;
    " 2>/dev/null)"

    if [[ -n "$low_success" ]]; then
        while IFS='|' read -r sub ok total; do
            [[ -z "$sub" ]] && continue
            local pct
            pct="$(_pct "$ok" "$total")"
            issues+=("- \`${sub}\` success rate below 90%: **${pct}** (${ok}/${total} runs)")
        done <<< "$low_success"
    fi

    if [[ ${#issues[@]} -eq 0 ]]; then
        issues+=("- No issues detected this week")
    fi

    printf '%s\n' "${issues[@]}"
}

# ── POST to Discord ───────────────────────────────────────────────────────────
_post_discord() {
    local webhook_url="${DISCORD_WEBHOOK_URL:-}"
    local payload="$1"

    if [[ -z "$webhook_url" ]]; then
        log "DISCORD_WEBHOOK_URL not set — printing digest to stdout instead"
        printf '%s\n' "$payload"
        return 0
    fi

    local http_code
    http_code="$(curl -fsS -w '%{http_code}' -o /dev/null \
        -H 'Content-Type: application/json' \
        --max-time 15 \
        -d "$payload" \
        "$webhook_url" 2>/dev/null)" || { log "WARN: curl failed posting to Discord"; return 1; }

    if [[ "$http_code" =~ ^2 ]]; then
        log "Discord POST OK (${http_code})"
    else
        log "WARN: Discord POST returned HTTP ${http_code}"
        return 1
    fi
}

# ── Main ──────────────────────────────────────────────────────────────────────
main() {
    log "Starting weekly digest"

    [[ -f "${METRICS_DB}" ]] || { log "Metrics DB not found — nothing to report"; exit 0; }

    local week_end week_start
    week_end="$(_week_end_date)"
    week_start="$(_week_start_date)"

    log "Reporting period: ${week_start} → ${week_end}"

    local table highlights issues
    table="$(_build_table "$week_start")"
    highlights="$(_build_highlights "$week_start")"
    issues="$(_build_issues "$week_start")"

    # Build the full digest text
    local digest
    digest="$(printf '%s\n' \
        "**Pipeline Weekly Digest** (week ending ${week_end})" \
        "" \
        "${table}" \
        "" \
        "**Highlights:**" \
        "${highlights}" \
        "" \
        "**Issues:**" \
        "${issues}")"

    log "Digest composed (${#digest} chars)"

    # Discord payload: wrap in JSON content field (max 2000 chars per message)
    # Truncate if oversized
    if [[ ${#digest} -gt 1900 ]]; then
        digest="${digest:0:1900}..."
    fi

    # Escape special chars for JSON: backslash, double-quote, newline
    local json_content
    json_content="${digest//\\/\\\\}"
    json_content="${json_content//\"/\\\"}"
    json_content="${json_content//$'\n'/\\n}"

    local payload
    payload="{\"content\":\"${json_content}\"}"

    _post_discord "$payload"
    log "Done"
}

main "$@"
