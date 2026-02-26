"""Tests for the SQLite state database."""

import pytest

from streaming.db import (
    get_active_matches,
    get_left_streaming,
    get_streaming_item,
    init_db,
    mark_deleted,
    mark_left_streaming,
    record_scan,
    upsert_streaming_item,
)


class TestInitDb:
    def test_creates_tables(self, tmp_db):
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        conn.close()
        assert "streaming_status" in tables
        assert "scan_history" in tables

    def test_idempotent(self, tmp_db):
        # Calling init_db again should not fail
        init_db(tmp_db)


class TestUpsertAndGet:
    def test_insert_new_item(self, tmp_db):
        is_new = upsert_streaming_item(
            tmp_db, tmdb_id=550, media_type="movie", provider_id=8,
            provider_name="Netflix", title="Fight Club", year=1999,
            arr_id=42, library="movies", size_bytes=5_000_000_000,
            path="/media/movies/Fight Club (1999)/movie.mkv",
        )
        assert is_new is True
        item = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item["title"] == "Fight Club"
        assert item["provider_name"] == "Netflix"
        assert item["left_at"] is None
        assert item["deleted_at"] is None

    def test_update_existing_item(self, tmp_db):
        upsert_streaming_item(
            tmp_db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
        )
        is_new = upsert_streaming_item(
            tmp_db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
            arr_id=99, size_bytes=6_000_000_000,
        )
        assert is_new is False
        item = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item["arr_id"] == 99
        assert item["size_bytes"] == 6_000_000_000

    def test_get_nonexistent(self, tmp_db):
        assert get_streaming_item(tmp_db, 999, "movie", 8) is None

    def test_different_providers_separate_rows(self, tmp_db):
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club")
        upsert_streaming_item(tmp_db, 550, "movie", 337, "Disney Plus", "Fight Club")
        assert get_streaming_item(tmp_db, 550, "movie", 8) is not None
        assert get_streaming_item(tmp_db, 550, "movie", 337) is not None


class TestLeftStreaming:
    def test_mark_left_streaming(self, tmp_db):
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club")
        # Simulate: item was last seen before scan_time
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "UPDATE streaming_status SET last_seen='2026-01-01T00:00:00Z'"
        )
        conn.commit()
        conn.close()

        left = mark_left_streaming(tmp_db, "2026-02-01T00:00:00Z")
        assert len(left) == 1
        assert left[0]["title"] == "Fight Club"

        item = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item["left_at"] is not None

    def test_returns_to_streaming_clears_left_at(self, tmp_db):
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club")
        # Mark as left
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "UPDATE streaming_status SET last_seen='2026-01-01T00:00:00Z'"
        )
        conn.commit()
        conn.close()
        mark_left_streaming(tmp_db, "2026-02-01T00:00:00Z")

        # Re-upsert (came back)
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club")
        item = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item["left_at"] is None

    def test_get_left_streaming(self, tmp_db):
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club")
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute(
            "UPDATE streaming_status SET last_seen='2026-01-01T00:00:00Z'"
        )
        conn.commit()
        conn.close()
        mark_left_streaming(tmp_db, "2026-02-01T00:00:00Z")

        left = get_left_streaming(tmp_db)
        assert len(left) == 1


class TestActiveMatches:
    def test_get_active_matches(self, tmp_db):
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club")
        upsert_streaming_item(tmp_db, 600, "movie", 337, "Disney Plus", "Toy Story")
        matches = get_active_matches(tmp_db)
        assert len(matches) == 2

    def test_excludes_deleted(self, tmp_db):
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club")
        mark_deleted(tmp_db, 550, "movie", 8)
        matches = get_active_matches(tmp_db)
        assert len(matches) == 0


class TestScanHistory:
    def test_record_scan(self, tmp_db):
        record_scan(tmp_db, "CL", 671, 149, 50, 5, 2, 15.3)
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM scan_history").fetchone()
        conn.close()
        assert row["movies_checked"] == 671
        assert row["matches_found"] == 50
        assert row["duration_seconds"] == 15.3
