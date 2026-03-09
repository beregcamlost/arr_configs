"""Tests for the SQLite state database."""

import pytest

from streaming.db import (
    add_exclusion,
    flag_stale_item,
    get_active_matches,
    get_active_matches_filtered,
    get_exclusions,
    get_left_streaming,
    get_scan_history,
    get_stale_candidate_paths,
    get_stale_flagged_items,
    get_streaming_item,
    get_summary_stats,
    init_db,
    list_exclusions,
    mark_deleted,
    mark_left_streaming,
    record_scan,
    remove_exclusion,
    touch_keep_local_items,
    unflag_stale_item,
    update_streaming_seasons,
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
        assert "streaming_exclusions" in tables

    def test_idempotent(self, tmp_db):
        # Calling init_db again should not fail
        init_db(tmp_db)

    def test_has_season_columns(self, tmp_db):
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(streaming_status)").fetchall()]
        conn.close()
        assert "season_count" in cols
        assert "streaming_seasons" in cols

    def test_has_stale_flagged_at_column(self, tmp_db):
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(streaming_status)").fetchall()]
        conn.close()
        assert "stale_flagged_at" in cols


class TestStaleFlagging:
    def _insert_item(self, db, tmdb_id=550, provider_id=8, path="/media/movies/Fight Club (1999)"):
        upsert_streaming_item(
            db, tmdb_id=tmdb_id, media_type="movie", provider_id=provider_id,
            provider_name="Netflix", title="Fight Club", year=1999,
            arr_id=42, library="movies", size_bytes=5_000_000_000, path=path,
        )

    def test_flag_stale_item(self, tmp_db):
        self._insert_item(tmp_db)
        count = flag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        assert count == 1
        item = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item["stale_flagged_at"] is not None

    def test_flag_stale_item_no_overwrite(self, tmp_db):
        """Flagging again should NOT overwrite the original timestamp."""
        self._insert_item(tmp_db)
        flag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        item1 = get_streaming_item(tmp_db, 550, "movie", 8)
        ts1 = item1["stale_flagged_at"]
        import time; time.sleep(0.05)
        flag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        item2 = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item2["stale_flagged_at"] == ts1

    def test_unflag_stale_item(self, tmp_db):
        self._insert_item(tmp_db)
        flag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        unflag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        item = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item["stale_flagged_at"] is None

    def test_get_stale_flagged_items(self, tmp_db):
        self._insert_item(tmp_db, tmdb_id=550, path="/media/movies/Fight Club (1999)")
        self._insert_item(tmp_db, tmdb_id=862, path="/media/movies/Toy Story (1995)")
        flag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        items = get_stale_flagged_items(tmp_db)
        assert len(items) == 1
        assert items[0]["tmdb_id"] == 550

    def test_get_stale_flagged_items_excludes_deleted(self, tmp_db):
        """Deleted items should not appear in stale flagged list."""
        self._insert_item(tmp_db)
        flag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        mark_deleted(tmp_db, tmdb_id=550, media_type="movie")
        items = get_stale_flagged_items(tmp_db)
        assert len(items) == 0

    def test_get_stale_candidate_paths(self, tmp_db):
        self._insert_item(tmp_db, tmdb_id=550, path="/media/movies/Fight Club (1999)")
        self._insert_item(tmp_db, tmdb_id=862, path="/media/movies/Toy Story (1995)")
        flag_stale_item(tmp_db, tmdb_id=550, media_type="movie")
        paths = get_stale_candidate_paths(tmp_db)
        assert "/media/movies/Fight Club (1999)" in paths
        assert "/media/movies/Toy Story (1995)" not in paths


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

    def test_insert_with_season_fields(self, tmp_db):
        upsert_streaming_item(
            tmp_db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
            season_count=5, streaming_seasons="[1, 2, 3]",
        )
        item = get_streaming_item(tmp_db, 1396, "tv", 8)
        assert item["season_count"] == 5
        assert item["streaming_seasons"] == "[1, 2, 3]"

    def test_update_preserves_season_fields_when_none(self, tmp_db):
        upsert_streaming_item(
            tmp_db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
            season_count=5, streaming_seasons="[1, 2, 3]",
        )
        # Update without season fields — should preserve existing
        upsert_streaming_item(tmp_db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008)
        item = get_streaming_item(tmp_db, 1396, "tv", 8)
        assert item["season_count"] == 5
        assert item["streaming_seasons"] == "[1, 2, 3]"


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


class TestTouchKeepLocal:
    def test_prevents_left_streaming(self, tmp_db):
        """Keep-local items should not be flagged as left-streaming."""
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club",
                              arr_id=42)
        # Simulate old last_seen
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE streaming_status SET last_seen='2026-01-01T00:00:00Z'")
        conn.commit()
        conn.close()

        # Touch as keep-local
        touched = touch_keep_local_items(tmp_db, [(42, "movie")], "2026-02-01T00:00:00Z")
        assert touched == 1

        # Should NOT be flagged as left
        left = mark_left_streaming(tmp_db, "2026-02-01T00:00:00Z")
        assert len(left) == 0

    def test_clears_existing_left_at(self, tmp_db):
        """Touch should clear left_at for items already flagged."""
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club",
                              arr_id=42)
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute("UPDATE streaming_status SET last_seen='2026-01-01T00:00:00Z'")
        conn.commit()
        conn.close()
        mark_left_streaming(tmp_db, "2026-02-01T00:00:00Z")

        # Item is now "left streaming"
        assert len(get_left_streaming(tmp_db)) == 1

        # Touch clears left_at
        touch_keep_local_items(tmp_db, [(42, "movie")], "2026-03-01T00:00:00Z")
        assert len(get_left_streaming(tmp_db)) == 0

        # Should now be active again
        item = get_streaming_item(tmp_db, 550, "movie", 8)
        assert item["left_at"] is None
        assert item["last_seen"] == "2026-03-01T00:00:00Z"

    def test_no_match_returns_zero(self, tmp_db):
        """Touch with non-existent arr_id returns 0."""
        touched = touch_keep_local_items(tmp_db, [(999, "movie")], "2026-02-01T00:00:00Z")
        assert touched == 0

    def test_empty_list(self, tmp_db):
        """Empty list is a no-op."""
        touched = touch_keep_local_items(tmp_db, [], "2026-02-01T00:00:00Z")
        assert touched == 0

    def test_skips_deleted_items(self, tmp_db):
        """Touch should not update items that are already deleted."""
        upsert_streaming_item(tmp_db, 550, "movie", 8, "Netflix", "Fight Club",
                              arr_id=42)
        mark_deleted(tmp_db, 550, "movie", 8)

        touched = touch_keep_local_items(tmp_db, [(42, "movie")], "2026-03-01T00:00:00Z")
        assert touched == 0


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


def _seed_items(db):
    """Helper: insert several items for filter tests."""
    upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                          arr_id=1, library="movies", size_bytes=5_000_000_000)
    upsert_streaming_item(db, 862, "movie", 337, "Disney Plus", "Toy Story", 1995,
                          arr_id=2, library="moviesanimated", size_bytes=2_000_000_000)
    upsert_streaming_item(db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
                          arr_id=10, library="tv", size_bytes=80_000_000_000)
    upsert_streaming_item(db, 550, "movie", 337, "Disney Plus", "Fight Club", 1999,
                          arr_id=1, library="movies", size_bytes=5_000_000_000)


class TestFilteredMatches:
    def test_no_filter(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(tmp_db)
        assert len(rows) == 4

    def test_filter_by_provider(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(tmp_db, provider="Netflix")
        assert len(rows) == 2
        assert all(r["provider_name"] == "Netflix" for r in rows)

    def test_filter_by_provider_case_insensitive(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(tmp_db, provider="netflix")
        assert len(rows) == 2

    def test_filter_by_library(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(tmp_db, library="movies")
        assert len(rows) == 2
        assert all(r["library"] == "movies" for r in rows)

    def test_filter_by_min_size(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(tmp_db, min_size=4_000_000_000)
        assert len(rows) == 3  # Fight Club (2 providers) + Breaking Bad

    def test_sort_by_size(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(tmp_db, sort_by="size")
        sizes = [r.get("size_bytes", 0) or 0 for r in rows]
        assert sizes == sorted(sizes, reverse=True)

    def test_sort_by_provider(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(tmp_db, sort_by="provider")
        providers = [r["provider_name"] for r in rows]
        assert providers[0] == "Disney Plus"

    def test_combined_filters(self, tmp_db):
        _seed_items(tmp_db)
        rows = get_active_matches_filtered(
            tmp_db, provider="Netflix", library="movies"
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "Fight Club"

    def test_excludes_deleted(self, tmp_db):
        _seed_items(tmp_db)
        mark_deleted(tmp_db, 550, "movie", 8)
        rows = get_active_matches_filtered(tmp_db, provider="Netflix")
        assert len(rows) == 1

    def test_since_days(self, tmp_db):
        _seed_items(tmp_db)
        # All items were just inserted, so since_days=1 should include all
        rows = get_active_matches_filtered(tmp_db, since_days=1)
        assert len(rows) == 4
        # Set one item's first_seen to 30 days ago
        import sqlite3
        conn = sqlite3.connect(tmp_db)
        conn.execute("""
            UPDATE streaming_status SET first_seen=datetime('now', '-30 days')
            WHERE tmdb_id=862 AND media_type='movie' AND provider_id=337
        """)
        conn.commit()
        conn.close()
        rows = get_active_matches_filtered(tmp_db, since_days=7)
        assert len(rows) == 3


class TestGetScanHistory:
    def test_get_scan_history(self, tmp_db):
        record_scan(tmp_db, "CL", 100, 50, 10, 2, 1, 5.0)
        record_scan(tmp_db, "CL", 200, 60, 20, 3, 0, 8.0)
        history = get_scan_history(tmp_db, limit=5)
        assert len(history) == 2
        # Most recent first
        assert history[0]["movies_checked"] == 200
        assert history[1]["movies_checked"] == 100

    def test_get_scan_history_empty(self, tmp_db):
        history = get_scan_history(tmp_db)
        assert history == []


class TestSummaryStats:
    def test_summary_with_data(self, tmp_db):
        _seed_items(tmp_db)
        record_scan(tmp_db, "CL", 671, 149, 50, 5, 2, 15.3)
        stats = get_summary_stats(tmp_db)
        assert stats["total_active"] == 4
        assert stats["total_size_bytes"] == 92_000_000_000
        # by_provider: Netflix=2, Disney Plus=2
        providers = {p["provider_name"]: p["count"] for p in stats["by_provider"]}
        assert providers["Netflix"] == 2
        assert providers["Disney Plus"] == 2
        # by_library: deduplicated (Fight Club on 2 providers counts once in movies)
        libs = {l["library"]: l["count"] for l in stats["by_library"]}
        assert libs["movies"] == 1  # Fight Club deduped
        assert libs["tv"] == 1
        assert libs["moviesanimated"] == 1
        # last_scan
        assert stats["last_scan"] is not None
        assert stats["last_scan"]["movies_checked"] == 671

    def test_summary_empty(self, tmp_db):
        stats = get_summary_stats(tmp_db)
        assert stats["total_active"] == 0
        assert stats["total_size_bytes"] == 0
        assert stats["by_provider"] == []
        assert stats["by_library"] == []
        assert stats["last_scan"] is None


class TestUpdateStreamingSeasons:
    def test_updates_seasons(self, tmp_db):
        upsert_streaming_item(tmp_db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008)
        update_streaming_seasons(tmp_db, 1396, 8, "[1, 2, 3]", season_count=5)
        item = get_streaming_item(tmp_db, 1396, "tv", 8)
        assert item["streaming_seasons"] == "[1, 2, 3]"
        assert item["season_count"] == 5

    def test_updates_without_season_count(self, tmp_db):
        upsert_streaming_item(tmp_db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
                              season_count=5)
        update_streaming_seasons(tmp_db, 1396, 8, "[1, 2]")
        item = get_streaming_item(tmp_db, 1396, "tv", 8)
        assert item["streaming_seasons"] == "[1, 2]"
        assert item["season_count"] == 5  # unchanged

    def test_skips_deleted_items(self, tmp_db):
        upsert_streaming_item(tmp_db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008)
        mark_deleted(tmp_db, 1396, "tv", 8)
        update_streaming_seasons(tmp_db, 1396, 8, "[1, 2, 3]", season_count=5)
        item = get_streaming_item(tmp_db, 1396, "tv", 8)
        assert item["streaming_seasons"] is None


class TestExclusions:
    def test_add_exclusion(self, tmp_db):
        add_exclusion(tmp_db, 550, "movie", title="Fight Club", reason="TMDB false positive")
        exclusions = get_exclusions(tmp_db)
        assert (550, "movie") in exclusions

    def test_add_exclusion_idempotent(self, tmp_db):
        add_exclusion(tmp_db, 550, "movie", title="Fight Club", reason="reason1")
        add_exclusion(tmp_db, 550, "movie", title="Fight Club", reason="reason2")
        exclusions = list_exclusions(tmp_db)
        assert len(exclusions) == 1
        assert exclusions[0]["reason"] == "reason2"

    def test_remove_exclusion(self, tmp_db):
        add_exclusion(tmp_db, 550, "movie", title="Fight Club")
        count = remove_exclusion(tmp_db, 550, "movie")
        assert count == 1
        assert (550, "movie") not in get_exclusions(tmp_db)

    def test_remove_nonexistent(self, tmp_db):
        count = remove_exclusion(tmp_db, 999, "movie")
        assert count == 0

    def test_get_exclusions_empty(self, tmp_db):
        assert get_exclusions(tmp_db) == set()

    def test_get_exclusions_multiple(self, tmp_db):
        add_exclusion(tmp_db, 550, "movie", title="Fight Club")
        add_exclusion(tmp_db, 1396, "tv", title="Breaking Bad")
        exclusions = get_exclusions(tmp_db)
        assert len(exclusions) == 2
        assert (550, "movie") in exclusions
        assert (1396, "tv") in exclusions

    def test_list_exclusions(self, tmp_db):
        add_exclusion(tmp_db, 550, "movie", title="Fight Club", reason="false positive")
        items = list_exclusions(tmp_db)
        assert len(items) == 1
        assert items[0]["tmdb_id"] == 550
        assert items[0]["title"] == "Fight Club"
        assert items[0]["reason"] == "false positive"
        assert items[0]["added_at"] is not None

    def test_list_exclusions_empty(self, tmp_db):
        assert list_exclusions(tmp_db) == []

    def test_same_tmdb_different_media_type(self, tmp_db):
        add_exclusion(tmp_db, 550, "movie")
        add_exclusion(tmp_db, 550, "tv")
        assert len(get_exclusions(tmp_db)) == 2
