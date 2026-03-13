"""Tests for translation config."""

import pytest
from translation.config import (
    Config, load_config,
    DEEPL_LANG_MAP, DEEPL_SOURCE_LANG_MAP,
    GOOGLE_LANG_MAP,
    PROVIDER_DEEPL, PROVIDER_GOOGLE,
)


def test_load_config_from_env(env_config):
    """Config loads all required values from environment."""
    cfg = load_config()
    assert cfg.deepl_api_key == "test-deepl-key:fx"
    assert cfg.bazarr_api_key == "test-bazarr-key"
    assert cfg.discord_webhook_url == "https://discord.com/api/webhooks/test"
    assert cfg.bazarr_url == "http://127.0.0.1:6767/bazarr"
    assert cfg.bazarr_db == "/opt/bazarr/data/db/bazarr.db"
    assert cfg.google_translate_enabled is True


def test_load_config_no_deepl_key_google_enabled(monkeypatch):
    """Config loads fine without DEEPL_API_KEY when Google is enabled."""
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_TRANSLATE_ENABLED", "1")
    cfg = load_config()
    assert cfg.deepl_api_key == ""
    assert cfg.google_translate_enabled is True


def test_load_config_no_providers_raises(monkeypatch):
    """Config raises ValueError when no translation provider available."""
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_TRANSLATE_ENABLED", "0")
    with pytest.raises(ValueError, match="DEEPL_API_KEY"):
        load_config()


def test_load_config_google_disabled(env_config, monkeypatch):
    """Config respects GOOGLE_TRANSLATE_ENABLED=0."""
    monkeypatch.setenv("GOOGLE_TRANSLATE_ENABLED", "0")
    cfg = load_config()
    assert cfg.google_translate_enabled is False
    assert cfg.deepl_api_key == "test-deepl-key:fx"


def test_load_config_cli_overrides(env_config):
    """CLI overrides take precedence over env vars."""
    cfg = load_config(bazarr_db="/custom/bazarr.db", state_dir="/custom/state")
    assert cfg.bazarr_db == "/custom/bazarr.db"
    assert cfg.state_dir == "/custom/state"


def test_deepl_lang_map_basics():
    """DeepL language mapping covers common Bazarr codes."""
    assert DEEPL_LANG_MAP["en"] == "EN-US"
    assert DEEPL_LANG_MAP["es"] == "ES"
    assert DEEPL_LANG_MAP["fr"] == "FR"
    assert DEEPL_LANG_MAP["it"] == "IT"
    assert DEEPL_LANG_MAP["pt"] == "PT-BR"
    assert DEEPL_LANG_MAP["zh"] == "ZH-HANS"
    assert DEEPL_LANG_MAP["zt"] == "ZH-HANT"


def test_deepl_source_lang_map():
    """Source language mapping uses base codes (no region)."""
    assert DEEPL_SOURCE_LANG_MAP["en"] == "EN"
    assert DEEPL_SOURCE_LANG_MAP["es"] == "ES"
    assert DEEPL_SOURCE_LANG_MAP["pt"] == "PT"
    assert DEEPL_SOURCE_LANG_MAP["zh"] == "ZH"


def test_google_lang_map_basics():
    """Google language mapping covers common Bazarr codes."""
    assert GOOGLE_LANG_MAP["en"] == "en"
    assert GOOGLE_LANG_MAP["es"] == "es"
    assert GOOGLE_LANG_MAP["zh"] == "zh-cn"
    assert GOOGLE_LANG_MAP["zt"] == "zh-tw"
    assert GOOGLE_LANG_MAP["nb"] == "no"  # Norwegian Bokmal -> no


def test_google_lang_map_extra_languages():
    """Google supports languages that DeepL doesn't."""
    assert "hi" in GOOGLE_LANG_MAP
    assert "th" in GOOGLE_LANG_MAP
    assert "vi" in GOOGLE_LANG_MAP
    assert "hi" not in DEEPL_LANG_MAP


def test_provider_constants():
    """Provider constants are defined."""
    assert PROVIDER_DEEPL == "deepl"
    assert PROVIDER_GOOGLE == "google"
