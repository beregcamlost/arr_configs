"""Tests for streaming_checker cross-validation voting logic."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.config import Config


def _make_cfg(**overrides):
    """Create a Config with test defaults."""
    defaults = dict(
        tmdb_api_key="test-tmdb",
        radarr_key="test-radarr",
        sonarr_key="test-sonarr",
        rapidapi_key="test-rapid",
        watchmode_api_key="test-wm",
        country="CL",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _provider(pid, name):
    return {"provider_id": pid, "provider_name": name}


# Patch all three external sources used by _cross_validate_matches
_PATCH_MOTN = "streaming.streaming_checker.get_streaming_providers"
_PATCH_WM = "streaming.streaming_checker.get_watchmode_providers"
_PATCH_JW = "streaming.streaming_checker.get_justwatch_providers"
_PATCH_SLEEP = "streaming.streaming_checker.time.sleep"


class TestCrossValidateMatches:
    """Tests for _cross_validate_matches with 4-source voting."""

    def _import_fn(self):
        from streaming.streaming_checker import _cross_validate_matches
        return _cross_validate_matches

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_all_agree_confirmed(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """TMDB + MoTN + Watchmode + JustWatch all agree → confirmed."""
        cfg = _make_cfg()
        tmdb_results = {
            (550, "movie"): [_provider(8, "Netflix")],
        }
        items_by_key = {(550, "movie"): {"title": "Fight Club"}}

        mock_motn.return_value = ([{"service_id": "netflix"}], True)
        mock_wm.return_value = ([{"provider_id": 8}], True)
        mock_jw.return_value = ([{"provider_id": 8}], True)

        fn = self._import_fn()
        result = fn(cfg, tmdb_results, items_by_key)
        assert len(result[(550, "movie")]["confirmed"]) == 1
        assert result[(550, "movie")]["disputed"] == []

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_tmdb_plus_justwatch_confirmed(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """TMDB + JustWatch agree, MoTN + Watchmode disagree → confirmed (2 votes)."""
        cfg = _make_cfg()
        tmdb_results = {
            (550, "movie"): [_provider(8, "Netflix")],
        }
        items_by_key = {(550, "movie"): {"title": "Fight Club"}}

        mock_motn.return_value = ([], True)  # MoTN says no
        mock_wm.return_value = ([], True)    # Watchmode says no
        mock_jw.return_value = ([{"provider_id": 8}], True)  # JW agrees

        fn = self._import_fn()
        result = fn(cfg, tmdb_results, items_by_key)
        assert len(result[(550, "movie")]["confirmed"]) == 1

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_only_tmdb_disputed(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """Only TMDB votes yes, all external sources disagree → disputed."""
        cfg = _make_cfg()
        tmdb_results = {
            (550, "movie"): [_provider(8, "Netflix")],
        }
        items_by_key = {(550, "movie"): {"title": "Fight Club"}}

        mock_motn.return_value = ([], True)
        mock_wm.return_value = ([], True)
        mock_jw.return_value = ([], True)

        fn = self._import_fn()
        result = fn(cfg, tmdb_results, items_by_key)
        assert result[(550, "movie")]["confirmed"] == []
        assert len(result[(550, "movie")]["disputed"]) == 1

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_justwatch_error_no_vote(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """JustWatch error (jw_available=False) → doesn't affect vote count."""
        cfg = _make_cfg()
        tmdb_results = {
            (550, "movie"): [_provider(8, "Netflix")],
        }
        items_by_key = {(550, "movie"): {"title": "Fight Club"}}

        mock_motn.return_value = ([{"service_id": "netflix"}], True)  # MoTN agrees
        mock_wm.return_value = ([], True)     # Watchmode disagrees
        mock_jw.return_value = ([], False)    # JW error — not counted

        fn = self._import_fn()
        result = fn(cfg, tmdb_results, items_by_key)
        # TMDB + MoTN = 2 votes, confirmed
        assert len(result[(550, "movie")]["confirmed"]) == 1

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_no_external_sources_confirmed(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """No external source reachable → treat as confirmed."""
        cfg = _make_cfg(rapidapi_key="", watchmode_api_key="")
        tmdb_results = {
            (550, "movie"): [_provider(8, "Netflix")],
        }
        items_by_key = {(550, "movie"): {"title": "Fight Club"}}

        mock_motn.return_value = ([], False)
        mock_wm.return_value = ([], False)
        mock_jw.return_value = ([], False)

        fn = self._import_fn()
        result = fn(cfg, tmdb_results, items_by_key)
        assert len(result[(550, "movie")]["confirmed"]) == 1

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_no_item_context_confirmed(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """No item in items_by_key → treat as confirmed without API calls."""
        cfg = _make_cfg()
        tmdb_results = {
            (550, "movie"): [_provider(8, "Netflix")],
        }
        items_by_key = {}  # No item context

        fn = self._import_fn()
        result = fn(cfg, tmdb_results, items_by_key)
        assert len(result[(550, "movie")]["confirmed"]) == 1
        # No external API should have been called
        mock_motn.assert_not_called()
        mock_wm.assert_not_called()
        mock_jw.assert_not_called()

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_empty_results(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """Empty tmdb_results → empty dict."""
        cfg = _make_cfg()
        fn = self._import_fn()
        result = fn(cfg, {}, {})
        assert result == {}

    @patch(_PATCH_SLEEP)
    @patch(_PATCH_JW)
    @patch(_PATCH_WM)
    @patch(_PATCH_MOTN)
    def test_justwatch_receives_title(self, mock_motn, mock_wm, mock_jw, mock_sleep):
        """JustWatch is called with the item's title."""
        cfg = _make_cfg()
        tmdb_results = {
            (550, "movie"): [_provider(8, "Netflix")],
        }
        items_by_key = {(550, "movie"): {"title": "Fight Club"}}

        mock_motn.return_value = ([], False)
        mock_wm.return_value = ([], False)
        mock_jw.return_value = ([], True)

        fn = self._import_fn()
        fn(cfg, tmdb_results, items_by_key)

        mock_jw.assert_called_once_with(
            550, "movie", "Fight Club", country="CL",
        )
