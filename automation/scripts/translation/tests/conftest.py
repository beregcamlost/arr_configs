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
    """Create a temporary SQLite database for testing.

    Uses 'translation_state.db' name to match translator._db_path().
    """
    db_path = str(tmp_path / "translation_state.db")
    init_db(db_path)
    return db_path


@pytest.fixture
def env_config(monkeypatch):
    """Set up environment variables for Config loading (DeepL available)."""
    monkeypatch.setenv("DEEPL_API_KEY", "test-deepl-key:fx")
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")


@pytest.fixture
def env_config_google_only(monkeypatch):
    """Set up environment variables for Google-only mode (no DeepL key)."""
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)
    monkeypatch.setenv("GOOGLE_TRANSLATE_ENABLED", "1")
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")


@pytest.fixture
def env_config_gemini(monkeypatch):
    """Set up environment variables with Gemini keys."""
    monkeypatch.setenv("DEEPL_API_KEY", "test-deepl-key:fx")
    monkeypatch.setenv("GEMINI_API_KEY_1", "test-gemini-key-1")
    monkeypatch.setenv("GEMINI_API_KEY_2", "test-gemini-key-2")
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
