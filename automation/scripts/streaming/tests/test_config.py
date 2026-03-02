"""Tests for config module."""

import pytest

from streaming.config import Config, PROVIDER_MAP, load_config


class TestLoadConfig:
    def test_loads_from_env(self, env_config):
        cfg = load_config()
        assert cfg.tmdb_api_key == "test-tmdb-key"
        assert cfg.radarr_key == "test-radarr-key"
        assert cfg.sonarr_key == "test-sonarr-key"
        assert cfg.emby_api_key == "test-emby-key"

    def test_defaults(self, env_config):
        cfg = load_config()
        assert cfg.country == "CL"
        assert cfg.providers == ["netflix", "disney"]
        assert cfg.dry_run is False
        assert cfg.verbose is False
        assert cfg.radarr_url == "http://127.0.0.1:7878/radarr"
        assert cfg.sonarr_url == "http://127.0.0.1:8989/sonarr"

    def test_cli_overrides(self, env_config):
        cfg = load_config(country="US", providers="netflix,hbo", dry_run=True, verbose=True)
        assert cfg.country == "US"
        assert cfg.providers == ["netflix", "hbo"]
        assert cfg.dry_run is True
        assert cfg.verbose is True

    def test_missing_tmdb_key_raises(self, monkeypatch):
        monkeypatch.delenv("TMDB_API_KEY", raising=False)
        monkeypatch.setenv("RADARR_KEY", "x")
        monkeypatch.setenv("SONARR_KEY", "x")
        with pytest.raises(ValueError, match="TMDB_API_KEY"):
            load_config()

    def test_missing_radarr_key_raises(self, monkeypatch):
        monkeypatch.setenv("TMDB_API_KEY", "x")
        monkeypatch.delenv("RADARR_KEY", raising=False)
        monkeypatch.setenv("SONARR_KEY", "x")
        with pytest.raises(ValueError, match="RADARR_KEY"):
            load_config()

    def test_unknown_provider_raises(self, env_config):
        with pytest.raises(ValueError, match="Unknown provider 'hulu'"):
            load_config(providers="netflix,hulu")

    def test_provider_ids(self, env_config):
        cfg = load_config()
        assert cfg.provider_ids == [8, 337]

    def test_custom_db_path(self, env_config, tmp_path):
        db = str(tmp_path / "custom.db")
        cfg = load_config(db_path=db)
        assert cfg.db_path == db

    def test_rapidapi_key_from_env(self, env_config, monkeypatch):
        monkeypatch.setenv("RAPIDAPI_KEY", "test-rapid-key")
        cfg = load_config()
        assert cfg.rapidapi_key == "test-rapid-key"

    def test_rapidapi_key_default_empty(self, env_config):
        cfg = load_config()
        assert cfg.rapidapi_key == ""
