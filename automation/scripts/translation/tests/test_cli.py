"""Tests for translator CLI."""

import os
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from translation.translator import cli
import translation.translator as translator_mod


@patch("translation.translator.deepl_get_usage")
@patch("translation.translator.scan_recent_missing")
def test_translate_since_no_results(mock_scan, mock_get_usage, env_config, tmp_db, monkeypatch):
    """translate --since with no missing subs does nothing."""
    mock_scan.return_value = []
    mock_get_usage.return_value = {"character_count": 0, "character_limit": 500000}
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    runner = CliRunner()
    result = runner.invoke(cli, ["translate", "--since", "60", "--state-dir", os.path.dirname(tmp_db)])
    assert result.exit_code == 0
    assert "no files" in result.output.lower() or "0" in result.output


@patch("translation.translator.translate_file")
@patch("translation.translator.deepl_get_usage")
def test_translate_file_mode(mock_get_usage, mock_translate_file, env_config, tmp_path, monkeypatch):
    """translate --file calls translate_file for a single path."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    mock_get_usage.return_value = {"character_count": 0, "character_limit": 500000}
    mock_translate_file.return_value = ([], [])
    video = tmp_path / "Movie.mkv"
    video.write_bytes(b"\x00" * 100)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "translate", "--file", str(video),
        "--state-dir", str(tmp_path / "state"),
    ])
    assert result.exit_code == 0


def test_status_command(env_config, tmp_db, monkeypatch):
    """status command shows monthly usage."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--state-dir", os.path.dirname(tmp_db)])
    assert result.exit_code == 0
    assert "chars" in result.output.lower() or "usage" in result.output.lower() or "0" in result.output


@patch("translation.deepl_client._get_translator")
def test_usage_command(mock_get_translator, env_config, monkeypatch):
    """usage command queries DeepL API."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    mock_translator = MagicMock()
    mock_usage = MagicMock()
    mock_usage.character = MagicMock(count=50000, limit=500000)
    mock_translator.get_usage.return_value = mock_usage
    mock_get_translator.return_value = mock_translator
    runner = CliRunner()
    result = runner.invoke(cli, ["usage"])
    assert result.exit_code == 0


def test_usage_command_no_deepl_key(monkeypatch):
    """usage command without DeepL key shows message."""
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPL_API_KEYS", raising=False)
    monkeypatch.setenv("GOOGLE_TRANSLATE_ENABLED", "1")
    runner = CliRunner()
    result = runner.invoke(cli, ["usage"])
    assert result.exit_code == 0
    assert "no deepl" in result.output.lower()


@patch("translation.translator.deepl_get_usage")
@patch("translation.translator.scan_recent_missing")
@patch("translation.translator.translate_file")
def test_translate_max_chars_stops_batch(mock_tf, mock_scan, mock_get_usage, env_config, tmp_db, monkeypatch):
    """--max-chars stops processing after budget is exhausted."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    mock_get_usage.return_value = {"character_count": 0, "character_limit": 500000}
    mock_scan.return_value = [
        {"path": "/path/a.mkv"},
        {"path": "/path/b.mkv"},
        {"path": "/path/c.mkv"},
    ]
    mock_tf.side_effect = [
        ([{"file": "a.mkv", "target": "es", "chars": 8000, "provider": "deepl"}], []),
        ([{"file": "b.mkv", "target": "es", "chars": 5000, "provider": "deepl"}], []),
        ([{"file": "c.mkv", "target": "es", "chars": 3000, "provider": "deepl"}], []),
    ]
    runner = CliRunner()
    result = runner.invoke(cli, [
        "translate", "--since", "60", "--max-chars", "10000",
        "--state-dir", os.path.dirname(tmp_db),
    ])
    assert result.exit_code == 0
    assert mock_tf.call_count <= 2


@patch("translation.translator.scan_recent_missing")
@patch("translation.google_client.create_translator")
def test_translate_google_only_mode(mock_create_google, mock_scan, tmp_db, monkeypatch):
    """translate works with Google-only mode (no DeepL key)."""
    translator_mod._deepl_quota_exceeded = False
    monkeypatch.delenv("DEEPL_API_KEY", raising=False)
    monkeypatch.delenv("DEEPL_API_KEYS", raising=False)
    monkeypatch.setenv("GOOGLE_TRANSLATE_ENABLED", "1")
    monkeypatch.setenv("BAZARR_API_KEY", "test")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "")
    mock_create_google.return_value = MagicMock()
    mock_scan.return_value = []
    runner = CliRunner()
    result = runner.invoke(cli, [
        "translate", "--since", "60",
        "--state-dir", os.path.dirname(tmp_db),
    ])
    assert result.exit_code == 0
    assert "no files" in result.output.lower() or "google" in result.output.lower() or "0" in result.output


def test_status_shows_provider_breakdown(tmp_db, env_config, monkeypatch):
    """status command shows provider breakdown."""
    from translation.db import record_translation
    record_translation(tmp_db, "/path/v1.mkv", "en", "es", 1000, "success", "deepl")
    record_translation(tmp_db, "/path/v2.mkv", "en", "fr", 2000, "success", "google")
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    runner = CliRunner()
    result = runner.invoke(cli, ["status", "--state-dir", os.path.dirname(tmp_db)])
    assert result.exit_code == 0
    assert "deepl" in result.output.lower()
    assert "google" in result.output.lower()
