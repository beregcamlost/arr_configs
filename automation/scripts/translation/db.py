"""SQLite state database for translation tracking."""

import contextlib
import os
import sqlite3
from datetime import datetime, timezone

from translation.config import PROVIDER_DEEPL, PROVIDER_GEMINI, PROVIDER_GOOGLE  # noqa: F401


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
        cursor = conn.execute("PRAGMA table_info(translation_log)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "provider" not in columns:
            # SQL literal must be a string; PROVIDER_DEEPL is the source of truth
            conn.execute(
                "ALTER TABLE translation_log "
                "ADD COLUMN provider TEXT NOT NULL DEFAULT 'deepl'"
            )
            conn.commit()
        if "key_index" not in columns:
            conn.execute(
                "ALTER TABLE translation_log "
                "ADD COLUMN key_index INTEGER DEFAULT NULL"
            )
            conn.commit()
        conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_per_key_budget
            ON translation_log (provider, key_index, created_at)
        ''')
        conn.commit()


def record_translation(db_path, media_path, source_lang, target_lang,
                        chars_used, status, provider=PROVIDER_OLLAMA, key_index=None):
    """Record a translation attempt."""
    with contextlib.closing(_connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO translation_log
               (media_path, source_lang, target_lang, chars_used, status,
                created_at, provider, key_index)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (media_path, source_lang, target_lang, chars_used, status,
             _now_iso(), provider, key_index),
        )
        conn.commit()


_TRANSIENT_PREFIXES = ("error: timed out", "error: Ollama unreachable", "error: Connection")


def _get_cooldown_hours(db_path, media_path, target_lang):
    with contextlib.closing(_connect(db_path)) as conn:
        row = conn.execute(
            """SELECT status FROM translation_log
               WHERE media_path = ? AND target_lang = ?
                 AND status != 'no_source' AND status != 'success'
               ORDER BY created_at DESC LIMIT 1""",
            (media_path, target_lang),
        ).fetchone()
    if row and any(row[0].startswith(p) for p in _TRANSIENT_PREFIXES):
        return 1
    return 24


def is_on_cooldown(db_path, media_path, target_lang, cooldown_hours=None):
    if cooldown_hours is None:
        cooldown_hours = _get_cooldown_hours(db_path, media_path, target_lang)
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            """SELECT 1 FROM translation_log
               WHERE media_path = ? AND target_lang = ?
                 AND created_at > datetime('now', ?)
                 AND status != 'no_source'
               LIMIT 1""",
            (media_path, target_lang, f"-{cooldown_hours} hours"),
        )
        result = cursor.fetchone() is not None
    return result


def get_monthly_chars(db_path, provider=None, key_index=None):
    """Get total characters used this calendar month.

    provider: when set, restricts to a single provider (e.g. 'deepl').
    key_index: when set alongside provider, further restricts to a single API key.
    Default None values sum across all providers / all keys.
    """
    with contextlib.closing(_connect(db_path)) as conn:
        if provider is not None and key_index is not None:
            cursor = conn.execute(
                """SELECT COALESCE(SUM(chars_used), 0) as total
                   FROM translation_log
                   WHERE created_at >= date('now', 'start of month')
                     AND provider = ?
                     AND key_index = ?""",
                (provider, key_index),
            )
        elif provider is not None:
            cursor = conn.execute(
                """SELECT COALESCE(SUM(chars_used), 0) as total
                   FROM translation_log
                   WHERE created_at >= date('now', 'start of month')
                     AND provider = ?""",
                (provider,),
            )
        else:
            cursor = conn.execute(
                """SELECT COALESCE(SUM(chars_used), 0) as total
                   FROM translation_log
                   WHERE created_at >= date('now', 'start of month')"""
            )
        total = cursor.fetchone()["total"]
    return total


def is_permanently_failed(db_path, media_path, target_lang):
    """Return True if (media_path, target_lang) has a prior NoneType parse failure.

    NoneType errors arise from source SRTs that cannot be parsed — retrying
    never helps, so these are skipped permanently rather than cycling every 24h.
    """
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            """SELECT 1 FROM translation_log
               WHERE media_path = ? AND target_lang = ?
                 AND (status LIKE '%NoneType%' OR status LIKE '%the JSON%')
               LIMIT 1""",
            (media_path, target_lang),
        )
        return cursor.fetchone() is not None


def get_monthly_chars_by_provider(db_path):
    """Get characters used this month grouped by provider."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            """SELECT provider, COALESCE(SUM(chars_used), 0) as total
               FROM translation_log
               WHERE created_at >= date('now', 'start of month')
               GROUP BY provider"""
        )
        return {row["provider"]: row["total"] for row in cursor.fetchall()}


def get_daily_requests(db_path, provider, key_index=None):
    """Count translation attempts for a provider today (UTC calendar day).

    key_index: when set, restricts count to a single API key's rows.
    Each row represents one file-level translation call (which may involve
    multiple batch API calls internally). Used for daily request-budget checks
    that guard against free-tier overage.
    """
    with contextlib.closing(_connect(db_path)) as conn:
        if key_index is not None:
            cursor = conn.execute(
                """SELECT COUNT(*) as total
                   FROM translation_log
                   WHERE provider = ?
                     AND key_index = ?
                     AND created_at >= datetime('now', 'start of day')""",
                (provider, key_index),
            )
        else:
            cursor = conn.execute(
                """SELECT COUNT(*) as total
                   FROM translation_log
                   WHERE provider = ?
                     AND created_at >= datetime('now', 'start of day')""",
                (provider,),
            )
        return cursor.fetchone()["total"]


def get_monthly_chars_by_key(db_path, provider):
    """Return {key_index: chars_used} for the current calendar month."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            """SELECT key_index, COALESCE(SUM(chars_used), 0) as total
               FROM translation_log
               WHERE provider = ?
                 AND created_at >= date('now', 'start of month')
                 AND key_index IS NOT NULL
               GROUP BY key_index""",
            (provider,),
        )
        return {row["key_index"]: row["total"] for row in cursor.fetchall()}


def get_daily_requests_by_key(db_path, provider):
    """Return {key_index: request_count} for today."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            """SELECT key_index, COUNT(*) as total
               FROM translation_log
               WHERE provider = ?
                 AND created_at >= datetime('now', 'start of day')
                 AND key_index IS NOT NULL
               GROUP BY key_index""",
            (provider,),
        )
        return {row["key_index"]: row["total"] for row in cursor.fetchall()}


def get_recent_translations(db_path, limit=20):
    """Get most recent translation log entries."""
    with contextlib.closing(_connect(db_path)) as conn:
        cursor = conn.execute(
            "SELECT * FROM translation_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in cursor.fetchall()]
    return rows
