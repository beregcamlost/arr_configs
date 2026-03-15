"""Tests for the JustWatch GraphQL client."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.justwatch_client import (
    JUSTWATCH_TO_PROVIDER,
    get_justwatch_providers,
)


def _mock_response(json_data=None, status_code=200):
    resp = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status.return_value = None
    return resp


def _graphql_response(*nodes):
    """Build a JustWatch GraphQL response with the given nodes."""
    return {
        "data": {
            "popularTitles": {
                "edges": [{"node": n} for n in nodes],
            }
        }
    }


def _node(tmdb_id, obj_type, package_ids):
    """Build a node with FLATRATE offers for the given package IDs."""
    return {
        "objectType": obj_type,
        "content": {"externalIds": {"tmdbId": str(tmdb_id)}},
        "offers": [
            {"monetizationType": "FLATRATE", "package": {"packageId": pid}}
            for pid in package_ids
        ],
    }


class TestGetJustwatchProviders:
    @patch("streaming.justwatch_client.requests.post")
    def test_success_netflix_disney(self, mock_post):
        """Netflix + Disney offers → returns both provider IDs."""
        node = _node(550, "MOVIE", [8, 337])
        mock_post.return_value = _mock_response(_graphql_response(node))

        result, success = get_justwatch_providers(550, "movie", "Fight Club", country="CL")
        assert success is True
        assert len(result) == 2
        provider_ids = {r["provider_id"] for r in result}
        assert provider_ids == {8, 337}

    @patch("streaming.justwatch_client.requests.post")
    def test_not_found_returns_empty_true(self, mock_post):
        """No matching TMDB ID in results → ([], True)."""
        node = _node(999, "MOVIE", [8])  # Different TMDB ID
        mock_post.return_value = _mock_response(_graphql_response(node))

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is True
        assert result == []

    @patch("streaming.justwatch_client.requests.post")
    def test_network_error_returns_false(self, mock_post):
        import requests as req
        mock_post.side_effect = req.ConnectionError("timeout")

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is False
        assert result == []

    @patch("streaming.justwatch_client.requests.post")
    def test_timeout_returns_false(self, mock_post):
        import requests as req
        mock_post.side_effect = req.Timeout("read timed out")

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is False
        assert result == []

    @patch("streaming.justwatch_client.requests.post")
    def test_rate_limit_returns_false(self, mock_post):
        mock_post.return_value = _mock_response(status_code=429)

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is False
        assert result == []

    @patch("streaming.justwatch_client.requests.post")
    def test_404_returns_true(self, mock_post):
        mock_post.return_value = _mock_response(status_code=404)

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is True
        assert result == []

    @patch("streaming.justwatch_client.requests.post")
    def test_crunchyroll(self, mock_post):
        """Crunchyroll offer (packageId 283) → returns provider 283."""
        node = _node(37854, "SHOW", [283])
        mock_post.return_value = _mock_response(_graphql_response(node))

        result, success = get_justwatch_providers(37854, "tv", "One Piece", country="CL")
        assert success is True
        assert len(result) == 1
        assert result[0]["provider_id"] == 283

    @patch("streaming.justwatch_client.requests.post")
    def test_hbo_max_mapping(self, mock_post):
        """JustWatch HBO Max (1899) maps to TMDB provider ID 384."""
        node = _node(1396, "SHOW", [1899])
        mock_post.return_value = _mock_response(_graphql_response(node))

        result, success = get_justwatch_providers(1396, "tv", "Breaking Bad")
        assert success is True
        assert result[0]["provider_id"] == 384

    @patch("streaming.justwatch_client.requests.post")
    def test_filters_non_flatrate(self, mock_post):
        """Only FLATRATE offers are returned, not rent/buy."""
        node = {
            "objectType": "MOVIE",
            "content": {"externalIds": {"tmdbId": "550"}},
            "offers": [
                {"monetizationType": "FLATRATE", "package": {"packageId": 8}},
                {"monetizationType": "RENT", "package": {"packageId": 337}},
                {"monetizationType": "BUY", "package": {"packageId": 119}},
            ],
        }
        mock_post.return_value = _mock_response(_graphql_response(node))

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is True
        assert len(result) == 1
        assert result[0]["provider_id"] == 8

    @patch("streaming.justwatch_client.requests.post")
    def test_deduplicates_offers(self, mock_post):
        """Duplicate packageIds in offers are deduplicated."""
        node = _node(550, "MOVIE", [8, 8, 8, 337])
        mock_post.return_value = _mock_response(_graphql_response(node))

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert len(result) == 2

    @patch("streaming.justwatch_client.requests.post")
    def test_matches_correct_type(self, mock_post):
        """Movie search doesn't match a SHOW node with same TMDB ID."""
        show_node = _node(550, "SHOW", [8])
        movie_node = _node(550, "MOVIE", [337])
        mock_post.return_value = _mock_response(_graphql_response(show_node, movie_node))

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is True
        assert len(result) == 1
        assert result[0]["provider_id"] == 337  # From movie node

    @patch("streaming.justwatch_client.requests.post")
    def test_unknown_package_ignored(self, mock_post):
        """Package IDs not in JUSTWATCH_TO_PROVIDER are skipped."""
        node = _node(550, "MOVIE", [8, 99999])
        mock_post.return_value = _mock_response(_graphql_response(node))

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert len(result) == 1
        assert result[0]["provider_id"] == 8

    def test_empty_title_returns_true(self):
        """Empty title → ([], True) without making API call."""
        result, success = get_justwatch_providers(550, "movie", "")
        assert success is True
        assert result == []

    @patch("streaming.justwatch_client.requests.post")
    def test_empty_graphql_response(self, mock_post):
        """Empty edges → ([], True)."""
        mock_post.return_value = _mock_response({"data": {"popularTitles": {"edges": []}}})

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is True
        assert result == []

    @patch("streaming.justwatch_client.requests.post")
    def test_null_data_handling(self, mock_post):
        """Null data in response handled gracefully."""
        mock_post.return_value = _mock_response({"data": None})

        result, success = get_justwatch_providers(550, "movie", "Fight Club")
        assert success is True
        assert result == []


class TestJustwatchProviderMapping:
    def test_all_expected_providers_mapped(self):
        expected = {8, 337, 384, 119, 350, 531, 283}
        assert expected == set(JUSTWATCH_TO_PROVIDER.values())

    def test_netflix_mapping(self):
        assert JUSTWATCH_TO_PROVIDER[8] == 8

    def test_hbo_max_mapping(self):
        # JustWatch uses 1899 for HBO Max, TMDB uses 384
        assert JUSTWATCH_TO_PROVIDER[1899] == 384

    def test_crunchyroll_mapping(self):
        assert JUSTWATCH_TO_PROVIDER[283] == 283
