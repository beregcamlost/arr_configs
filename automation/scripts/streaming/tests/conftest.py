"""Shared test fixtures for streaming checker tests."""

import os
import sys

import pytest

# Ensure automation/scripts is on the path so `streaming` package is importable
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir),
)

from streaming.db import init_db


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    db_path = str(tmp_path / "test_state.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def env_config(monkeypatch):
    """Set up environment variables for Config loading."""
    monkeypatch.setenv("TMDB_API_KEY", "test-tmdb-key")
    monkeypatch.setenv("RADARR_KEY", "test-radarr-key")
    monkeypatch.setenv("SONARR_KEY", "test-sonarr-key")
    monkeypatch.setenv("EMBY_API_KEY", "test-emby-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
