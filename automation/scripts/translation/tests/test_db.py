"""Tests for translation state database."""

from translation.db import (
    init_db,
    record_translation,
    is_on_cooldown,
    is_permanently_failed,
    get_monthly_chars,
    get_monthly_chars_by_provider,
    get_recent_translations,
    get_daily_requests,
    get_monthly_chars_by_key,
    get_daily_requests_by_key,
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


# --- Fix 1: provider-scoped get_monthly_chars ---

def test_get_monthly_chars_no_filter_sums_all_providers(tmp_db):
    """get_monthly_chars() with no provider arg sums across all providers."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success", "deepl")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success", "gemini")
    record_translation(tmp_db, "/path/v3.mkv", "en", "de", 500, "success", "google")
    total = get_monthly_chars(tmp_db)
    assert total == 3500


def test_get_monthly_chars_provider_deepl(tmp_db):
    """get_monthly_chars(provider='deepl') only counts DeepL rows."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success", "deepl")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success", "gemini")
    record_translation(tmp_db, "/path/v3.mkv", "en", "de", 500, "success", "google")
    assert get_monthly_chars(tmp_db, provider="deepl") == 1000


def test_get_monthly_chars_provider_gemini(tmp_db):
    """get_monthly_chars(provider='gemini') only counts Gemini rows."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success", "deepl")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success", "gemini")
    record_translation(tmp_db, "/path/v3.mkv", "en", "de", 500, "success", "google")
    assert get_monthly_chars(tmp_db, provider="gemini") == 2000


def test_get_monthly_chars_provider_google(tmp_db):
    """get_monthly_chars(provider='google') only counts Google rows."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success", "deepl")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success", "gemini")
    record_translation(tmp_db, "/path/v3.mkv", "en", "de", 500, "success", "google")
    assert get_monthly_chars(tmp_db, provider="google") == 500


def test_get_monthly_chars_provider_zero_when_no_rows(tmp_db):
    """get_monthly_chars returns 0 when no rows exist for that provider."""
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success", "deepl")
    assert get_monthly_chars(tmp_db, provider="gemini") == 0
    assert get_monthly_chars(tmp_db, provider="google") == 0


# --- Fix 3: permanent-skip for NoneType parse failures ---

def test_is_permanently_failed_nonetype_status(tmp_db):
    """is_permanently_failed returns True when a NoneType error row exists."""
    record_translation(
        tmp_db, "/path/bad.mkv", "en", "es", 0,
        "error: 'NoneType' object has no attribute 'encode'",
    )
    assert is_permanently_failed(tmp_db, "/path/bad.mkv", "es") is True


def test_is_permanently_failed_json_nonetype_status(tmp_db):
    """is_permanently_failed triggers on 'the JSON object ... NoneType' errors."""
    record_translation(
        tmp_db, "/path/bad.mkv", "en", "es", 0,
        "error: the JSON object must be str, bytes or bytearray, not NoneType",
    )
    assert is_permanently_failed(tmp_db, "/path/bad.mkv", "es") is True


def test_is_permanently_failed_normal_error_not_triggered(tmp_db):
    """is_permanently_failed returns False for transient (non-NoneType) errors."""
    record_translation(tmp_db, "/path/ok.mkv", "en", "es", 0, "error: timeout")
    assert is_permanently_failed(tmp_db, "/path/ok.mkv", "es") is False


def test_is_permanently_failed_success_not_triggered(tmp_db):
    """is_permanently_failed returns False for successfully translated files."""
    record_translation(tmp_db, "/path/ok.mkv", "en", "es", 1000, "success", "deepl")
    assert is_permanently_failed(tmp_db, "/path/ok.mkv", "es") is False


def test_is_permanently_failed_different_lang_not_triggered(tmp_db):
    """is_permanently_failed is scoped to (media_path, target_lang) pair."""
    record_translation(
        tmp_db, "/path/bad.mkv", "en", "es", 0,
        "error: 'NoneType' object has no attribute 'encode'",
    )
    # Same file, different target lang must not be blocked
    assert is_permanently_failed(tmp_db, "/path/bad.mkv", "fr") is False


def test_is_permanently_failed_different_path_not_triggered(tmp_db):
    """is_permanently_failed is scoped per file path."""
    record_translation(
        tmp_db, "/path/bad.mkv", "en", "es", 0,
        "error: 'NoneType' object has no attribute 'encode'",
    )
    assert is_permanently_failed(tmp_db, "/path/other.mkv", "es") is False


def test_cooldown_still_works_for_transient_failures(tmp_db):
    """Normal transient errors (non-NoneType) are subject to cooldown, not permanent skip."""
    record_translation(tmp_db, "/path/ok.mkv", "en", "es", 0, "error: network timeout")
    # Cooldown should fire (row is recent)
    assert is_on_cooldown(tmp_db, "/path/ok.mkv", "es", cooldown_hours=24) is True
    # But permanent skip must NOT fire
    assert is_permanently_failed(tmp_db, "/path/ok.mkv", "es") is False


# --- Per-key budget tracking ---

def test_init_db_has_key_index_column(tmp_db):
    """init_db creates key_index column."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    cursor = conn.execute("PRAGMA table_info(translation_log)")
    columns = {row[1] for row in cursor.fetchall()}
    assert "key_index" in columns
    conn.close()


def test_init_db_key_index_migration_idempotent(tmp_path):
    """Running init_db twice on an existing DB with key_index does not fail."""
    db = str(tmp_path / "migration_test.db")
    init_db(db)
    init_db(db)  # second call must not raise
    record_translation(db, "/p/v.mkv", "en", "es", 100, "success", "gemini", key_index=2)
    rows = get_recent_translations(db)
    assert rows[0]["key_index"] == 2


def test_record_translation_stores_key_index(tmp_db):
    """record_translation persists key_index."""
    record_translation(tmp_db, "/p/v.mkv", "en", "es", 500, "success", "gemini", key_index=3)
    rows = get_recent_translations(tmp_db)
    assert rows[0]["key_index"] == 3


def test_record_translation_key_index_defaults_null(tmp_db):
    """record_translation stores NULL key_index when not provided."""
    record_translation(tmp_db, "/p/v.mkv", "en", "es", 100, "success", "deepl")
    rows = get_recent_translations(tmp_db)
    assert rows[0]["key_index"] is None


def test_get_monthly_chars_per_key_filter(tmp_db):
    """get_monthly_chars with key_index only counts rows for that key."""
    record_translation(tmp_db, "/p/v1.mkv", "en", "es", 1000, "success", "gemini", key_index=0)
    record_translation(tmp_db, "/p/v2.mkv", "en", "fr", 2000, "success", "gemini", key_index=1)
    record_translation(tmp_db, "/p/v3.mkv", "en", "de", 500,  "success", "gemini", key_index=0)
    assert get_monthly_chars(tmp_db, provider="gemini", key_index=0) == 1500
    assert get_monthly_chars(tmp_db, provider="gemini", key_index=1) == 2000


def test_get_monthly_chars_key_index_zero_for_missing(tmp_db):
    """get_monthly_chars returns 0 for a key_index with no rows."""
    record_translation(tmp_db, "/p/v.mkv", "en", "es", 1000, "success", "gemini", key_index=0)
    assert get_monthly_chars(tmp_db, provider="gemini", key_index=5) == 0


def test_get_daily_requests_per_key_filter(tmp_db):
    """get_daily_requests with key_index counts only that key's rows today."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    for path, ki in [("/a.mkv", 0), ("/b.mkv", 0), ("/c.mkv", 1)]:
        conn.execute(
            """INSERT INTO translation_log
               (media_path, source_lang, target_lang, chars_used, status, created_at, provider, key_index)
               VALUES (?, 'en', 'es', 100, 'success', ?, 'gemini', ?)""",
            (path, today, ki),
        )
    conn.commit()
    conn.close()
    assert get_daily_requests(tmp_db, "gemini", key_index=0) == 2
    assert get_daily_requests(tmp_db, "gemini", key_index=1) == 1
    assert get_daily_requests(tmp_db, "gemini", key_index=2) == 0


def test_idx_per_key_budget_created(tmp_db):
    """init_db creates idx_per_key_budget index on translation_log."""
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_per_key_budget'"
    )
    assert cursor.fetchone() is not None
    conn.close()


# --- Grouped bulk helpers ---

def test_get_monthly_chars_by_key_groups_correctly(tmp_db):
    """get_monthly_chars_by_key returns per-key totals for the current month."""
    record_translation(tmp_db, "/p/v1.mkv", "en", "es", 1000, "success", "gemini", key_index=0)
    record_translation(tmp_db, "/p/v2.mkv", "en", "fr", 2000, "success", "gemini", key_index=1)
    record_translation(tmp_db, "/p/v3.mkv", "en", "de", 500,  "success", "gemini", key_index=0)
    result = get_monthly_chars_by_key(tmp_db, "gemini")
    assert result == {0: 1500, 1: 2000}


def test_get_monthly_chars_by_key_excludes_null_key_index(tmp_db):
    """Rows with NULL key_index are excluded from grouped result."""
    record_translation(tmp_db, "/p/old.mkv", "en", "es", 9999, "success", "gemini")
    record_translation(tmp_db, "/p/new.mkv", "en", "es", 100,  "success", "gemini", key_index=0)
    result = get_monthly_chars_by_key(tmp_db, "gemini")
    assert result == {0: 100}


def test_get_monthly_chars_by_key_excludes_other_providers(tmp_db):
    """get_monthly_chars_by_key is scoped to the requested provider."""
    record_translation(tmp_db, "/p/v1.mkv", "en", "es", 1000, "success", "deepl", key_index=0)
    record_translation(tmp_db, "/p/v2.mkv", "en", "fr", 500,  "success", "gemini", key_index=0)
    assert get_monthly_chars_by_key(tmp_db, "deepl") == {0: 1000}
    assert get_monthly_chars_by_key(tmp_db, "gemini") == {0: 500}


def test_get_monthly_chars_by_key_empty_when_no_rows(tmp_db):
    """get_monthly_chars_by_key returns empty dict when no rows exist."""
    assert get_monthly_chars_by_key(tmp_db, "gemini") == {}


def test_get_daily_requests_by_key_groups_correctly(tmp_db):
    """get_daily_requests_by_key returns per-key counts for today."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    for path, ki in [("/a.mkv", 0), ("/b.mkv", 0), ("/c.mkv", 1)]:
        conn.execute(
            """INSERT INTO translation_log
               (media_path, source_lang, target_lang, chars_used, status, created_at, provider, key_index)
               VALUES (?, 'en', 'es', 100, 'success', ?, 'gemini', ?)""",
            (path, today, ki),
        )
    conn.commit()
    conn.close()
    result = get_daily_requests_by_key(tmp_db, "gemini")
    assert result == {0: 2, 1: 1}


def test_get_daily_requests_by_key_excludes_null_key_index(tmp_db):
    """Rows with NULL key_index are excluded from grouped result."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    import sqlite3
    conn = sqlite3.connect(tmp_db)
    conn.execute(
        """INSERT INTO translation_log
           (media_path, source_lang, target_lang, chars_used, status, created_at, provider, key_index)
           VALUES ('/a.mkv', 'en', 'es', 100, 'success', ?, 'gemini', NULL)""",
        (today,),
    )
    conn.commit()
    conn.close()
    assert get_daily_requests_by_key(tmp_db, "gemini") == {}


def test_get_monthly_chars_null_key_index_excluded_from_per_key_query(tmp_db):
    """Rows with NULL key_index (old data) are not counted in per-key queries."""
    # Old row without key_index
    record_translation(tmp_db, "/p/old.mkv", "en", "es", 9999, "success", "gemini")
    # New row with key_index=0
    record_translation(tmp_db, "/p/new.mkv", "en", "es", 100, "success", "gemini", key_index=0)
    # Per-key query must only see the 100-char row, not the 9999-char legacy row
    assert get_monthly_chars(tmp_db, provider="gemini", key_index=0) == 100
