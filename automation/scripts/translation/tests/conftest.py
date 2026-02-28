"""Shared test fixtures for translation tests."""

import os
import sys

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir),
)

from translation.db import init_db


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    db_path = str(tmp_path / "test_translation.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def env_config(monkeypatch):
    """Set up environment variables for Config loading."""
    monkeypatch.setenv("DEEPL_API_KEY", "test-deepl-key:fx")
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
