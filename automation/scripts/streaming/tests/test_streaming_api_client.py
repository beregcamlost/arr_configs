"""Tests for the Streaming Availability API client."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.streaming_api_client import (
    ADDON_TO_PROVIDER,
    get_season_availability,
    get_streaming_providers,
    get_watchmode_providers,
)


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

    @patch("streaming.streaming_api_client.requests.get")
    def test_return_status_true_on_success(self, mock_get):
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [{"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"}]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result, success = get_streaming_providers("test-key", 550, "movie", "cl", return_status=True)
        assert success is True
        assert len(result) == 1

    @patch("streaming.streaming_api_client.requests.get")
    def test_return_status_true_on_404(self, mock_get):
        """404 = API checked, item not found — success=True."""
        mock_get.return_value.status_code = 404
        result, success = get_streaming_providers("test-key", 550, "movie", "cl", return_status=True)
        assert success is True
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_return_status_false_on_429(self, mock_get):
        """429 rate limit = API didn't check — success=False."""
        mock_get.return_value.status_code = 429
        result, success = get_streaming_providers("test-key", 550, "movie", "cl", return_status=True)
        assert success is False
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_return_status_false_on_network_error(self, mock_get):
        import requests as req
        mock_get.side_effect = req.RequestException("timeout")
        result, success = get_streaming_providers("test-key", 550, "movie", "cl", return_status=True)
        assert success is False
        assert result == []


class TestGetWatchmodeProviders:
    """Tests for get_watchmode_providers() — Watchmode API streaming lookup."""

    def test_no_api_key_returns_false(self):
        result, success = get_watchmode_providers("", 550, "movie")
        assert result == []
        assert success is False

    @patch("streaming.streaming_api_client.requests.get")
    def test_found_on_netflix(self, mock_get):
        # First call: search → returns watchmode ID
        search_resp = _mock_response({"title_results": [{"id": 12345}]})
        # Second call: sources → returns Netflix sub
        sources_resp = _mock_response([
            {"type": "sub", "source_id": 203},  # Netflix
            {"type": "buy", "source_id": 371},   # Apple (buy, not sub)
        ])
        mock_get.side_effect = [search_resp, sources_resp]

        result, success = get_watchmode_providers("test-key", 550, "movie")
        assert success is True
        assert len(result) == 1
        assert result[0]["provider_id"] == 8  # Netflix TMDB ID

    @patch("streaming.streaming_api_client.requests.get")
    def test_not_found_returns_empty_success(self, mock_get):
        """Title not found = API checked, not streaming."""
        mock_get.return_value = _mock_response({"title_results": []})
        result, success = get_watchmode_providers("test-key", 99999, "movie")
        assert success is True
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_search_rate_limit(self, mock_get):
        resp = MagicMock()
        resp.status_code = 429
        mock_get.return_value = resp
        result, success = get_watchmode_providers("test-key", 550, "movie")
        assert success is False
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_search_network_error(self, mock_get):
        import requests as req
        mock_get.side_effect = req.RequestException("timeout")
        result, success = get_watchmode_providers("test-key", 550, "movie")
        assert success is False
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_sources_rate_limit(self, mock_get):
        """Search succeeds but sources endpoint hits rate limit."""
        search_resp = _mock_response({"title_results": [{"id": 12345}]})
        sources_resp = MagicMock()
        sources_resp.status_code = 429
        mock_get.side_effect = [search_resp, sources_resp]

        result, success = get_watchmode_providers("test-key", 550, "movie")
        assert success is False
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_multiple_providers(self, mock_get):
        search_resp = _mock_response({"title_results": [{"id": 12345}]})
        sources_resp = _mock_response([
            {"type": "sub", "source_id": 203},  # Netflix
            {"type": "sub", "source_id": 157},  # Disney+
            {"type": "sub", "source_id": 387},  # Max
        ])
        mock_get.side_effect = [search_resp, sources_resp]

        result, success = get_watchmode_providers("test-key", 550, "movie")
        assert success is True
        assert len(result) == 3
        provider_ids = {r["provider_id"] for r in result}
        assert provider_ids == {8, 337, 384}

    @patch("streaming.streaming_api_client.requests.get")
    def test_filters_unknown_source_ids(self, mock_get):
        """Source IDs not in WATCHMODE_TO_PROVIDER mapping are ignored."""
        search_resp = _mock_response({"title_results": [{"id": 12345}]})
        sources_resp = _mock_response([
            {"type": "sub", "source_id": 203},  # Netflix (known)
            {"type": "sub", "source_id": 999},  # Unknown
        ])
        mock_get.side_effect = [search_resp, sources_resp]

        result, success = get_watchmode_providers("test-key", 550, "movie")
        assert success is True
        assert len(result) == 1
        assert result[0]["provider_id"] == 8


class TestCrunchyrollAddonDetection:
    """Tests for Crunchyroll addon-type detection in get_streaming_providers."""

    @patch("streaming.streaming_api_client.requests.get")
    def test_crunchyroll_addon_detected(self, mock_get):
        """MoTN response with type=addon, service.id=crunchyrollcl → Crunchyroll provider."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "netflix", "name": "Netflix"}, "type": "subscription"},
                    {"service": {"id": "crunchyrollcl", "name": "Crunchyroll"}, "type": "addon"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 37854, "tv", country="cl")
        assert len(result) == 2
        service_ids = {r["service_id"] for r in result}
        assert "netflix" in service_ids
        assert "crunchyrollcl" in service_ids

    @patch("streaming.streaming_api_client.requests.get")
    def test_crunchyroll_subscription_type(self, mock_get):
        """Standard subscription-type Crunchyroll works as before."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "crunchyroll", "name": "Crunchyroll"}, "type": "subscription"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 37854, "tv", country="cl")
        assert len(result) == 1
        assert result[0]["service_id"] == "crunchyroll"

    @patch("streaming.streaming_api_client.requests.get")
    def test_addon_ignored_for_unknown_service(self, mock_get):
        """Unknown addon service IDs don't leak through."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "unknownaddon", "name": "Unknown"}, "type": "addon"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 550, "movie", country="cl")
        assert result == []

    @patch("streaming.streaming_api_client.requests.get")
    def test_addon_not_duplicated_if_subscription_exists(self, mock_get):
        """If crunchyrollcl appears as both subscription and addon, no duplicate."""
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "streamingOptions": {
                "cl": [
                    {"service": {"id": "crunchyrollcl", "name": "Crunchyroll"}, "type": "subscription"},
                    {"service": {"id": "crunchyrollcl", "name": "Crunchyroll"}, "type": "addon"},
                ]
            }
        }
        mock_get.return_value.raise_for_status = lambda: None
        result = get_streaming_providers("test-key", 37854, "tv", country="cl")
        assert len(result) == 1

    def test_addon_to_provider_mapping(self):
        assert ADDON_TO_PROVIDER == {"crunchyrollcl": 283}
