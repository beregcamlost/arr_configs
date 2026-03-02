"""Tests for the CLI entry point."""

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

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
        db = str(tmp_path / "test.db")
        from streaming.db import init_db
        init_db(db)
        result = runner.invoke(cli, ["report", "--db-path", db])
        assert result.exit_code == 0
        assert "No streaming matches" in result.output

    def test_report_with_data(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000)
        result = runner.invoke(cli, ["report", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Netflix" in result.output

    def test_report_json(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999)
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
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              arr_id=1, library="movies", size_bytes=5_000_000_000)

        result = runner.invoke(cli, ["confirm-delete", "--yes", "--dry-run", "--db-path", db])
        assert result.exit_code == 0
        assert "dry-run" in result.output.lower()
        mock_delete.assert_not_called()

    def test_confirm_delete_requires_yes(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db
        init_db(db)
        result = runner.invoke(cli, ["confirm-delete", "--db-path", db])
        assert result.exit_code != 0

    @patch("streaming.streaming_checker.ensure_tag", return_value=4)
    @patch("streaming.streaming_checker.get_item")
    @patch("streaming.streaming_checker.delete_item")
    def test_confirm_delete_tmdb_ids_filter(self, mock_delete, mock_get_item, mock_tag,
                                             runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              arr_id=1, library="movies", size_bytes=5_000_000_000)
        upsert_streaming_item(db, 862, "movie", 337, "Disney Plus", "Toy Story", 1995,
                              arr_id=2, library="moviesanimated", size_bytes=2_000_000_000)
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
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              arr_id=1, library="movies", size_bytes=5_000_000_000)
        upsert_streaming_item(db, 862, "movie", 337, "Disney Plus", "Toy Story", 1995,
                              arr_id=2, library="moviesanimated", size_bytes=2_000_000_000)
        result = runner.invoke(cli, [
            "confirm-delete", "--yes", "--dry-run", "--library", "moviesanimated", "--db-path", db
        ])
        assert result.exit_code == 0
        assert "Toy Story" in result.output
        assert "Fight Club" not in result.output


class TestReportFilters:
    def test_report_provider_filter(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000)
        upsert_streaming_item(db, 862, "movie", 337, "Disney Plus", "Toy Story", 1995,
                              library="moviesanimated", size_bytes=2_000_000_000)
        result = runner.invoke(cli, ["report", "--provider", "Netflix", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Toy Story" not in result.output

    def test_report_library_filter(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000)
        upsert_streaming_item(db, 862, "movie", 337, "Disney Plus", "Toy Story", 1995,
                              library="moviesanimated", size_bytes=2_000_000_000)
        result = runner.invoke(cli, ["report", "--library", "movies", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Toy Story" not in result.output

    def test_report_min_size_filter(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000)
        upsert_streaming_item(db, 862, "movie", 337, "Disney Plus", "Toy Story", 1995,
                              library="moviesanimated", size_bytes=2_000_000_000)
        result = runner.invoke(cli, ["report", "--min-size", "4", "--db-path", db])
        assert result.exit_code == 0
        assert "Fight Club" in result.output
        assert "Toy Story" not in result.output

    def test_report_sort_by_size(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000)
        upsert_streaming_item(db, 862, "movie", 8, "Netflix", "Toy Story", 1995,
                              library="moviesanimated", size_bytes=2_000_000_000)
        result = runner.invoke(cli, ["report", "--sort-by", "size", "--db-path", db])
        assert result.exit_code == 0
        # Fight Club (5 GB) should appear before Toy Story (2 GB) in the output
        fc_pos = result.output.index("Fight Club")
        ts_pos = result.output.index("Toy Story")
        assert fc_pos < ts_pos


class TestSummary:
    def test_summary_empty(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db
        init_db(db)
        result = runner.invoke(cli, ["summary", "--db-path", db])
        assert result.exit_code == 0
        assert "No active streaming matches" in result.output

    def test_summary_with_data(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item, record_scan
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000)
        upsert_streaming_item(db, 862, "movie", 337, "Disney Plus", "Toy Story", 1995,
                              library="moviesanimated", size_bytes=2_000_000_000)
        record_scan(db, "CL", 100, 50, 2, 2, 0, 10.5)
        result = runner.invoke(cli, ["summary", "--db-path", db])
        assert result.exit_code == 0
        assert "Active matches: 2" in result.output
        assert "Netflix" in result.output
        assert "Disney Plus" in result.output

    def test_summary_json(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000)
        result = runner.invoke(cli, ["summary", "--json", "--db-path", db])
        assert result.exit_code == 0
        import json
        data = json.loads(result.output)
        assert data["stats"]["total_active"] == 1


class TestCheckSeasons:
    def test_missing_rapidapi_key(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        result = runner.invoke(cli, ["check-seasons", "--db-path", db])
        assert result.exit_code != 0
        assert "RAPIDAPI_KEY" in result.output

    def test_no_tv_matches(self, runner, env_config, monkeypatch, tmp_path):
        monkeypatch.setenv("RAPIDAPI_KEY", "test-key")
        db = str(tmp_path / "test.db")
        from streaming.db import init_db
        init_db(db)
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
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
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
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
                              arr_id=10, library="tv", size_bytes=80_000_000_000)

        mock_avail.return_value = {1: ["netflix"], 2: ["netflix"], 3: []}

        result = runner.invoke(cli, ["check-seasons", "--dry-run", "--db-path", db])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()
        assert "Would tag keep-local" in result.output


class TestReportEnhanced:
    def test_report_with_season_data(self, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 1396, "tv", 8, "Netflix", "Breaking Bad", 2008,
                              library="tv", size_bytes=80_000_000_000,
                              season_count=5, streaming_seasons="[1, 2, 3]")
        result = runner.invoke(cli, ["report", "--db-path", db])
        assert result.exit_code == 0
        assert "3/5 seasons" in result.output

    @patch("streaming.streaming_checker.get_last_played_map")
    def test_report_no_play_days(self, mock_play_map, runner, env_config, tmp_path):
        db = str(tmp_path / "test.db")
        from streaming.db import init_db, upsert_streaming_item
        init_db(db)
        upsert_streaming_item(db, 550, "movie", 8, "Netflix", "Fight Club", 1999,
                              library="movies", size_bytes=5_000_000_000,
                              path="/media/movies/Fight Club (1999)")
        upsert_streaming_item(db, 862, "movie", 8, "Netflix", "Toy Story", 1995,
                              library="movies", size_bytes=2_000_000_000,
                              path="/media/movies/Toy Story (1995)")

        # Fight Club played recently, Toy Story never played
        mock_play_map.return_value = {
            "/media/movies/Fight Club (1999)": "2026-02-28T00:00:00Z",
        }

        result = runner.invoke(cli, ["report", "--no-play-days", "90", "--db-path", db])
        assert result.exit_code == 0, result.output
        # Toy Story should appear (never played), Fight Club should not (played recently)
        assert "Toy Story" in result.output
        assert "never played" in result.output
