"""Tests for translation state database."""

from translation.db import (
    init_db,
    record_translation,
    is_on_cooldown,
    get_monthly_chars,
    get_monthly_chars_by_provider,
    get_recent_translations,
)


def test_init_db_creates_table(tmp_db):
    """init_db creates translation_log table."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='translation_log'"
    )
    assert cursor.fetchone() is not None
    conn.close()


def test_init_db_has_provider_column(tmp_db):
    """init_db creates provider column."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    cursor = conn.execute("PRAGMA table_info(translation_log)")
    columns = {row[1] for row in cursor.fetchall()}
    assert "provider" in columns
    conn.close()


def test_record_translation(tmp_db):
    """record_translation inserts a row."""
    record_translation(
        tmp_db,
        media_path="/path/to/video.mkv",
        source_lang="en",
        target_lang="es",
        chars_used=1500,
        status="success",
    )
    rows = get_recent_translations(tmp_db, limit=10)
    assert len(rows) == 1
    assert rows[0]["media_path"] == "/path/to/video.mkv"
    assert rows[0]["chars_used"] == 1500
    assert rows[0]["status"] == "success"
    assert rows[0]["provider"] == "deepl"  # default


def test_record_translation_with_provider(tmp_db):
    """record_translation stores provider field."""
    record_translation(tmp_db, "/path/v.mkv", "en", "es", 1000, "success", "google")
    rows = get_recent_translations(tmp_db, limit=10)
    assert rows[0]["provider"] == "google"


def test_cooldown_active(tmp_db):
    """is_on_cooldown returns True within cooldown window."""
    record_translation(tmp_db, "/path/video.mkv", "en", "es", 100, "success")
    assert is_on_cooldown(tmp_db, "/path/video.mkv", "es", cooldown_hours=24) is True


def test_cooldown_inactive_different_lang(tmp_db):
    """is_on_cooldown returns False for different target language."""
    record_translation(tmp_db, "/path/video.mkv", "en", "es", 100, "success")
    assert is_on_cooldown(tmp_db, "/path/video.mkv", "fr", cooldown_hours=24) is False


def test_cooldown_inactive_different_path(tmp_db):
    """is_on_cooldown returns False for different file."""
    record_translation(tmp_db, "/path/video1.mkv", "en", "es", 100, "success")
    assert is_on_cooldown(tmp_db, "/path/video2.mkv", "es", cooldown_hours=24) is False


def test_get_monthly_chars(tmp_db):
    """get_monthly_chars sums chars_used for current month."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success")
    record_translation(tmp_db, "/path/v3.mkv", "en", "es", 500, "failed")
    total = get_monthly_chars(tmp_db)
    assert total == 3500  # includes failed — chars were still consumed


def test_get_monthly_chars_by_provider(tmp_db):
    """get_monthly_chars_by_provider groups by provider."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success", "deepl")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success", "google")
    record_translation(tmp_db, "/path/v3.mkv", "en", "de", 500, "success", "deepl")
    breakdown = get_monthly_chars_by_provider(tmp_db)
    assert breakdown["deepl"] == 1500
    assert breakdown["google"] == 2000


def test_get_recent_translations_limit(tmp_db):
    """get_recent_translations respects limit."""
    for i in range(5):
        record_translation(tmp_db, f"/path/v{i}.mkv", "en", "es", 100, "success")
    rows = get_recent_translations(tmp_db, limit=3)
    assert len(rows) == 3


def test_init_db_migration_idempotent(tmp_path):
    """Running init_db twice doesn't fail (migration is safe)."""
    db_path = str(tmp_path / "test_migration.db")
    init_db(db_path)
    init_db(db_path)  # second call should be safe
    record_translation(db_path, "/path/v.mkv", "en", "es", 100, "success", "google")
    rows = get_recent_translations(db_path)
    assert rows[0]["provider"] == "google"
