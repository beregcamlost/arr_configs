"""SQLite state database for streaming availability tracking."""

import contextlib
import os
import sqlite3
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path):
    """Create database directory and tables if they don't exist."""
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    with contextlib.closing(_connect(db_path)) as conn:
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
                season_count INTEGER,
                streaming_seasons TEXT,
                stale_flagged_at TEXT,
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
            CREATE INDEX IF NOT EXISTS idx_streaming_active
                ON streaming_status (left_at, deleted_at);
            CREATE INDEX IF NOT EXISTS idx_streaming_arr
                ON streaming_status (arr_id, media_type);
            CREATE INDEX IF NOT EXISTS idx_streaming_stale
                ON streaming_status (stale_flagged_at, deleted_at);
            CREATE TABLE IF NOT EXISTS streaming_exclusions (
                tmdb_id INTEGER NOT NULL,
                media_type TEXT NOT NULL,
                title TEXT,
                reason TEXT,
                added_at TEXT NOT NULL,
                PRIMARY KEY (tmdb_id, media_type)
            );
        """)
        # Migration: add columns to existing DBs
        try:
            conn.execute("ALTER TABLE streaming_status ADD COLUMN season_count INTEGER")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE streaming_status ADD COLUMN streaming_seasons TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            conn.execute("ALTER TABLE streaming_status ADD COLUMN stale_flagged_at TEXT")
        except Exception:
            pass


def upsert_streaming_item(db_path, tmdb_id, media_type, provider_id, provider_name,
                           title, year=None, arr_id=None, library=None,
                           size_bytes=None, path=None, season_count=None,
                           streaming_seasons=None):
    """Insert or update a streaming match. Clears left_at if item returns.

    Args:
        season_count: Number of seasons owned locally (from Sonarr).
        streaming_seasons: JSON string of season numbers available on this provider.
    """
    now = _now_iso()
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "SELECT first_seen FROM streaming_status WHERE tmdb_id=? AND media_type=? AND provider_id=?",
            (tmdb_id, media_type, provider_id),
        )
        row = cursor.fetchone()
        if row:
            conn.execute("""
                UPDATE streaming_status
                SET provider_name=?, title=?, year=?, arr_id=?, library=?,
                    size_bytes=?, path=?, last_seen=?, left_at=NULL,
                    season_count=COALESCE(?, season_count),
                    streaming_seasons=COALESCE(?, streaming_seasons)
                WHERE tmdb_id=? AND media_type=? AND provider_id=?
            """, (provider_name, title, year, arr_id, library, size_bytes, path,
                  now, season_count, streaming_seasons,
                  tmdb_id, media_type, provider_id))
            is_new = False
        else:
            conn.execute("""
                INSERT INTO streaming_status
                (tmdb_id, media_type, provider_id, provider_name, title, year,
                 arr_id, library, size_bytes, path, first_seen, last_seen,
                 season_count, streaming_seasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tmdb_id, media_type, provider_id, provider_name, title, year,
                  arr_id, library, size_bytes, path, now, now,
                  season_count, streaming_seasons))
            is_new = True
        conn.commit()
    return is_new


def get_streaming_item(db_path, tmdb_id, media_type, provider_id):
    """Get a single streaming status record."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "SELECT * FROM streaming_status WHERE tmdb_id=? AND media_type=? AND provider_id=?",
            (tmdb_id, media_type, provider_id),
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def mark_left_streaming(db_path, scan_time):
    """Mark items not seen in current scan as left-streaming. Returns list of newly left items."""
    with contextlib.closing(_connect(db_path)) as conn:
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
    return left_items


def get_active_matches(db_path):
    """Get all active streaming matches (not left, not deleted)."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "SELECT * FROM streaming_status WHERE left_at IS NULL AND deleted_at IS NULL"
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return rows


def get_left_streaming(db_path):
    """Get items that left streaming but haven't been deleted."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "SELECT * FROM streaming_status WHERE left_at IS NOT NULL AND deleted_at IS NULL"
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return rows


def touch_keep_local_items(db_path, arr_id_types, timestamp):
    """Update last_seen and clear left_at for keep-local items.

    Prevents keep-local items from being flagged as left-streaming,
    and clears the flag if they were already marked.

    Args:
        arr_id_types: list of (arr_id, media_type) tuples.
        timestamp: ISO timestamp to set as last_seen.

    Returns:
        Number of DB rows updated.
    """
    if not arr_id_types:
        return 0
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.executemany(
            "UPDATE streaming_status SET last_seen=?, left_at=NULL "
            "WHERE arr_id=? AND media_type=? AND deleted_at IS NULL",
            [(timestamp, arr_id, media_type) for arr_id, media_type in arr_id_types],
        )
        count = cursor.rowcount
        conn.commit()
    return count


def update_streaming_seasons(db_path, tmdb_id, provider_id, streaming_seasons, season_count=None):
    """Update streaming_seasons (and optionally season_count) for a specific item.

    Args:
        streaming_seasons: JSON string of season numbers available on this provider.
        season_count: Number of seasons owned locally (optional update).
    """
    with contextlib.closing(_connect(db_path)) as conn:
        if season_count is not None:
            conn.execute("""
                UPDATE streaming_status
                SET streaming_seasons=?, season_count=?
                WHERE tmdb_id=? AND media_type='tv' AND provider_id=?
                  AND deleted_at IS NULL
            """, (streaming_seasons, season_count, tmdb_id, provider_id))
        else:
            conn.execute("""
                UPDATE streaming_status
                SET streaming_seasons=?
                WHERE tmdb_id=? AND media_type='tv' AND provider_id=?
                  AND deleted_at IS NULL
            """, (streaming_seasons, tmdb_id, provider_id))
        conn.commit()


def mark_deleted(db_path, tmdb_id, media_type, provider_id=None):
    """Mark an item as deleted.

    If provider_id is None, marks all rows for the given tmdb_id+media_type.
    """
    now = _now_iso()
    with contextlib.closing(_connect(db_path)) as conn:
        if provider_id is None:
            conn.execute("""
                UPDATE streaming_status SET deleted_at=?
                WHERE tmdb_id=? AND media_type=?
            """, (now, tmdb_id, media_type))
        else:
            conn.execute("""
                UPDATE streaming_status SET deleted_at=?
                WHERE tmdb_id=? AND media_type=? AND provider_id=?
            """, (now, tmdb_id, media_type, provider_id))
        conn.commit()


def flag_stale_item(db_path, tmdb_id, media_type):
    """Flag an item as stale. Does not overwrite existing flag timestamp."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "UPDATE streaming_status SET stale_flagged_at = ? "
            "WHERE tmdb_id = ? AND media_type = ? AND stale_flagged_at IS NULL "
            "AND deleted_at IS NULL",
            (_now_iso(), tmdb_id, media_type),
        )
        count = cursor.rowcount
        conn.commit()
    return count


def unflag_stale_item(db_path, tmdb_id, media_type):
    """Clear stale flag (item was watched or no longer qualifies)."""
    with contextlib.closing(_connect(db_path)) as conn:
        conn.execute(
            "UPDATE streaming_status SET stale_flagged_at = NULL "
            "WHERE tmdb_id = ? AND media_type = ?",
            (tmdb_id, media_type),
        )
        conn.commit()


def get_stale_flagged_items(db_path):
    """Get all items currently flagged as stale (not deleted)."""
    with contextlib.closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM streaming_status "
            "WHERE stale_flagged_at IS NOT NULL AND deleted_at IS NULL "
            "ORDER BY stale_flagged_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_stale_candidate_paths(db_path):
    """Get dir-level paths of all stale-flagged items (for codec manager)."""
    with contextlib.closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT path FROM streaming_status "
            "WHERE stale_flagged_at IS NOT NULL AND deleted_at IS NULL "
            "AND path IS NOT NULL"
        ).fetchall()
    return [r[0] for r in rows]


def record_scan(db_path, country, movies_checked, series_checked,
                matches_found, newly_streaming, left_streaming, duration_seconds):
    """Record a scan in the history table."""
    now = _now_iso()
    with contextlib.closing(_connect(db_path)) as conn:
        conn.execute("""
            INSERT INTO scan_history
            (timestamp, country, movies_checked, series_checked, matches_found,
             newly_streaming, left_streaming, duration_seconds)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (now, country, movies_checked, series_checked, matches_found,
              newly_streaming, left_streaming, duration_seconds))
        conn.commit()


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

    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()]
    return rows


def get_scan_history(db_path, limit=5):
    """Get most recent scan history entries."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "SELECT * FROM scan_history ORDER BY scan_id DESC LIMIT ?", (limit,)
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return rows


def add_exclusion(db_path, tmdb_id, media_type, title=None, reason=None):
    """Add an item to the streaming exclusion list."""
    now = _now_iso()
    with contextlib.closing(_connect(db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO streaming_exclusions "
            "(tmdb_id, media_type, title, reason, added_at) VALUES (?, ?, ?, ?, ?)",
            (tmdb_id, media_type, title, reason, now),
        )
        conn.commit()


def remove_exclusion(db_path, tmdb_id, media_type):
    """Remove an item from the streaming exclusion list. Returns rows deleted."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "DELETE FROM streaming_exclusions WHERE tmdb_id=? AND media_type=?",
            (tmdb_id, media_type),
        )
        count = cursor.rowcount
        conn.commit()
    return count


def get_exclusions(db_path):
    """Get set of (tmdb_id, media_type) for all excluded items."""
    with contextlib.closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT tmdb_id, media_type FROM streaming_exclusions"
        ).fetchall()
    return {(r[0], r[1]) for r in rows}


def list_exclusions(db_path):
    """Get all exclusions as list of dicts for display."""
    with contextlib.closing(_connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM streaming_exclusions ORDER BY added_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_summary_stats(db_path):
    """Get summary statistics for active streaming matches.

    Returns dict with: total_active, total_size_bytes, by_provider, by_library, last_scan.
    """
    with contextlib.closing(_connect(db_path)) as conn:
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

    return {
        "total_active": total_active,
        "total_size_bytes": total_size_bytes,
        "by_provider": by_provider,
        "by_library": by_library,
        "last_scan": last_scan,
    }
