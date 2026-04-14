"""Tests for stale-cleanup grace period (--grace-days) feature.

Grace period: items added within N days are skipped regardless of play history.
This prevents auto-deletion of recently added content that hasn't had time to be watched.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from streaming.db import init_db
from streaming.streaming_checker import cli


def _iso(dt):
    """Format datetime as ISO8601 with Z suffix (Radarr/Sonarr format)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(n):
    """Return datetime N days ago from now (UTC)."""
    return datetime.now(timezone.utc) - timedelta(days=n)


def _make_movie(arr_id, title, size_bytes, added_days_ago, path=None):
    return {
        "tmdb_id": arr_id * 100,
        "arr_id": arr_id,
        "title": title,
        "year": 2020,
        "path": path or f"/media/movies/{title} (2020)",
        "size_bytes": size_bytes,
        "tags": [],
        "has_file": True,
        "library": "movies",
        "media_type": "movie",
        "added": _iso(_days_ago(added_days_ago)),
    }


def _make_series(arr_id, title, size_bytes, added_days_ago, path=None):
    return {
        "tmdb_id": arr_id * 100,
        "arr_id": arr_id,
        "title": title,
        "year": 2020,
        "path": path or f"/media/tv/{title}",
        "size_bytes": size_bytes,
        "tags": [],
        "library": "tv",
        "media_type": "tv",
        "season_count": 1,
        "season_numbers": [1],
        "added": _iso(_days_ago(added_days_ago)),
    }


# Common patch targets
_PATCH_FETCH_MOVIES = "streaming.streaming_checker.fetch_movies"
_PATCH_FETCH_SERIES = "streaming.streaming_checker.fetch_series"
_PATCH_PLAY_MAP = "streaming.streaming_checker.get_last_played_map"
_PATCH_KEEP_LOCAL = "streaming.streaming_checker._get_keep_local_set"
_PATCH_KEEP_LOCAL_TAG_IDS = "streaming.streaming_checker._get_keep_local_tag_ids"
_PATCH_ACTIVE_MATCHES = "streaming.streaming_checker.get_active_matches_filtered"
_PATCH_GET_ITEM = "streaming.streaming_checker.get_item"
_PATCH_IS_PLAYING = "streaming.streaming_checker.is_playing"
_PATCH_DELETE_ITEM = "streaming.streaming_checker.delete_item"
_PATCH_ENSURE_TAG = "streaming.streaming_checker.ensure_tag"
_PATCH_REFRESH = "streaming.streaming_checker.refresh_library"
_PATCH_DISCORD = "streaming.streaming_checker.notify_stale_cleanup"
_PATCH_DUAL_AUDIO = "streaming.streaming_checker._is_dual_audio_keep_local"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    return db_path


def _run_stale_cleanup(runner, db, movies, series, play_map, extra_args=None):
    """Helper: run stale-cleanup with standard mocks."""
    args = [
        "stale-cleanup",
        "--yes",
        "--no-play-days", "365",
        "--min-size-gb", "3.0",
        "--db-path", db,
    ]
    if extra_args:
        args.extend(extra_args)

    with patch(_PATCH_FETCH_MOVIES, return_value=movies), \
         patch(_PATCH_FETCH_SERIES, return_value=series), \
         patch(_PATCH_PLAY_MAP, return_value=play_map), \
         patch(_PATCH_KEEP_LOCAL_TAG_IDS, return_value=(1, 1)), \
         patch(_PATCH_KEEP_LOCAL, return_value=set()), \
         patch(_PATCH_ACTIVE_MATCHES, return_value=[]), \
         patch(_PATCH_DUAL_AUDIO, return_value=False), \
         patch(_PATCH_ENSURE_TAG, return_value=1), \
         patch(_PATCH_GET_ITEM, return_value={"tags": []}), \
         patch(_PATCH_IS_PLAYING, return_value=False), \
         patch(_PATCH_DELETE_ITEM) as mock_delete, \
         patch(_PATCH_REFRESH), \
         patch(_PATCH_DISCORD):
        result = runner.invoke(cli, args, catch_exceptions=False)
        return result, mock_delete


class TestGracePeriodSkipsRecentItems:
    """Items added within grace period are skipped even if never played."""

    def test_never_played_added_10_days_ago_skipped(self, runner, db, env_config):
        """Item added 10 days ago, never played → skipped (within 180-day grace)."""
        movie = _make_movie(1, "Stargate Atlantis", size_bytes=5_000_000_000, added_days_ago=10)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},  # never played
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_not_called()
        # When all items are in grace period, the stale list is empty and the command
        # exits early with "No items found..." before reaching the deletion summary line
        assert "No items found" in result.output or "Deleted 0" in result.output

    def test_never_played_added_179_days_ago_skipped(self, runner, db, env_config):
        """Item added 179 days ago (just inside grace period), never played → skipped."""
        movie = _make_movie(1, "Recent Show", size_bytes=5_000_000_000, added_days_ago=179)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_not_called()

    def test_never_played_added_10_days_ago_series_skipped(self, runner, db, env_config):
        """Series added 10 days ago, never played → skipped (within grace period)."""
        show = _make_series(2, "New Show", size_bytes=8_000_000_000, added_days_ago=10)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[], series=[show],
            play_map={},
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_not_called()


class TestGracePeriodExpiredItemsAreStale:
    """Items past the grace period with no recent play are stale and eligible for deletion."""

    def test_never_played_added_200_days_ago_is_stale(self, runner, db, env_config):
        """Item added 200 days ago (past 180-day grace), never played → stale, deleted."""
        movie = _make_movie(1, "Old Movie", size_bytes=5_000_000_000, added_days_ago=200)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_called_once()

    def test_never_played_added_365_days_ago_is_stale(self, runner, db, env_config):
        """Item added a year ago, never played → stale."""
        movie = _make_movie(1, "Ancient Movie", size_bytes=5_000_000_000, added_days_ago=365)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_called_once()

    def test_played_too_long_ago_added_200_days_ago_is_stale(self, runner, db, env_config):
        """Item added 200 days ago, played 400 days ago → past grace AND past play cutoff → stale."""
        movie = _make_movie(1, "Forgotten Movie", size_bytes=5_000_000_000, added_days_ago=200)
        play_map = {movie["path"]: _iso(_days_ago(400))}
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map=play_map,
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_called_once()


class TestGracePeriodDoesNotAffectRecentlyPlayed:
    """Play history still protects items from deletion regardless of added date."""

    def test_added_100_days_ago_played_50_days_ago_not_stale(self, runner, db, env_config):
        """Item added 100 days ago, played 50 days ago → NOT stale (played within 365d)."""
        movie = _make_movie(1, "Active Movie", size_bytes=5_000_000_000, added_days_ago=100)
        play_map = {movie["path"]: _iso(_days_ago(50))}
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map=play_map,
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_not_called()

    def test_added_200_days_ago_played_200_days_ago_not_stale(self, runner, db, env_config):
        """Item added 200 days ago (past grace), played 200 days ago → still within 365d play cutoff."""
        movie = _make_movie(1, "Barely Active", size_bytes=5_000_000_000, added_days_ago=200)
        play_map = {movie["path"]: _iso(_days_ago(200))}
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map=play_map,
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_not_called()


class TestGraceDaysZeroDisablesGrace:
    """--grace-days 0 disables grace period; never-played items are stale immediately."""

    def test_grace_days_zero_never_played_recent_is_stale(self, runner, db, env_config):
        """--grace-days 0: item added 10 days ago, never played → stale (grace disabled)."""
        movie = _make_movie(1, "Brand New Movie", size_bytes=5_000_000_000, added_days_ago=10)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},
            extra_args=["--grace-days", "0"],
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_called_once()

    def test_grace_days_zero_played_recently_still_safe(self, runner, db, env_config):
        """--grace-days 0: item played 50 days ago is still protected by play history."""
        movie = _make_movie(1, "Watched Movie", size_bytes=5_000_000_000, added_days_ago=10)
        play_map = {movie["path"]: _iso(_days_ago(50))}
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map=play_map,
            extra_args=["--grace-days", "0"],
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_not_called()


class TestGracePeriodCustomDays:
    """--grace-days with non-default values."""

    def test_custom_grace_30_days_item_added_20_days_ago_skipped(self, runner, db, env_config):
        """--grace-days 30: item added 20 days ago → skipped."""
        movie = _make_movie(1, "Newer Movie", size_bytes=5_000_000_000, added_days_ago=20)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},
            extra_args=["--grace-days", "30"],
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_not_called()

    def test_custom_grace_30_days_item_added_40_days_ago_stale(self, runner, db, env_config):
        """--grace-days 30: item added 40 days ago, never played → stale."""
        movie = _make_movie(1, "Older Movie", size_bytes=5_000_000_000, added_days_ago=40)
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},
            extra_args=["--grace-days", "30"],
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_called_once()


class TestGracePeriodMissingAddedField:
    """Items with missing or empty 'added' field are not protected by grace period."""

    def test_empty_added_field_never_played_is_stale(self, runner, db, env_config):
        """Item with empty 'added' field, never played → treated as stale (no grace)."""
        movie = _make_movie(1, "No Added Date", size_bytes=5_000_000_000, added_days_ago=200)
        movie["added"] = ""  # empty added — grace period cannot be computed
        result, mock_delete = _run_stale_cleanup(
            runner, db,
            movies=[movie], series=[],
            play_map={},
        )
        assert result.exit_code == 0, result.output
        mock_delete.assert_called_once()
