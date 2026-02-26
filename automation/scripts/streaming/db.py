"""SQLite state database for streaming availability tracking."""

import os
import sqlite3
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def init_db(db_path):
    """Create database directory and tables if they don't exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS streaming_status (
            tmdb_id INTEGER NOT NULL,
            media_type TEXT NOT NULL,
            provider_id INTEGER NOT NULL,
            provider_name TEXT NOT NULL,
            title TEXT NOT NULL,
            year INTEGER,
            arr_id INTEGER,
            library TEXT,
            size_bytes INTEGER,
            path TEXT,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            left_at TEXT,
            deleted_at TEXT,
            PRIMARY KEY (tmdb_id, media_type, provider_id)
        );
        CREATE TABLE IF NOT EXISTS scan_history (
            scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            country TEXT NOT NULL DEFAULT 'CL',
            movies_checked INTEGER,
            series_checked INTEGER,
            matches_found INTEGER,
            newly_streaming INTEGER,
            left_streaming INTEGER,
            duration_seconds REAL
        );
    """)
    conn.close()


def upsert_streaming_item(db_path, tmdb_id, media_type, provider_id, provider_name,
                           title, year=None, arr_id=None, library=None,
                           size_bytes=None, path=None):
    """Insert or update a streaming match. Clears left_at if item returns."""
    now = _now_iso()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(
        "SELECT first_seen FROM streaming_status WHERE tmdb_id=? AND media_type=? AND provider_id=?",
        (tmdb_id, media_type, provider_id),
    )
    row = cursor.fetchone()
    if row:
        conn.execute("""
            UPDATE streaming_status
            SET provider_name=?, title=?, year=?, arr_id=?, library=?,
                size_bytes=?, path=?, last_seen=?, left_at=NULL
            WHERE tmdb_id=? AND media_type=? AND provider_id=?
        """, (provider_name, title, year, arr_id, library, size_bytes, path,
              now, tmdb_id, media_type, provider_id))
        is_new = False
    else:
        conn.execute("""
            INSERT INTO streaming_status
            (tmdb_id, media_type, provider_id, provider_name, title, year,
             arr_id, library, size_bytes, path, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (tmdb_id, media_type, provider_id, provider_name, title, year,
              arr_id, library, size_bytes, path, now, now))
        is_new = True
    conn.commit()
    conn.close()
    return is_new


def get_streaming_item(db_path, tmdb_id, media_type, provider_id):
    """Get a single streaming status record."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(
        "SELECT * FROM streaming_status WHERE tmdb_id=? AND media_type=? AND provider_id=?",
        (tmdb_id, media_type, provider_id),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_left_streaming(db_path, scan_time):
    """Mark items not seen in current scan as left-streaming. Returns list of newly left items."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute("""
        SELECT * FROM streaming_status
        WHERE last_seen < ? AND left_at IS NULL AND deleted_at IS NULL
    """, (scan_time,))
    left_items = [dict(r) for r in cursor.fetchall()]
    if left_items:
        now = _now_iso()
        conn.execute("""
            UPDATE streaming_status SET left_at=?
            WHERE last_seen < ? AND left_at IS NULL AND deleted_at IS NULL
        """, (now, scan_time))
        conn.commit()
    conn.close()
    return left_items


def get_active_matches(db_path):
    """Get all active streaming matches (not left, not deleted)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(
        "SELECT * FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL"
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_left_streaming(db_path):
    """Get items that left streaming but haven't been deleted."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(
        "SELECT * FROM streaming_status WHERE left_at IS NOT NULL AND deleted_at IS NULL"
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def mark_deleted(db_path, tmdb_id, media_type, provider_id):
    """Mark an item as deleted."""
    now = _now_iso()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        UPDATE streaming_status SET deleted_at=?
        WHERE tmdb_id=? AND media_type=? AND provider_id=?
    """, (now, tmdb_id, media_type, provider_id))
    conn.commit()
    conn.close()


def record_scan(db_path, country, movies_checked, series_checked,
                matches_found, newly_streaming, left_streaming, duration_seconds):
    """Record a scan in the history table."""
    now = _now_iso()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        INSERT INTO scan_history
        (timestamp, country, movies_checked, series_checked, matches_found,
         newly_streaming, left_streaming, duration_seconds)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (now, country, movies_checked, series_checked, matches_found,
          newly_streaming, left_streaming, duration_seconds))
    conn.commit()
    conn.close()
