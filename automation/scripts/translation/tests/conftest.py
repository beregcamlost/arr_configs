"""Shared test fixtures for translation tests."""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir),
)

from translation.db import init_db


def mock_ollama_response(response_text):
    """Create a mock for urllib.request.urlopen that returns Ollama chat response."""
    mock_resp = MagicMock()
    body = json.dumps({
        "message": {"role": "assistant", "content": response_text}
    }).encode("utf-8")
    mock_resp.read.return_value = body
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


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
    monkeypatch.delenv("DEEPL_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")


@pytest.fixture
def env_config_google_only(monkeypatch):
    """Set up environment variables for Google-only mode (no DeepL key)."""
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPL_API_KEYS", raising=False)
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("GOOGLE_TRANSLATE_ENABLED", "1")
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")


@pytest.fixture
def env_config_gemini(monkeypatch):
    """Set up environment variables with Gemini keys."""
    monkeypatch.setenv("DEEPL_API_KEY", "test-deepl-key:fx")
    monkeypatch.delenv("DEEPL_API_KEYS", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEYS", "test-gemini-key-1,test-gemini-key-2")
    monkeypatch.setenv("BAZARR_API_KEY", "test-bazarr-key")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")
