"""SQLite state database for translation tracking."""

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
    conn = _connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS translation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            media_path TEXT NOT NULL,
            source_lang TEXT NOT NULL,
            target_lang TEXT NOT NULL,
            chars_used INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_translation_cooldown
            ON translation_log (media_path, target_lang, created_at);
    """)
    conn.close()


def record_translation(db_path, media_path, source_lang, target_lang,
                        chars_used, status):
    """Record a translation attempt."""
    conn = _connect(db_path)
    conn.execute(
        """INSERT INTO translation_log
           (media_path, source_lang, target_lang, chars_used, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (media_path, source_lang, target_lang, chars_used, status, _now_iso()),
    )
    conn.commit()
    conn.close()


def is_on_cooldown(db_path, media_path, target_lang, cooldown_hours=24):
    """Check if a (media_path, target_lang) pair is within cooldown."""
    conn = _connect(db_path)
    cursor = conn.execute(
        """SELECT 1 FROM translation_log
           WHERE media_path = ? AND target_lang = ?
             AND created_at > datetime('now', ?)
           LIMIT 1""",
        (media_path, target_lang, f"-{cooldown_hours} hours"),
    )
    result = cursor.fetchone() is not None
    conn.close()
    return result


def get_monthly_chars(db_path):
    """Get total characters used this calendar month."""
    conn = _connect(db_path)
    cursor = conn.execute(
        """SELECT COALESCE(SUM(chars_used), 0) as total
           FROM translation_log
           WHERE created_at >= date('now', 'start of month')"""
    )
    total = cursor.fetchone()["total"]
    conn.close()
    return total


def get_recent_translations(db_path, limit=20):
    """Get most recent translation log entries."""
    conn = _connect(db_path)
    cursor = conn.execute(
        "SELECT * FROM translation_log ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows
