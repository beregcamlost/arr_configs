"""Tests for the Emby client."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.emby_client import get_last_played_map, is_playing, refresh_item, refresh_library


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


class TestGetLastPlayedMap:
    @patch("streaming.emby_client.requests.get")
    def test_activity_log_to_play_map(self, mock_get):
        """Activity log playback.stop events resolved to paths."""
        activity_resp = _mock_response({
            "Items": [
                {"Type": "playback.stop", "ItemId": "100", "Date": "2026-01-15T10:00:00.000Z"},
                {"Type": "playback.stop", "ItemId": "200", "Date": "2026-02-01T12:00:00.000Z"},
                {"Type": "playback.start", "ItemId": "100", "Date": "2026-01-15T09:00:00.000Z"},
            ],
            "TotalRecordCount": 3,
        })
        items_resp = _mock_response({
            "Items": [
                {"Id": "100", "Path": "/media/movies/Fight Club (1999)/movie.mkv"},
                {"Id": "200", "Path": "/media/tv/Breaking Bad/Season 1/ep1.mkv"},
            ],
        })
        mock_get.side_effect = [activity_resp, items_resp]
        result = get_last_played_map("http://localhost:8096", "key")
        assert result["/media/movies/Fight Club (1999)"] == "2026-01-15T10:00:00Z"
        assert result["/media/tv/Breaking Bad"] == "2026-02-01T12:00:00Z"

    @patch("streaming.emby_client.requests.get")
    def test_keeps_latest_date(self, mock_get):
        """Multiple playback events for same item keeps the latest."""
        activity_resp = _mock_response({
            "Items": [
                {"Type": "playback.stop", "ItemId": "100", "Date": "2026-01-01T00:00:00.000Z"},
                {"Type": "playback.stop", "ItemId": "100", "Date": "2026-02-15T00:00:00.000Z"},
            ],
            "TotalRecordCount": 2,
        })
        items_resp = _mock_response({
            "Items": [
                {"Id": "100", "Path": "/media/movies/Fight Club (1999)/movie.mkv"},
            ],
        })
        mock_get.side_effect = [activity_resp, items_resp]
        result = get_last_played_map("http://localhost:8096", "key")
        assert result["/media/movies/Fight Club (1999)"] == "2026-02-15T00:00:00Z"

    @patch("streaming.emby_client.requests.get")
    def test_tv_episodes_aggregate_to_series(self, mock_get):
        """Multiple episodes from same series aggregate to series dir."""
        activity_resp = _mock_response({
            "Items": [
                {"Type": "playback.stop", "ItemId": "300", "Date": "2026-01-01T00:00:00.000Z"},
                {"Type": "playback.stop", "ItemId": "301", "Date": "2026-02-01T00:00:00.000Z"},
            ],
            "TotalRecordCount": 2,
        })
        items_resp = _mock_response({
            "Items": [
                {"Id": "300", "Path": "/media/tv/Breaking Bad/Season 1/ep1.mkv"},
                {"Id": "301", "Path": "/media/tv/Breaking Bad/Season 2/ep1.mkv"},
            ],
        })
        mock_get.side_effect = [activity_resp, items_resp]
        result = get_last_played_map("http://localhost:8096", "key")
        assert len(result) == 1
        assert result["/media/tv/Breaking Bad"] == "2026-02-01T00:00:00Z"

    @patch("streaming.emby_client.requests.get")
    def test_no_playback_events_returns_empty(self, mock_get):
        activity_resp = _mock_response({
            "Items": [
                {"Type": "AuthenticationSucceeded", "Date": "2026-01-01T00:00:00.000Z"},
            ],
            "TotalRecordCount": 1,
        })
        mock_get.side_effect = [activity_resp]
        result = get_last_played_map("http://localhost:8096", "key")
        assert result == {}

    @patch("streaming.emby_client.requests.get")
    def test_activity_log_failure_returns_empty(self, mock_get):
        mock_get.side_effect = Exception("connection refused")
        result = get_last_played_map("http://localhost:8096", "key")
        assert result == {}


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
