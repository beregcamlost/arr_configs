"""Tests for translation config."""

from translation.config import Config, load_config, DEEPL_LANG_MAP


def test_load_config_from_env(env_config):
    """Config loads all required values from environment."""
    cfg = load_config()
    assert cfg.deepl_api_key == "test-deepl-key:fx"
    assert cfg.bazarr_api_key == "test-bazarr-key"
    assert cfg.discord_webhook_url == "https://discord.com/api/webhooks/test"
    assert cfg.bazarr_url == "http://127.0.0.1:6767/bazarr"
    assert cfg.bazarr_db == "/opt/bazarr/data/db/bazarr.db"


def test_load_config_missing_deepl_key():
    """Config raises ValueError when DEEPL_API_KEY is missing."""
    import pytest
    with pytest.raises(ValueError, match="DEEPL_API_KEY"):
        load_config()


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
    from translation.config import DEEPL_SOURCE_LANG_MAP
    assert DEEPL_SOURCE_LANG_MAP["en"] == "EN"
    assert DEEPL_SOURCE_LANG_MAP["es"] == "ES"
    assert DEEPL_SOURCE_LANG_MAP["pt"] == "PT"
    assert DEEPL_SOURCE_LANG_MAP["zh"] == "ZH"
