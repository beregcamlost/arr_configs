"""Tests for Discord notifications."""

from unittest.mock import patch, MagicMock
from translation.discord import notify_translations, notify_quota_warning


@patch("translation.discord.requests.post")
def test_notify_translations(mock_post):
    """notify_translations sends embed with summary."""
    mock_post.return_value = MagicMock(status_code=204)
    notify_translations(
        webhook_url="https://discord.com/api/webhooks/test",
        translated=[
            {"file": "Movie.mkv", "target": "es", "chars": 1500},
            {"file": "Show.S01E01.mkv", "target": "fr", "chars": 2000},
        ],
        failed=[],
        total_chars=3500,
        monthly_chars=45000,
    )
    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert len(payload["embeds"]) == 1


@patch("translation.discord.requests.post")
def test_notify_translations_skips_empty(mock_post):
    """notify_translations does nothing when no translations."""
    notify_translations("https://hook", [], [], 0, 0)
    mock_post.assert_not_called()


@patch("translation.discord.requests.post")
def test_notify_quota_warning(mock_post):
    """notify_quota_warning sends red embed."""
    mock_post.return_value = MagicMock(status_code=204)
    notify_quota_warning("https://hook", 490000, 500000)
    mock_post.assert_called_once()
    payload = mock_post.call_args[1]["json"]
    assert payload["embeds"][0]["color"] == 15158332  # RED


def test_notify_no_webhook():
    """Functions handle empty webhook gracefully."""
    # Should not raise
    notify_translations("", [{"file": "x", "target": "es", "chars": 1}], [], 1, 1)
    notify_quota_warning("", 100, 500000)
