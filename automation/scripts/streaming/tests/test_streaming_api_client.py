"""Tests for the Streaming Availability API client."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.streaming_api_client import get_season_availability, get_streaming_providers


def _mock_response(json_data=None, status_code=200):
    resp = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


# Real API structure: title-based season numbers, no seasonNumber field
SAMPLE_RESPONSE = {
    "seasons": [
        {
            "title": "Season 1",
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix"}},
                    {"service": {"id": "amazon"}},
                ],
            },
        },
        {
            "title": "Season 2",
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix"}},
                ],
            },
        },
        {
            "title": "Season 3",
            "streamingOptions": {
                "cl": [],
            },
        },
        {
            "title": "Season 4",
            "streamingOptions": {},
        },
    ],
}


class TestGetSeasonAvailability:
    @patch("streaming.streaming_api_client.requests.get")
    def test_parses_seasons(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_RESPONSE)
        result = get_season_availability("test-key", 1396, "cl")
        assert result[1] == ["netflix", "amazon"]
        assert result[2] == ["netflix"]
        assert result[3] == []
        assert result[4] == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_url_has_tv_prefix(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_RESPONSE)
        get_season_availability("test-key", 1396, "cl")
        url = mock_get.call_args[0][0]
        assert "/shows/tv/1396" in url

    @patch("streaming.streaming_api_client.requests.get")
    def test_deduplicates_providers(self, mock_get):
        data = {
            "seasons": [
                {
                    "title": "Season 1",
                    "streamingOptions": {
                        "cl": [
                            {"service": {"id": "netflix"}},
                            {"service": {"id": "netflix"}},
                        ],
                    },
                },
            ],
        }
        mock_get.return_value = _mock_response(data)
        result = get_season_availability("test-key", 1396, "cl")
        assert result[1] == ["netflix"]

    @patch("streaming.streaming_api_client.requests.get")
    def test_fallback_to_index_when_no_title(self, mock_get):
        """Seasons without parseable title use 1-based index."""
        data = {
            "seasons": [
                {"title": None, "streamingOptions": {"cl": [{"service": {"id": "netflix"}}]}},
                {"streamingOptions": {"cl": []}},
            ],
        }
        mock_get.return_value = _mock_response(data)
        result = get_season_availability("test-key", 1396, "cl")
        assert 1 in result
        assert 2 in result

    @patch("streaming.streaming_api_client.requests.get")
    def test_rate_limit_returns_empty(self, mock_get):
        resp = MagicMock()
        resp.status_code = 429
        mock_get.return_value = resp
        result = get_season_availability("test-key", 1396, "cl")
        assert result == {}

    @patch("streaming.streaming_api_client.requests.get")
    def test_not_found_returns_empty(self, mock_get):
        resp = MagicMock()
        resp.status_code = 404
        mock_get.return_value = resp
        result = get_season_availability("test-key", 99999, "cl")
        assert result == {}

    @patch("streaming.streaming_api_client.requests.get")
    def test_auth_error_returns_empty(self, mock_get):
        resp = MagicMock()
        resp.status_code = 401
        mock_get.return_value = resp
        result = get_season_availability("bad-key", 1396, "cl")
        assert result == {}

    @patch("streaming.streaming_api_client.requests.get")
    def test_network_error_returns_empty(self, mock_get):
        import requests
        mock_get.side_effect = requests.ConnectionError("timeout")
        result = get_season_availability("test-key", 1396, "cl")
        assert result == {}

    @patch("streaming.streaming_api_client.requests.get")
    def test_empty_seasons(self, mock_get):
        mock_get.return_value = _mock_response({"seasons": []})
        result = get_season_availability("test-key", 1396, "cl")
        assert result == {}

    @patch("streaming.streaming_api_client.requests.get")
    def test_country_case_insensitive(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_RESPONSE)
        get_season_availability("test-key", 1396, "CL")
        call_kwargs = mock_get.call_args[1]
        assert call_kwargs["params"]["country"] == "cl"

    @patch("streaming.streaming_api_client.requests.get")
    def test_api_headers(self, mock_get):
        mock_get.return_value = _mock_response(SAMPLE_RESPONSE)
        get_season_availability("my-api-key", 1396, "cl")
        call_kwargs = mock_get.call_args[1]
        assert call_kwargs["headers"]["X-RapidAPI-Key"] == "my-api-key"
        assert "streaming-availability" in call_kwargs["headers"]["X-RapidAPI-Host"]


class TestGetStreamingProviders:
    """Tests for get_streaming_providers() — single-item streaming lookup."""

    @patch("streaming.streaming_api_client.requests.get")
    def test_movie_found_on_netflix(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"},
                    {"service": {"id": "apple", "name": "Apple TV"}, "type": "rent"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == [{"service_id": "netflix", "service_name": "Netflix"}]
        call_url = mock_get.call_args[0][0]
        assert "/shows/movie/550" in call_url

    @patch("streaming.streaming_api_client.requests.get")
    def test_tv_found_on_multiple(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"},
                    {"service": {"id": "disney", "name": "Disney+"}, "type": "subscription"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 1396, "tv", country="cl")
        assert len(result) == 2
        call_url = mock_get.call_args[0][0]
        assert "/shows/tv/1396" in call_url

    @patch("streaming.streaming_api_client.requests.get")
    def test_not_found_returns_empty(self, mock_get):
        mock_get.return_value.status_code = 404
        result = get_streaming_providers("test-key", 99999, "movie", country="cl")
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_rate_limit_returns_empty(self, mock_get):
        mock_get.return_value.status_code = 429
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_no_streaming_in_country_returns_empty(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {"us": [{"service": {"id": "netflix"}, "type": "subscription"}]}
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_filters_subscription_only(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"},
                    {"service": {"id": "apple", "name": "Apple TV"}, "type": "buy"},
                    {"service": {"id": "prime", "name": "Amazon Prime"}, "type": "rent"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert len(result) == 1
        assert result[0]["service_id"] == "netflix"

    @patch("streaming.streaming_api_client.requests.get")
    def test_network_error_returns_empty(self, mock_get):
        import requests as req
        mock_get.side_effect = req.RequestException("timeout")
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == []
