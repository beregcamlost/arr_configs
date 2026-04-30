# Pipeline Metrics

## Overview

Phase 6 L added a lightweight SQLite-based metrics layer that records per-subsystem
run outcomes and aggregates them daily for a weekly Discord digest.

## What's tracked

| Subsystem | Recorded by |
|-----------|-------------|
| `fast_lane` | `media_pipeline.sh` |
| `slow_lane` | `media_pipeline.sh` |
| `translator` | `translator.py` (translate command) |
| `phase5_backfill` | `phase5_backfill.sh` |
| `health` | `pipeline_health.sh` |

Each completed run records:
- Start and finish timestamps (unix epoch)
- Exit code (0 = success)
- Files processed and files failed
- Optional JSON metadata blob (provider, model, severity, etc.)

## Database

**Location:** `/APPBOX_DATA/storage/.metrics-state/pipeline_metrics.db`

**Mode:** WAL (Write-Ahead Logging) — supports concurrent readers without blocking writers.

**Tables:**

### `subsystem_runs` — raw run log

```
id              INTEGER PK AUTOINCREMENT
subsystem       TEXT    — e.g. 'fast_lane', 'translator'
started_at      INTEGER — unix epoch
finished_at     INTEGER — unix epoch, NULL if still running
exit_code       INTEGER — 0=success, NULL if still running
files_processed INTEGER — files successfully handled
files_failed    INTEGER — files that errored
metadata        TEXT    — JSON blob, subsystem-specific
```

### `daily_aggregates` — pre-computed daily rollup

```
date                TEXT  PRIMARY KEY component (YYYY-MM-DD)
subsystem           TEXT  PRIMARY KEY component
total_runs          INTEGER
successful_runs     INTEGER
failed_runs         INTEGER
total_files_processed INTEGER
avg_duration_sec    REAL
```

## Example queries

```bash
# Open the DB
sqlite3 /APPBOX_DATA/storage/.metrics-state/pipeline_metrics.db

# Last 20 runs across all subsystems
SELECT datetime(started_at,'unixepoch','localtime') AS started,
       subsystem, exit_code, files_processed, files_failed,
       (finished_at - started_at) AS dur_sec
FROM subsystem_runs
ORDER BY started_at DESC LIMIT 20;

# Today's translator runs
SELECT id, datetime(started_at,'unixepoch','localtime'), exit_code,
       files_processed, files_failed, metadata
FROM subsystem_runs
WHERE subsystem = 'translator'
  AND date(started_at,'unixepoch','localtime') = date('now','localtime');

# Weekly summary from daily_aggregates
SELECT date, subsystem, total_runs, successful_runs, failed_runs,
       total_files_processed,
       round(avg_duration_sec, 1) AS avg_dur_sec
FROM daily_aggregates
WHERE date >= date('now','-7 days')
ORDER BY date DESC, subsystem;

# Success rate per subsystem last 7 days
SELECT subsystem,
       SUM(total_runs) AS runs,
       round(100.0 * SUM(successful_runs) / SUM(total_runs), 1) AS pct_ok,
       SUM(total_files_processed) AS files
FROM daily_aggregates
WHERE date >= date('now','-7 days')
GROUP BY subsystem
ORDER BY pct_ok ASC;
```

## Helper libraries

### Bash: `lib_metrics.sh`

**Location:** `/config/berenstuff/automation/scripts/lib_metrics.sh`

Source it after `lib_env.sh`:

```bash
source "${SCRIPT_DIR}/lib_metrics.sh"

RUN_ID=$(metrics_run_start "my_subsystem")
# ... do work ...
metrics_run_end "$RUN_ID" "$exit_code" "$files_ok" "$files_fail" '{"key":"val"}'
```

Functions:
- `metrics_run_start <subsystem>` — inserts row, returns run_id on stdout
- `metrics_run_end <run_id> <exit_code> [files_processed] [files_failed] [metadata_json]` — updates row
- `metrics_get_recent <subsystem> <since_seconds>` — TSV of recent rows

All functions are **fail-soft**: they warn to stderr on DB errors and never cause the calling script to abort.

### Python: `lib_metrics.py`

**Location:** `/config/berenstuff/automation/scripts/translation/lib_metrics.py`

```python
from translation.lib_metrics import record_run_start, record_run_end

run_id = record_run_start("my_subsystem")
# ... do work ...
record_run_end(run_id, exit_code=0, files_processed=10, files_failed=0,
               metadata={"model": "aya-expanse:8b"})
```

`record_run_start` returns `-1` if the DB is unavailable. `record_run_end` is a no-op when `run_id == -1`.

## Automated jobs

### Daily aggregator

**Script:** `/config/berenstuff/automation/scripts/metrics_daily_aggregate.sh`  
**Cron schedule:** `0 1 * * *` (01:00 daily)  
**Log:** `/config/berenstuff/automation/logs/metrics_aggregate.log`

Computes per-subsystem stats for the previous day from `subsystem_runs` and
inserts/replaces rows in `daily_aggregates`. Safe to re-run (INSERT OR REPLACE).

### Weekly Discord digest

**Script:** `/config/berenstuff/automation/scripts/metrics_weekly_digest.sh`  
**Cron schedule:** `0 9 * * 0` (Sunday 09:00)  
**Log:** `/config/berenstuff/automation/logs/metrics_weekly.log`

Reads the last 7 days of `daily_aggregates`, builds a markdown table,
auto-generates highlights (top performers) and issues (failures, low success rate),
and POSTs to `DISCORD_WEBHOOK_URL`.

Both cron entries are **staged in `jobs.yml` with a `# TODO: enable` comment** —
enable them manually after review.

## Extending to new subsystems

1. In the script (bash), add after sourcing `lib_metrics.sh`:
   ```bash
   RUN_ID=$(metrics_run_start "my_new_subsystem")
   ```
   And at the end (or in a trap):
   ```bash
   metrics_run_end "$RUN_ID" "$exit_code" "$files_ok" "$files_fail"
   ```

2. In Python, import and call `record_run_start` / `record_run_end`.

3. No schema changes needed — `subsystem` is a free-form text field.
   The daily aggregator picks up any new subsystem name automatically.
