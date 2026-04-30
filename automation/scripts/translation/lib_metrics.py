"""
lib_metrics.py — Lightweight metrics helper for the mubuntu pipeline (Python side).

Standalone — no dependencies beyond stdlib.  Designed for use by translator.py
and any other Python pipeline component.

Usage:
    from translation.lib_metrics import record_run_start, record_run_end

    run_id = record_run_start("translator")
    # ... do work ...
    record_run_end(run_id, exit_code=0, files_processed=12, files_failed=1,
                   metadata={"provider": "ollama", "model": "aya-expanse:8b"})
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_METRICS_DB = Path("/APPBOX_DATA/storage/.metrics-state/pipeline_metrics.db")
_BUSY_TIMEOUT_MS = 5000

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Open the metrics DB with WAL mode and a generous busy timeout."""
    conn = sqlite3.connect(str(_METRICS_DB), timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


def _ensure_schema() -> None:
    """Create tables if they don't exist. Fail-soft: logs and returns on error."""
    try:
        conn = _connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS subsystem_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              subsystem TEXT NOT NULL,
              started_at INTEGER NOT NULL,
              finished_at INTEGER,
              exit_code INTEGER,
              files_processed INTEGER DEFAULT 0,
              files_failed INTEGER DEFAULT 0,
              metadata TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_subsystem_runs_subsystem_time
              ON subsystem_runs(subsystem, started_at);
            CREATE TABLE IF NOT EXISTS daily_aggregates (
              date TEXT NOT NULL,
              subsystem TEXT NOT NULL,
              total_runs INTEGER NOT NULL,
              successful_runs INTEGER NOT NULL,
              failed_runs INTEGER NOT NULL,
              total_files_processed INTEGER NOT NULL,
              avg_duration_sec REAL,
              PRIMARY KEY (date, subsystem)
            );
        """)
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("[lib_metrics] could not ensure schema: %s", exc)


# Run schema check once at import time (fail-soft)
try:
    _ensure_schema()
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_run_start(subsystem: str) -> int:
    """
    Insert a new subsystem_runs row and return its run_id.

    Returns -1 if the DB is unavailable (fail-soft — caller should tolerate).
    """
    try:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO subsystem_runs (subsystem, started_at) VALUES (?, ?)",
            (subsystem, int(time.time())),
        )
        conn.commit()
        run_id: int = cur.lastrowid  # type: ignore[assignment]
        conn.close()
        return run_id
    except Exception as exc:  # noqa: BLE001
        log.warning("[lib_metrics] record_run_start failed for %s: %s", subsystem, exc)
        return -1


def record_run_end(
    run_id: int,
    exit_code: int,
    files_processed: int = 0,
    files_failed: int = 0,
    metadata: Optional[dict] = None,
) -> None:
    """
    Update the subsystem_runs row for run_id with completion info.

    Fail-soft: logs a warning and returns if the DB is unavailable or run_id
    is -1 (sentinel for a failed record_run_start).
    """
    if run_id == -1:
        return  # start was a no-op; skip silently

    meta_json: Optional[str] = json.dumps(metadata) if metadata is not None else None
    try:
        conn = _connect()
        conn.execute(
            """
            UPDATE subsystem_runs
            SET finished_at     = ?,
                exit_code       = ?,
                files_processed = ?,
                files_failed    = ?,
                metadata        = ?
            WHERE id = ?
            """,
            (int(time.time()), exit_code, files_processed, files_failed, meta_json, run_id),
        )
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("[lib_metrics] record_run_end failed for run_id=%s: %s", run_id, exc)
