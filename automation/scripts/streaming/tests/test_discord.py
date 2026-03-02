"""Tests for the Discord notification module."""

from unittest.mock import MagicMock, patch

import pytest

from streaming.discord import (
    BLUE,
    GREEN,
    ORANGE,
    RED,
    YELLOW,
    format_size,
    notify_deletion,
    notify_scan_results,
    send_embed,
)


class TestFormatSize:
    def test_zero(self):
        assert format_size(0) == "0 B"

    def test_bytes(self):
        assert format_size(500) == "500.0 B"

    def test_kilobytes(self):
        assert format_size(1536) == "1.5 KB"

    def test_megabytes(self):
        assert format_size(10_485_760) == "10.0 MB"

    def test_gigabytes(self):
        assert format_size(2_147_483_648) == "2.0 GB"

    def test_terabytes(self):
        assert format_size(1_099_511_627_776) == "1.0 TB"

    def test_none(self):
        assert format_size(None) == "0 B"


class TestSendEmbed:
    @patch("streaming.discord.requests.post")
    def test_sends_embed(self, mock_post):
        mock_post.return_value = MagicMock(raise_for_status=MagicMock())
        send_embed("https://hooks.discord.com/test", "Title", "Desc", GREEN)
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["embeds"][0]["title"] == "Title"
        assert payload["embeds"][0]["color"] == GREEN

    @patch("streaming.discord.requests.post")
    def test_no_webhook_url_skips(self, mock_post):
        send_embed("", "Title", "Desc", GREEN)
        mock_post.assert_not_called()

    @patch("streaming.discord.requests.post")
    def test_webhook_failure_suppressed(self, mock_post):
        mock_post.side_effect = Exception("network error")
        # Should not raise
        send_embed("https://hooks.discord.com/test", "Title", "Desc", GREEN)


class TestNotifyScanResults:
    @patch("streaming.discord.send_embed")
    def test_empty_results_skips(self, mock_send):
        notify_scan_results("https://hook", [], [], {})
        mock_send.assert_not_called()

    @patch("streaming.discord.send_embed")
    def test_new_items_grouped_by_provider(self, mock_send):
        new_items = [
            {"title": "Fight Club", "year": 1999, "provider_name": "Netflix",
             "library": "movies", "size_bytes": 5_000_000_000},
            {"title": "Toy Story", "year": 1995, "provider_name": "Disney Plus",
             "library": "moviesanimated", "size_bytes": 2_000_000_000},
        ]
        stats = {"movies_checked": 100, "series_checked": 50, "matches_found": 2, "duration_seconds": 5.0}
        notify_scan_results("https://hook", new_items, [], stats)
        mock_send.assert_called_once()
        args = mock_send.call_args
        fields = args[1].get("fields") or args[0][4]
        # Should have two provider fields
        provider_names = [f["name"] for f in fields]
        assert any("Netflix" in n for n in provider_names)
        assert any("Disney Plus" in n for n in provider_names)

    @patch("streaming.discord.send_embed")
    def test_left_items_notification(self, mock_send):
        left_items = [
            {"title": "Old Movie", "provider_name": "Netflix"},
        ]
        stats = {"movies_checked": 100, "series_checked": 50, "matches_found": 0, "duration_seconds": 3.0}
        notify_scan_results("https://hook", [], left_items, stats)
        mock_send.assert_called_once()
        args = mock_send.call_args
        assert args[0][3] == YELLOW  # color (left-only = yellow)

    @patch("streaming.discord.send_embed")
    def test_stale_count_in_description(self, mock_send):
        new_items = [
            {"title": "Test", "year": 2020, "provider_name": "Netflix",
             "library": "movies", "size_bytes": 1_000_000_000},
        ]
        stats = {"movies_checked": 50, "series_checked": 20, "matches_found": 1, "duration_seconds": 2.0}
        notify_scan_results("https://hook", new_items, [], stats, stale_count=15)
        mock_send.assert_called_once()
        desc = mock_send.call_args[0][2]
        assert "15" in desc
        assert "90+" in desc


class TestNotifyDeletion:
    @patch("streaming.discord.send_embed")
    def test_deletion_notification(self, mock_send):
        deleted = [
            {"title": "Fight Club", "year": 1999, "provider_name": "Netflix"},
        ]
        notify_deletion("https://hook", deleted, 5_000_000_000)
        mock_send.assert_called_once()
        args = mock_send.call_args
        assert args[0][3] == RED  # color (deletion = red)
        fields = args[1].get("fields") or args[0][4]
        assert any("4.7 GB" in f["value"] for f in fields)

    @patch("streaming.discord.send_embed")
    def test_empty_deletion_skips(self, mock_send):
        notify_deletion("https://hook", [], 0)
        mock_send.assert_not_called()
