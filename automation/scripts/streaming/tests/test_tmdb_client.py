"""Tests for the TMDB Watch Providers client."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.tmdb_client import batch_check, check_streaming, get_watch_providers


def _mock_response(json_data, status_code=200):
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


TMDB_CL_NETFLIX = {
    "results": {
        "CL": {
            "flatrate": [
                {"provider_id": 8, "provider_name": "Netflix"},
            ],
            "rent": [
                {"provider_id": 3, "provider_name": "Google Play Movies"},
            ],
        }
    }
}

TMDB_CL_BOTH = {
    "results": {
        "CL": {
            "flatrate": [
                {"provider_id": 8, "provider_name": "Netflix"},
                {"provider_id": 337, "provider_name": "Disney Plus"},
            ],
        }
    }
}

TMDB_NO_CL = {
    "results": {
        "US": {
            "flatrate": [{"provider_id": 8, "provider_name": "Netflix"}],
        }
    }
}

TMDB_NO_FLATRATE = {
    "results": {
        "CL": {
            "rent": [{"provider_id": 3, "provider_name": "Google Play Movies"}],
        }
    }
}


class TestGetWatchProviders:
    @patch("streaming.tmdb_client.requests.get")
    def test_returns_country_data(self, mock_get):
        mock_get.return_value = _mock_response(TMDB_CL_NETFLIX)
        result = get_watch_providers("key", 550, "movie", "CL")
        assert "flatrate" in result
        assert result["flatrate"][0]["provider_name"] == "Netflix"

    @patch("streaming.tmdb_client.requests.get")
    def test_no_country_data(self, mock_get):
        mock_get.return_value = _mock_response(TMDB_NO_CL)
        result = get_watch_providers("key", 550, "movie", "CL")
        assert result == {}


class TestCheckStreaming:
    @patch("streaming.tmdb_client.requests.get")
    def test_finds_netflix(self, mock_get):
        mock_get.return_value = _mock_response(TMDB_CL_NETFLIX)
        matches = check_streaming("key", 550, "movie", [8, 337], "CL")
        assert len(matches) == 1
        assert matches[0]["provider_id"] == 8

    @patch("streaming.tmdb_client.requests.get")
    def test_finds_both_providers(self, mock_get):
        mock_get.return_value = _mock_response(TMDB_CL_BOTH)
        matches = check_streaming("key", 550, "movie", [8, 337], "CL")
        assert len(matches) == 2

    @patch("streaming.tmdb_client.requests.get")
    def test_rent_not_matched(self, mock_get):
        mock_get.return_value = _mock_response(TMDB_NO_FLATRATE)
        matches = check_streaming("key", 550, "movie", [8, 337], "CL")
        assert len(matches) == 0

    @patch("streaming.tmdb_client.requests.get")
    def test_no_country_returns_empty(self, mock_get):
        mock_get.return_value = _mock_response(TMDB_NO_CL)
        matches = check_streaming("key", 550, "movie", [8, 337], "CL")
        assert len(matches) == 0


class TestBatchCheck:
    @patch("streaming.tmdb_client.requests.get")
    @patch("streaming.tmdb_client.time.sleep")
    def test_batch_returns_matches(self, mock_sleep, mock_get):
        mock_get.return_value = _mock_response(TMDB_CL_NETFLIX)
        items = [
            {"tmdb_id": 550, "media_type": "movie", "title": "Fight Club"},
            {"tmdb_id": 600, "media_type": "movie", "title": "Toy Story"},
        ]
        results = batch_check("key", items, [8, 337], "CL", max_workers=2)
        assert (550, "movie") in results
        assert (600, "movie") in results

    @patch("streaming.tmdb_client.requests.get")
    @patch("streaming.tmdb_client.time.sleep")
    def test_batch_skips_zero_tmdb_id(self, mock_sleep, mock_get):
        mock_get.return_value = _mock_response(TMDB_CL_NETFLIX)
        items = [
            {"tmdb_id": 0, "media_type": "movie", "title": "No ID"},
            {"tmdb_id": 550, "media_type": "movie", "title": "Fight Club"},
        ]
        results = batch_check("key", items, [8, 337], "CL", max_workers=2)
        assert (0, "movie") not in results
        assert (550, "movie") in results

    @patch("streaming.tmdb_client.requests.get")
    @patch("streaming.tmdb_client.time.sleep")
    def test_batch_handles_api_error(self, mock_sleep, mock_get):
        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.raise_for_status.side_effect = __import__("requests").exceptions.HTTPError(
            response=error_resp
        )
        mock_get.return_value = error_resp
        items = [{"tmdb_id": 550, "media_type": "movie", "title": "Fight Club"}]
        results = batch_check("key", items, [8, 337], "CL", max_workers=2)
        assert len(results) == 0
