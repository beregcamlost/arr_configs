"""Tests for trending_add CLI and helpers."""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from streaming.trending_add import (
    CACHE_TTL_DAYS,
    _is_animated,
    _load_cache,
    _save_cache,
    cli,
)


# --- _is_animated ---

def test_is_animated_anime():
    assert _is_animated(["Action", "Anime"]) is True

def test_is_animated_animation():
    assert _is_animated(["Comedy", "Animation"]) is True

def test_is_animated_case_insensitive():
    assert _is_animated(["ANIMATION"]) is True

def test_is_animated_false():
    assert _is_animated(["Action", "Drama"]) is False

def test_is_animated_empty():
    assert _is_animated([]) is False


# --- Cache ---

def test_save_and_load_cache(tmp_path):
    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        data = [{"tmdb_id": 123, "title": "Test"}]
        _save_cache("apple", "movie", data)
        loaded = _load_cache("apple", "movie")
        assert loaded == data

def test_load_cache_missing(tmp_path):
    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        assert _load_cache("apple", "movie") is None

def test_load_cache_stale(tmp_path):
    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        path = tmp_path / "apple_movie.json"
        path.write_text("[]")
        # Set mtime to 8 days ago
        old_time = os.path.getmtime(str(path)) - (CACHE_TTL_DAYS + 1) * 86400
        os.utime(str(path), (old_time, old_time))
        assert _load_cache("apple", "movie") is None


# --- search_catalog mock ---

MOCK_CATALOG = [
    {"tmdb_id": 100, "title": "Movie A", "year": 2025, "show_type": "movie", "genres": ["Action"], "imdb_id": "tt100"},
    {"tmdb_id": 200, "title": "Movie B", "year": 2025, "show_type": "movie", "genres": ["Animation"], "imdb_id": "tt200"},
    {"tmdb_id": 300, "title": "Movie C", "year": 2024, "show_type": "movie", "genres": ["Drama"], "imdb_id": "tt300"},
]

MOCK_SERIES_CATALOG = [
    {"tmdb_id": 400, "title": "Series A", "year": 2025, "show_type": "series", "genres": ["Anime"], "imdb_id": "tt400"},
    {"tmdb_id": 500, "title": "Series B", "year": 2025, "show_type": "series", "genres": ["Drama"], "imdb_id": "tt500"},
]


def _mock_env():
    return {
        "TMDB_API_KEY": "test_key",
        "RADARR_KEY": "test_radarr",
        "SONARR_KEY": "test_sonarr",
        "RAPIDAPI_KEY": "test_rapid",
    }


@patch.dict(os.environ, _mock_env())
@patch("streaming.trending_add.fetch_movies", return_value=[
    {"tmdb_id": 100, "title": "Movie A", "year": 2025},  # Already in library
])
@patch("streaming.trending_add.fetch_series", return_value=[])
@patch("streaming.trending_add.search_catalog")
@patch("streaming.trending_add._notify_results")
def test_sync_dry_run_skips_existing(mock_notify, mock_catalog, mock_series, mock_movies, tmp_path):
    mock_catalog.return_value = MOCK_CATALOG[:2]

    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["-v", "sync", "--dry-run", "--movies", "2", "--series", "0"])
        assert result.exit_code == 0
        # Check notify was called: added should contain Movie B but not Movie A
        added = mock_notify.call_args[0][1]
        skipped = mock_notify.call_args[0][2]
        added_titles = {i["title"] for i in added}
        skipped_titles = {i["title"] for i in skipped}
        assert "Movie B" in added_titles
        assert "Movie A" in skipped_titles


@patch.dict(os.environ, _mock_env())
@patch("streaming.trending_add.fetch_movies", return_value=[])
@patch("streaming.trending_add.fetch_series", return_value=[])
@patch("streaming.trending_add.search_catalog")
@patch("streaming.trending_add._notify_results")
def test_sync_dry_run_animated_routing(mock_notify, mock_catalog, mock_series, mock_movies, tmp_path, caplog):
    mock_catalog.return_value = MOCK_CATALOG[:2]  # Movie A (Action) + Movie B (Animation)

    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["-v", "sync", "--dry-run", "--movies", "2", "--series", "0"])
        assert result.exit_code == 0
        # Movie B should route to moviesanimated — check added items
        added = mock_notify.call_args[0][1]
        assert any(i["title"] == "Movie B" and _is_animated(i.get("genres", [])) for i in added)


@patch.dict(os.environ, _mock_env())
@patch("streaming.trending_add.fetch_movies", return_value=[])
@patch("streaming.trending_add.fetch_series", return_value=[])
@patch("streaming.trending_add.search_catalog", return_value=[])
def test_sync_no_results(mock_catalog, mock_series, mock_movies, tmp_path):
    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["sync", "--dry-run"])
        assert result.exit_code == 0


@patch.dict(os.environ, _mock_env())
def test_cache_status_no_cache(tmp_path):
    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(cli, ["cache-status"])
        assert result.exit_code == 0
        assert "no cache" in result.output


@patch.dict(os.environ, _mock_env())
def test_cache_status_with_cache(tmp_path):
    with patch("streaming.trending_add.CACHE_DIR", tmp_path):
        _save_cache("apple", "movie", MOCK_CATALOG)
        runner = CliRunner()
        result = runner.invoke(cli, ["cache-status"])
        assert result.exit_code == 0
        assert "3 items" in result.output
        assert "fresh" in result.output


def test_sync_requires_rapidapi_key():
    env = _mock_env()
    del env["RAPIDAPI_KEY"]
    with patch.dict(os.environ, env, clear=False):
        # load_config won't fail but cfg.rapidapi_key will be empty
        runner = CliRunner()
        result = runner.invoke(cli, ["sync"])
        assert result.exit_code == 1


# --- arr_client add functions ---

@patch("streaming.arr_client.requests.get")
@patch("streaming.arr_client.requests.post")
def test_add_movie_success(mock_post, mock_get):
    # lookup returns metadata
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={"title": "Test", "tmdbId": 123, "images": []}),
    )
    mock_get.return_value.raise_for_status = MagicMock()
    mock_post.return_value = MagicMock(
        status_code=201,
        json=MagicMock(return_value={"id": 1, "title": "Test"}),
    )
    mock_post.return_value.raise_for_status = MagicMock()

    from streaming.arr_client import add_movie
    result = add_movie("http://localhost", "key", 123, 1, "/movies", tags=[1])
    assert result is not None
    assert result["title"] == "Test"


@patch("streaming.arr_client.requests.get")
def test_lookup_movie_404(mock_get):
    mock_get.return_value = MagicMock(status_code=404)
    from streaming.arr_client import lookup_movie
    assert lookup_movie("http://localhost", "key", 999) is None


@patch("streaming.arr_client.requests.get")
@patch("streaming.arr_client.requests.post")
def test_add_series_success(mock_post, mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value=[{"title": "TestSeries", "tvdbId": 456,
                                       "seasons": [{"seasonNumber": 1, "monitored": False}]}]),
    )
    mock_get.return_value.raise_for_status = MagicMock()
    mock_post.return_value = MagicMock(
        status_code=201,
        json=MagicMock(return_value={"id": 2, "title": "TestSeries"}),
    )
    mock_post.return_value.raise_for_status = MagicMock()

    from streaming.arr_client import add_series
    result = add_series("http://localhost", "key", 456, 4, "/tv", tags=[2])
    assert result is not None
    # Verify all seasons monitored
    call_json = mock_post.call_args[1]["json"]
    assert all(s["monitored"] for s in call_json["seasons"])


# --- streaming_api_client search_catalog ---

@patch("streaming.streaming_api_client.requests.get")
def test_search_catalog_parses_tmdb_id(mock_get):
    mock_get.return_value = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "shows": [
                {"tmdbId": "movie/12345", "title": "Test Film",
                 "releaseYear": 2025, "showType": "movie",
                 "genres": [{"name": "Action"}], "imdbId": "tt999"},
            ],
            "hasMore": False,
        }),
    )
    mock_get.return_value.raise_for_status = MagicMock()

    from streaming.streaming_api_client import search_catalog
    results = search_catalog("key", "apple", "movie", limit=5)
    assert len(results) == 1
    assert results[0]["tmdb_id"] == 12345
    assert results[0]["genres"] == ["Action"]


@patch("streaming.streaming_api_client.requests.get")
def test_search_catalog_rate_limit(mock_get):
    mock_get.return_value = MagicMock(status_code=429)

    from streaming.streaming_api_client import search_catalog
    results = search_catalog("key", "apple", "movie")
    assert results == []


@patch("streaming.streaming_api_client.requests.get")
def test_search_catalog_pagination(mock_get):
    page1 = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "shows": [{"tmdbId": f"movie/{i}", "title": f"Film {i}",
                        "releaseYear": 2025, "showType": "movie",
                        "genres": [], "imdbId": f"tt{i}"} for i in range(20)],
            "hasMore": True,
            "nextCursor": "abc123",
        }),
    )
    page1.raise_for_status = MagicMock()
    page2 = MagicMock(
        status_code=200,
        json=MagicMock(return_value={
            "shows": [{"tmdbId": f"movie/{i}", "title": f"Film {i}",
                        "releaseYear": 2025, "showType": "movie",
                        "genres": [], "imdbId": f"tt{i}"} for i in range(20, 35)],
            "hasMore": False,
        }),
    )
    page2.raise_for_status = MagicMock()
    mock_get.side_effect = [page1, page2]

    from streaming.streaming_api_client import search_catalog
    results = search_catalog("key", "apple", "movie", limit=30)
    assert len(results) == 30
