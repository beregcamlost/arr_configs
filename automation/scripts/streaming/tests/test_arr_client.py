"""Tests for the Radarr/Sonarr API client."""

from unittest.mock import MagicMock, call, patch

import pytest

from streaming.arr_client import (
    add_tag_to_item,
    create_tag,
    delete_item,
    ensure_tag,
    fetch_movies,
    fetch_series,
    get_item,
    get_tag_id,
    remove_tag_from_item,
)


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


RADARR_MOVIES = [
    {
        "id": 1,
        "tmdbId": 550,
        "title": "Fight Club",
        "year": 1999,
        "path": "/media/movies/Fight Club (1999)",
        "sizeOnDisk": 5_000_000_000,
        "tags": [1],
        "hasFile": True,
    },
    {
        "id": 2,
        "tmdbId": 862,
        "title": "Toy Story",
        "year": 1995,
        "path": "/media/moviesanimated/Toy Story (1995)",
        "sizeOnDisk": 2_000_000_000,
        "tags": [],
        "hasFile": True,
    },
]

SONARR_SERIES = [
    {
        "id": 10,
        "tmdbId": 1396,
        "title": "Breaking Bad",
        "year": 2008,
        "path": "/media/tv/Breaking Bad",
        "tags": [],
        "statistics": {"sizeOnDisk": 80_000_000_000},
    },
    {
        "id": 11,
        "tmdbId": 0,
        "title": "No TMDB",
        "year": 2020,
        "path": "/media/tvanimated/No TMDB",
        "tags": [],
        "statistics": {"sizeOnDisk": 1_000_000},
    },
]

TAGS = [
    {"id": 1, "label": "streaming-available"},
    {"id": 4, "label": "keep-local"},
]


class TestFetchMovies:
    @patch("streaming.arr_client.requests.get")
    def test_fetches_and_maps(self, mock_get):
        mock_get.return_value = _mock_response(RADARR_MOVIES)
        movies = fetch_movies("http://localhost:7878/radarr", "key")
        assert len(movies) == 2
        assert movies[0]["tmdb_id"] == 550
        assert movies[0]["library"] == "movies"
        assert movies[0]["has_file"] is True
        assert movies[1]["library"] == "moviesanimated"

    @patch("streaming.arr_client.requests.get")
    def test_empty_library(self, mock_get):
        mock_get.return_value = _mock_response([])
        movies = fetch_movies("http://localhost:7878/radarr", "key")
        assert movies == []


class TestFetchSeries:
    @patch("streaming.arr_client.requests.get")
    def test_fetches_and_maps(self, mock_get):
        mock_get.return_value = _mock_response(SONARR_SERIES)
        series = fetch_series("http://localhost:8989/sonarr", "key")
        assert len(series) == 2
        assert series[0]["tmdb_id"] == 1396
        assert series[0]["library"] == "tv"
        assert series[0]["size_bytes"] == 80_000_000_000
        assert series[1]["library"] == "tvanimated"
        assert series[1]["tmdb_id"] == 0


class TestTagCrud:
    @patch("streaming.arr_client.requests.get")
    def test_get_tag_id_found(self, mock_get):
        mock_get.return_value = _mock_response(TAGS)
        assert get_tag_id("http://localhost", "key", "keep-local") == 4

    @patch("streaming.arr_client.requests.get")
    def test_get_tag_id_not_found(self, mock_get):
        mock_get.return_value = _mock_response(TAGS)
        assert get_tag_id("http://localhost", "key", "nonexistent") is None

    @patch("streaming.arr_client.requests.post")
    def test_create_tag(self, mock_post):
        mock_post.return_value = _mock_response({"id": 10, "label": "new-tag"})
        tag_id = create_tag("http://localhost", "key", "new-tag")
        assert tag_id == 10

    @patch("streaming.arr_client.requests.post")
    @patch("streaming.arr_client.requests.get")
    def test_ensure_tag_exists(self, mock_get, mock_post):
        mock_get.return_value = _mock_response(TAGS)
        tag_id = ensure_tag("http://localhost", "key", "keep-local")
        assert tag_id == 4
        mock_post.assert_not_called()

    @patch("streaming.arr_client.requests.post")
    @patch("streaming.arr_client.requests.get")
    def test_ensure_tag_creates(self, mock_get, mock_post):
        mock_get.return_value = _mock_response(TAGS)
        mock_post.return_value = _mock_response({"id": 10, "label": "new-tag"})
        tag_id = ensure_tag("http://localhost", "key", "new-tag")
        assert tag_id == 10
        mock_post.assert_called_once()


class TestAddRemoveTag:
    @patch("streaming.arr_client.requests.put")
    @patch("streaming.arr_client.requests.get")
    def test_add_tag(self, mock_get, mock_put):
        mock_get.return_value = _mock_response({"id": 1, "tags": [1], "title": "Test"})
        mock_put.return_value = _mock_response({})
        add_tag_to_item("http://localhost", "key", "movie", 1, 5)
        put_data = mock_put.call_args[1]["json"]
        assert 5 in put_data["tags"]

    @patch("streaming.arr_client.requests.put")
    @patch("streaming.arr_client.requests.get")
    def test_add_tag_already_present(self, mock_get, mock_put):
        mock_get.return_value = _mock_response({"id": 1, "tags": [1, 5], "title": "Test"})
        add_tag_to_item("http://localhost", "key", "movie", 1, 5)
        mock_put.assert_not_called()

    @patch("streaming.arr_client.requests.put")
    @patch("streaming.arr_client.requests.get")
    def test_remove_tag(self, mock_get, mock_put):
        mock_get.return_value = _mock_response({"id": 1, "tags": [1, 5], "title": "Test"})
        mock_put.return_value = _mock_response({})
        remove_tag_from_item("http://localhost", "key", "movie", 1, 5)
        put_data = mock_put.call_args[1]["json"]
        assert 5 not in put_data["tags"]

    @patch("streaming.arr_client.requests.put")
    @patch("streaming.arr_client.requests.get")
    def test_remove_tag_not_present(self, mock_get, mock_put):
        mock_get.return_value = _mock_response({"id": 1, "tags": [1], "title": "Test"})
        remove_tag_from_item("http://localhost", "key", "movie", 1, 5)
        mock_put.assert_not_called()


class TestDeleteItem:
    @patch("streaming.arr_client.requests.delete")
    def test_delete_movie(self, mock_delete):
        mock_delete.return_value = _mock_response({})
        delete_item("http://localhost", "key", "movie", 1)
        mock_delete.assert_called_once()
        args = mock_delete.call_args
        assert "movie/1" in args[0][0]
        assert args[1]["params"]["deleteFiles"] == "true"

    @patch("streaming.arr_client.requests.delete")
    def test_delete_series(self, mock_delete):
        mock_delete.return_value = _mock_response({})
        delete_item("http://localhost", "key", "series", 10)
        args = mock_delete.call_args
        assert "series/10" in args[0][0]
        assert args[1]["params"]["addImportListExclusion"] == "false"


class TestGetItem:
    @patch("streaming.arr_client.requests.get")
    def test_get_existing(self, mock_get):
        mock_get.return_value = _mock_response({"id": 1, "title": "Test"})
        item = get_item("http://localhost", "key", "movie", 1)
        assert item["title"] == "Test"

    @patch("streaming.arr_client.requests.get")
    def test_get_not_found(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.return_value = resp
        item = get_item("http://localhost", "key", "movie", 999)
        assert item is None
