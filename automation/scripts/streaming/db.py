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


def get_active_matches_filtered(db_path, provider=None, library=None,
                                 min_size=None, since_days=None, sort_by="title"):
    """Get active streaming matches with optional filters.

    Args:
        provider: Filter by provider name (case-insensitive).
        library: Filter by library name (case-insensitive).
        min_size: Minimum size in bytes.
        since_days: Only items first seen within N days.
        sort_by: One of 'title', 'size', 'date', 'provider'.
    """
    sort_map = {
        "title": "title ASC",
        "size": "COALESCE(size_bytes, 0) DESC",
        "date": "first_seen DESC",
        "provider": "provider_name ASC, title ASC",
    }
    order = sort_map.get(sort_by, "title ASC")

    clauses = ["left_at IS NULL", "deleted_at IS NULL"]
    params = []

    if provider:
        clauses.append("LOWER(provider_name) = LOWER(?)")
        params.append(provider)
    if library:
        clauses.append("LOWER(library) = LOWER(?)")
        params.append(library)
    if min_size is not None:
        clauses.append("COALESCE(size_bytes, 0) >= ?")
        params.append(min_size)
    if since_days is not None:
        clauses.append("first_seen >= datetime('now', ?)")
        params.append(f"-{since_days} days")

    sql = f"SELECT * FROM streaming_status WHERE {' AND '.join(clauses)} ORDER BY {order}"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(sql, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_scan_history(db_path, limit=5):
    """Get most recent scan history entries."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    cursor = conn.execute(
        "SELECT * FROM scan_history ORDER BY scan_id DESC LIMIT ?", (limit,)
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_summary_stats(db_path):
    """Get summary statistics for active streaming matches.

    Returns dict with: total_active, total_size_bytes, by_provider, by_library, last_scan.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")

    # Total active matches and size
    row = conn.execute("""
        SELECT COUNT(*) as total, COALESCE(SUM(COALESCE(size_bytes, 0)), 0) as size
        FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL
    """).fetchone()
    total_active = row["total"]
    total_size_bytes = row["size"]

    # By provider
    by_provider = [dict(r) for r in conn.execute("""
        SELECT provider_name, COUNT(*) as count,
               COALESCE(SUM(COALESCE(size_bytes, 0)), 0) as size_bytes
        FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL
        GROUP BY provider_name ORDER BY count DESC
    """).fetchall()]

    # By library — deduplicated count (an item on multiple providers counts once)
    by_library = [dict(r) for r in conn.execute("""
        SELECT library, COUNT(DISTINCT tmdb_id || ':' || media_type) as count
        FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL
        GROUP BY library ORDER BY count DESC
    """).fetchall()]

    # Last scan
    last_scan_row = conn.execute(
        "SELECT * FROM scan_history ORDER BY scan_id DESC LIMIT 1"
    ).fetchone()
    last_scan = dict(last_scan_row) if last_scan_row else None

    conn.close()

    return {
        "total_active": total_active,
        "total_size_bytes": total_size_bytes,
        "by_provider": by_provider,
        "by_library": by_library,
        "last_scan": last_scan,
    }
