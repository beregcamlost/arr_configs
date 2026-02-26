"""Tests for the Emby client."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.emby_client import is_playing, refresh_item, refresh_library


def _mock_response(json_data=None, status_code=200):
    resp = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


class TestRefreshLibrary:
    @patch("streaming.emby_client.requests.post")
    def test_triggers_refresh(self, mock_post):
        mock_post.return_value = _mock_response()
        refresh_library("http://localhost:8096", "key")
        mock_post.assert_called_once()
        assert "Library/Refresh" in mock_post.call_args[0][0]


class TestIsPlaying:
    @patch("streaming.emby_client.requests.get")
    def test_detects_active_playback(self, mock_get):
        sessions = [
            {
                "NowPlayingItem": {
                    "Path": "/media/movies/Fight Club (1999)/movie.mkv"
                }
            }
        ]
        mock_get.return_value = _mock_response(sessions)
        assert is_playing(
            "http://localhost:8096", "key",
            "/media/movies/Fight Club (1999)/movie.mkv"
        ) is True

    @patch("streaming.emby_client.requests.get")
    def test_no_active_playback(self, mock_get):
        sessions = [{"NowPlayingItem": {}}]
        mock_get.return_value = _mock_response(sessions)
        assert is_playing(
            "http://localhost:8096", "key",
            "/media/movies/Fight Club (1999)/movie.mkv"
        ) is False


class TestRefreshItem:
    @patch("streaming.emby_client.requests.post")
    @patch("streaming.emby_client.requests.get")
    def test_finds_and_refreshes(self, mock_get, mock_post):
        search_resp = _mock_response({
            "Items": [
                {
                    "Id": "abc123",
                    "Path": "/media/movies/Fight Club (1999)/movie.mkv",
                }
            ]
        })
        mock_get.return_value = search_resp
        mock_post.return_value = _mock_response()

        result = refresh_item(
            "http://localhost:8096", "key",
            "/media/movies/Fight Club (1999)/movie.mkv"
        )
        assert result is True
        assert "abc123/Refresh" in mock_post.call_args[0][0]

    @patch("streaming.emby_client.requests.get")
    def test_item_not_found(self, mock_get):
        mock_get.return_value = _mock_response({"Items": []})
        result = refresh_item(
            "http://localhost:8096", "key",
            "/media/movies/Unknown Movie (2020)/movie.mkv"
        )
        assert result is False
