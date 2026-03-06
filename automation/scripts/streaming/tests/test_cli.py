"""Tests for the CLI entry point."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from streaming.db import init_db, upsert_streaming_item
from streaming.streaming_checker import cli


@pytest.fixture
def runner():
    return CliRunner()


MOCK_MOVIES = [
    {
        "tmdb_id": 550, "arr_id": 1, "title": "Fight Club", "year": 1999,
        "path": "/media/movies/Fight Club (1999)", "size_bytes": 5_000_000_000,
        "tags": [], "has_file": True, "library": "movies", "media_type": "movie",
    },
    {
        "tmdb_id": 862, "arr_id": 2, "title": "Toy Story", "year": 1995,
        "path": "/media/moviesanimated/Toy Story (1995)", "size_bytes": 2_000_000_000,
        "tags": [], "has_file": True, "library": "moviesanimated", "media_type": "movie",
    },
]

MOCK_SERIES = [
    {
        "tmdb_id": 1396, "arr_id": 10, "title": "Breaking Bad", "year": 2008,
        "path": "/media/tv/Breaking Bad", "size_bytes": 80_000_000_000,
        "tags": [], "library": "tv", "media_type": "tv",
        "season_count": 5, "season_numbers": [1, 2, 3, 4, 5],
    },
]


def _make_db(tmp_path):
    """Create and init a temp DB, return path string."""
    db = str(tmp_path / "test.db")
    init_db(db)
    return db


def _seed_fight_club(db, **overrides):
    """Insert Fight Club into DB. Returns db path for chaining."""
    kw = dict(tmdb_id=550, media_type="movie", provider_id=8,
              provider_name="Netflix", title="Fight Club", year=1999,
              arr_id=1, library="movies", size_bytes=5_000_000_000,
              path="/media/movies/Fight Club (1999)")
    kw.update(overrides)
    upsert_streaming_item(db, **kw)
    return db


def _seed_toy_story(db, **overrides):
    """Insert Toy Story into DB. Returns db path for chaining."""
    kw = dict(tmdb_id=862, media_type="movie", provider_id=337,
              provider_name="Disney Plus", title="Toy Story", year=1995,
              arr_id=2, library="moviesanimated", size_bytes=2_000_000_000,
              path="/media/moviesanimated/Toy Story (1995)")
    kw.update(overrides)
    upsert_streaming_item(db, **kw)
    return db


def _seed_both_movies(db, **toy_story_overrides):
    """Insert Fight Club + Toy Story into DB."""
    _seed_fight_club(db)
    _seed_toy_story(db, **toy_story_overrides)
    return db


class TestScan:
    @patch("streaming.streaming_checker.notify_scan_results")
    @patch("streaming.streaming_checker.batch_check")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_scan_dry_run(self, mock_movies, mock_series, mock_tag, mock_batch,
                          mock_notify, runner, env_config, tmp_path):
        """Dry run should find matches but not modify tags."""
        db = str(tmp_path / "test.db")

        # TMDB returns Netflix match for Fight Club
        mock_batch.return_value = {
            (550, "movie"): [{"provider_id": 8, "provider_name": "Netflix"}],
        }

        result = runner.invoke(cli, ["scan", "--dry-run", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()
        assert "Newly streaming: 1" in result.output

    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.notify_scan_results")
    @patch("streaming.streaming_checker.batch_check")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_scan_finds_matches(self, mock_movies, mock_series, mock_tag,
                                mock_batch, mock_notify, mock_add_tag,
                                runner, env_config, tmp_path):
        """Scan should find Netflix matches and tag items."""
        db = str(tmp_path / "test.db")

        mock_batch.return_value = {
            (550, "movie"): [{"provider_id": 8, "provider_name": "Netflix"}],
            (1396, "tv"): [{"provider_id": 8, "provider_name": "Netflix"}],
        }

        result = runner.invoke(cli, ["scan", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Newly streaming: 2" in result.output
        # Should have tagged items
        assert mock_add_tag.call_count == 2

    @patch("streaming.streaming_checker.batch_check")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.fetch_series", return_value=[])
    @patch("streaming.streaming_checker.fetch_movies", return_value=[])
    def test_scan_empty_library(self, mock_movies, mock_series, mock_tag,
                                mock_batch, runner, env_config, tmp_path):
        """Scan with empty library should complete without errors."""
        db = str(tmp_path / "test.db")
        mock_batch.return_value = {}

        result = runner.invoke(cli, ["scan", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Movies checked:  0" in result.output


class TestReport:
    def test_report_empty(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        result = runner.invoke(cli, ["report", "--db-path", db])
        assert result.exit_code == 0
        assert "No streaming matches" in result.output

    def test_report_with_data(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        result = runner.invoke(cli, ["report", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Netflix" in result.output

    def test_report_json(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        result = runner.invoke(cli, ["report", "--json", "--db-path", db])
        assert result.exit_code == 0
        data = __import__("json").loads(result.output)
        assert len(data["active"]) == 1


class TestConfirmDelete:
    @patch("streaming.streaming_checker.ensure_tag", return_value=4)
    @patch("streaming.streaming_checker.get_item")
    @patch("streaming.streaming_checker.delete_item")
    def test_confirm_delete_dry_run(self, mock_delete, mock_get_item, mock_tag,
                                     runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_fight_club(db)

        result = runner.invoke(cli, ["confirm-delete", "--yes", "--dry-run", "--db-path", db])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()
        mock_delete.assert_not_called()

    def test_confirm_delete_requires_yes(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        result = runner.invoke(cli, ["confirm-delete", "--db-path", db])
        assert result.exit_code != 0

    @patch("streaming.streaming_checker.ensure_tag", return_value=4)
    @patch("streaming.streaming_checker.get_item")
    @patch("streaming.streaming_checker.delete_item")
    def test_confirm_delete_tmdb_ids_filter(self, mock_delete, mock_get_item, mock_tag,
                                             runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_both_movies(db)
        result = runner.invoke(cli, [
            "confirm-delete", "--yes", "--dry-run", "--tmdb-ids", "550", "--db-path", db
        ])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Toy Story" not in result.output

    @patch("streaming.streaming_checker.ensure_tag", return_value=4)
    @patch("streaming.streaming_checker.get_item")
    @patch("streaming.streaming_checker.delete_item")
    def test_confirm_delete_library_filter(self, mock_delete, mock_get_item, mock_tag,
                                            runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_both_movies(db)
        result = runner.invoke(cli, [
            "confirm-delete", "--yes", "--dry-run", "--library", "moviesanimated", "--db-path", db
        ])
        assert result.exit_code == 0
        assert "Toy Story" in result.output
        assert "Fight Club" not in result.output


class TestReportFilters:
    def test_report_provider_filter(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_both_movies(db)
        result = runner.invoke(cli, ["report", "--provider", "Netflix", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Toy Story" not in result.output

    def test_report_library_filter(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_both_movies(db)
        result = runner.invoke(cli, ["report", "--library", "movies", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Toy Story" not in result.output

    def test_report_min_size_filter(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_both_movies(db)
        result = runner.invoke(cli, ["report", "--min-size", "4", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Toy Story" not in result.output

    def test_report_sort_by_size(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_both_movies(db, provider_id=8, provider_name="Netflix")
        result = runner.invoke(cli, ["report", "--sort-by", "size", "--db-path", db])
        assert result.exit_code == 0
        # Fight Club (5 GB) should appear before Toy Story (2 GB) in the output
        fc_pos = result.output.index("Fight Club")
        ts_pos = result.output.index("Toy Story")
        assert fc_pos < ts_pos


class TestSummary:
    def test_summary_empty(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        result = runner.invoke(cli, ["summary", "--db-path", db])
        assert result.exit_code == 0
        assert "No active streaming matches" in result.output

    def test_summary_with_data(self, runner, env_config, tmp_path):
        from streaming.db import record_scan
        db = _make_db(tmp_path)
        _seed_both_movies(db)
        record_scan(db, "CL", 100, 50, 2, 2, 0, 10.5)
        result = runner.invoke(cli, ["summary", "--db-path", db])
        assert result.exit_code == 0
        assert "Active matches: 2" in result.output
        assert "Netflix" in result.output
        assert "Disney Plus" in result.output

    def test_summary_json(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        result = runner.invoke(cli, ["summary", "--json", "--db-path", db])
        assert result.exit_code == 0
        import json
        data = json.loads(result.output)
        assert data["stats"]["total_active"] == 1


class TestCheckSeasons:
    def test_missing_rapidapi_key(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")  # no init needed — fails before DB access
        result = runner.invoke(cli, ["check-seasons", "--db-path", db])
        assert result.exit_code != 0
        assert "RAPIDAPI_KEY" in result.output

    def test_no_tv_matches(self, runner, env_config, monkeypatch, tmp_path):
        monkeypatch.setenv("RAPIDAPI_KEY", "test-key")
        db = _make_db(tmp_path)
        result = runner.invoke(cli, ["check-seasons", "--db-path", db])
        assert result.exit_code == 0
        assert "No active TV matches" in result.output

    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=4)
    @patch("streaming.streaming_checker.get_season_availability")
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    def test_check_seasons_tags_keep_local(self, mock_fetch, mock_avail, mock_tag,
                                            mock_add_tag, runner, env_config,
                                            monkeypatch, tmp_path):
        monkeypatch.setenv("RAPIDAPI_KEY", "test-key")
        db = _make_db(tmp_path)
        upsert_streaming_item(db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
                              arr_id=10, library="tv", size_bytes=80_000_000_000)

        # Netflix only has seasons 1-3 but we own 1-5
        mock_avail.return_value = {
            1: ["netflix"], 2: ["netflix"], 3: ["netflix"],
            4: [], 5: [],
        }

        result = runner.invoke(cli, ["check-seasons", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Keep-local tags: 1" in result.output
        mock_add_tag.assert_called_once()

    @patch("streaming.streaming_checker.get_season_availability")
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    def test_check_seasons_dry_run(self, mock_fetch, mock_avail,
                                    runner, env_config, monkeypatch, tmp_path):
        monkeypatch.setenv("RAPIDAPI_KEY", "test-key")
        db = _make_db(tmp_path)
        upsert_streaming_item(db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
                              arr_id=10, library="tv", size_bytes=80_000_000_000)

        mock_avail.return_value = {1: ["netflix"], 2: ["netflix"], 3: []}

        result = runner.invoke(cli, ["check-seasons", "--dry-run", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()
        assert "Would tag keep-local" in result.output


class TestReportEnhanced:
    def test_report_with_season_data(self, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        upsert_streaming_item(db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
                              library="tv", size_bytes=80_000_000_000,
                              season_count=5, streaming_seasons="[1, 2, 3]")
        result = runner.invoke(cli, ["report", "--db-path", db])
        assert result.exit_code == 0
        assert "3/5 seasons" in result.output

    @patch("streaming.streaming_checker.get_last_played_map")
    def test_report_no_play_days(self, mock_play_map, runner, env_config, tmp_path):
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        _seed_toy_story(db, provider_id=8, provider_name="Netflix",
                        library="movies", path="/media/movies/Toy Story (1995)")

        # Fight Club played recently, Toy Story never played
        mock_play_map.return_value = {
            "/media/movies/Fight Club (1999)": "2026-02-28T00:00:00Z",
        }

        result = runner.invoke(cli, ["report", "--no-play-days", "90", "--db-path", db])
        assert result.exit_code == 0, result.output
        # Toy Story should appear (never played), Fight Club should not (played recently)
        assert "Toy Story" in result.output
        assert "never played" in result.output


class TestStaleFlag:
    @patch("streaming.streaming_checker.notify_stale_flag")
    @patch("streaming.streaming_checker.get_last_played_map", return_value={})
    @patch("streaming.streaming_checker._get_keep_local_set", return_value={(999, "movie")})
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_stale_flag_never_played_on_streaming(
        self, mock_movies, mock_series, mock_kl, mock_play, mock_notify,
        runner, env_config, tmp_path,
    ):
        """Items on streaming + never played → flagged."""
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        result = runner.invoke(cli, ["stale-flag", "--no-play-days", "90", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Flagged: 1" in result.output

    @patch("streaming.streaming_checker.notify_stale_flag")
    @patch("streaming.streaming_checker.get_last_played_map")
    @patch("streaming.streaming_checker._get_keep_local_set", return_value={(999, "movie")})
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_stale_flag_recently_played_not_flagged(
        self, mock_movies, mock_series, mock_kl, mock_play, mock_notify,
        runner, env_config, tmp_path,
    ):
        """Items played recently → NOT flagged."""
        from datetime import datetime, timezone
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        mock_play.return_value = {
            "/media/movies/Fight Club (1999)": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = runner.invoke(cli, ["stale-flag", "--no-play-days", "90", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Flagged: 0" in result.output

    @patch("streaming.streaming_checker.notify_stale_flag")
    @patch("streaming.streaming_checker.get_last_played_map", return_value={})
    @patch("streaming.streaming_checker._get_keep_local_set", return_value={(999, "movie")})
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_stale_flag_not_on_streaming_not_flagged(
        self, mock_movies, mock_series, mock_kl, mock_play, mock_notify,
        runner, env_config, tmp_path,
    ):
        """Items NOT on streaming → NOT flagged even if stale."""
        db = _make_db(tmp_path)
        result = runner.invoke(cli, ["stale-flag", "--no-play-days", "90", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Flagged: 0" in result.output

    @patch("streaming.streaming_checker.notify_stale_flag")
    @patch("streaming.streaming_checker.get_last_played_map")
    @patch("streaming.streaming_checker._get_keep_local_set", return_value={(999, "movie")})
    @patch("streaming.streaming_checker.fetch_series", return_value=MOCK_SERIES)
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_stale_flag_unflag_watched_item(
        self, mock_movies, mock_series, mock_kl, mock_play, mock_notify,
        runner, env_config, tmp_path,
    ):
        """Previously flagged item that was watched → unflagged."""
        from datetime import datetime, timezone
        from streaming.db import flag_stale_item
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        flag_stale_item(db, tmdb_id=550, media_type="movie")
        mock_play.return_value = {
            "/media/movies/Fight Club (1999)": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        result = runner.invoke(cli, ["stale-flag", "--no-play-days", "90", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Unflagged: 1" in result.output


class TestStaleDelete:
    @patch("streaming.streaming_checker.refresh_library")
    @patch("streaming.streaming_checker.notify_deletion")
    @patch("streaming.streaming_checker.is_playing", return_value=False)
    @patch("streaming.streaming_checker.delete_item")
    @patch("streaming.streaming_checker.get_item")
    @patch("streaming.streaming_checker.get_last_played_map", return_value={})
    @patch("streaming.streaming_checker._get_keep_local_set", return_value={(999, "movie")})
    @patch("streaming.streaming_checker.fetch_series", return_value=[])
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_stale_delete_after_grace_period(
        self, mock_movies, mock_series, mock_kl, mock_play, mock_get_item,
        mock_delete, mock_playing, mock_notify, mock_refresh,
        runner, env_config, tmp_path,
    ):
        """Items flagged >15 days ago + still stale → deleted."""
        from datetime import datetime, timezone, timedelta
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        import sqlite3
        old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        conn = sqlite3.connect(db)
        conn.execute("UPDATE streaming_status SET stale_flagged_at = ?", (old_ts,))
        conn.commit()
        conn.close()
        mock_get_item.return_value = {"id": 1, "tags": [], "title": "Fight Club"}
        result = runner.invoke(cli, ["stale-delete", "--grace-days", "15", "--yes", "--db-path", db])
        assert result.exit_code == 0, result.output
        mock_delete.assert_called_once()

    @patch("streaming.streaming_checker.notify_deletion")
    @patch("streaming.streaming_checker.get_last_played_map", return_value={})
    @patch("streaming.streaming_checker._get_keep_local_set", return_value={(999, "movie")})
    @patch("streaming.streaming_checker.fetch_series", return_value=[])
    @patch("streaming.streaming_checker.fetch_movies", return_value=MOCK_MOVIES)
    def test_stale_delete_within_grace_period_skipped(
        self, mock_movies, mock_series, mock_kl, mock_play, mock_notify,
        runner, env_config, tmp_path,
    ):
        """Items flagged <15 days ago → NOT deleted."""
        from streaming.db import flag_stale_item
        db = _make_db(tmp_path)
        _seed_fight_club(db)
        flag_stale_item(db, tmdb_id=550, media_type="movie")
        result = runner.invoke(cli, ["stale-delete", "--grace-days", "15", "--yes", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "Deleted: 0" in result.output


class TestCheckImport:
    """Tests for the check-import subcommand."""

    MOCK_RADARR_ITEM = {
        "id": 1, "tmdbId": 550, "title": "Fight Club", "year": 1999,
        "path": "/media/movies/Fight Club (1999)",
        "movieFile": {"size": 5_000_000_000},
        "tags": [],
    }

    MOCK_SONARR_ITEM = {
        "id": 10, "tvdbId": 81189, "tmdbId": 1396, "title": "Breaking Bad", "year": 2008,
        "path": "/media/tv/Breaking Bad",
        "statistics": {"sizeOnDisk": 80_000_000_000},
        "tags": [],
    }

    @patch("streaming.streaming_checker.notify_import_streaming")
    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.get_tag_id", return_value=None)
    @patch("streaming.streaming_checker.get_streaming_providers")
    @patch("streaming.streaming_checker.get_item")
    def test_movie_on_streaming_tags_and_upserts(
        self, mock_get_item, mock_motn, mock_get_tag, mock_ensure, mock_add_tag,
        mock_notify, runner, env_config, tmp_path, monkeypatch
    ):
        """Movie found on streaming gets tagged + upserted into DB."""
        monkeypatch.setenv("RAPIDAPI_KEY", "test-rapid-key")
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_motn.return_value = [
            {"service_id": "netflix", "service_name": "Netflix"},
        ]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        assert "Netflix" in result.output
        mock_add_tag.assert_called_once()
        mock_notify.assert_called_once()

    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.get_tag_id", return_value=None)
    @patch("streaming.streaming_checker.get_streaming_providers", return_value=[])
    @patch("streaming.streaming_checker.check_streaming")
    @patch("streaming.streaming_checker.get_item")
    def test_movie_not_on_streaming_no_tag(
        self, mock_get_item, mock_tmdb, mock_motn, mock_get_tag, mock_add_tag,
        runner, env_config, tmp_path
    ):
        """Movie not on any streaming — no tag added."""
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_tmdb.return_value = []

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_add_tag.assert_not_called()

    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.get_tag_id", return_value=None)
    @patch("streaming.streaming_checker.get_streaming_providers", return_value=[])
    @patch("streaming.streaming_checker.check_streaming")
    @patch("streaming.streaming_checker.get_item")
    def test_tmdb_fallback_when_motn_empty(
        self, mock_get_item, mock_tmdb, mock_motn, mock_get_tag, mock_ensure, mock_add_tag,
        runner, env_config, tmp_path
    ):
        """Falls back to TMDB when MoTN returns empty."""
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_tmdb.return_value = [{"provider_id": 8, "provider_name": "Netflix"}]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_tmdb.assert_called_once()
        mock_add_tag.assert_called_once()

    @patch("streaming.streaming_checker.get_streaming_providers")
    @patch("streaming.streaming_checker.get_item")
    def test_already_tagged_skips_api(
        self, mock_get_item, mock_motn, runner, env_config, tmp_path
    ):
        """Item already tagged streaming-available skips all API calls."""
        db = _make_db(tmp_path)
        item = dict(self.MOCK_RADARR_ITEM)
        item["tags"] = [1]  # tag_id 1 = streaming-available
        mock_get_item.return_value = item

        with patch("streaming.streaming_checker.get_tag_id", return_value=1):
            result = runner.invoke(cli, [
                "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
                "--media-type", "movie", "--arr-id", "1", "--db-path", db,
            ])
        assert result.exit_code == 0, result.output
        mock_motn.assert_not_called()
        assert "already tagged" in result.output.lower()

    @patch("streaming.streaming_checker.get_item", return_value=None)
    def test_item_not_found_exits_gracefully(
        self, mock_get_item, runner, env_config, tmp_path
    ):
        """arr_id not found in Sonarr/Radarr — exits 0 with warning."""
        db = _make_db(tmp_path)
        result = runner.invoke(cli, [
            "check-import", "--file", "/test/file.mkv",
            "--media-type", "movie", "--arr-id", "999", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        assert "not found" in result.output.lower()

    @patch("streaming.streaming_checker.notify_import_streaming")
    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.get_tag_id", return_value=None)
    @patch("streaming.streaming_checker.get_streaming_providers")
    @patch("streaming.streaming_checker.get_item")
    def test_series_on_streaming(
        self, mock_get_item, mock_motn, mock_get_tag, mock_ensure, mock_add_tag,
        mock_notify, runner, env_config, tmp_path, monkeypatch
    ):
        """TV series found on streaming gets tagged."""
        monkeypatch.setenv("RAPIDAPI_KEY", "test-rapid-key")
        db = _make_db(tmp_path)
        mock_get_item.return_value = self.MOCK_SONARR_ITEM
        mock_motn.return_value = [
            {"service_id": "disney", "service_name": "Disney+"},
        ]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/tv/Breaking Bad/Season 1/ep.mkv",
            "--media-type", "series", "--arr-id", "10", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_add_tag.assert_called_once()

    @patch("streaming.streaming_checker.notify_import_streaming")
    @patch("streaming.streaming_checker.add_tag_to_item")
    @patch("streaming.streaming_checker.ensure_tag", return_value=1)
    @patch("streaming.streaming_checker.get_tag_id", return_value=None)
    @patch("streaming.streaming_checker.get_streaming_providers", return_value=[])
    @patch("streaming.streaming_checker.check_streaming")
    @patch("streaming.streaming_checker.get_item")
    def test_no_rapidapi_key_skips_motn(
        self, mock_get_item, mock_tmdb, mock_motn, mock_get_tag, mock_ensure, mock_add_tag,
        mock_notify, runner, env_config, tmp_path, monkeypatch
    ):
        """Without RAPIDAPI_KEY, skips MoTN and goes straight to TMDB."""
        db = _make_db(tmp_path)
        monkeypatch.delenv("RAPIDAPI_KEY", raising=False)
        mock_get_item.return_value = self.MOCK_RADARR_ITEM
        mock_tmdb.return_value = [{"provider_id": 8, "provider_name": "Netflix"}]

        result = runner.invoke(cli, [
            "check-import", "--file", "/media/movies/Fight Club (1999)/file.mkv",
            "--media-type", "movie", "--arr-id", "1", "--db-path", db,
        ])
        assert result.exit_code == 0, result.output
        mock_motn.assert_not_called()
        mock_tmdb.assert_called_once()
