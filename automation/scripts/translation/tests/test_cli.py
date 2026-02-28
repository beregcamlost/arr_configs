"""Tests for translator CLI."""

import os
from unittest.mock import patch, MagicMock
from click.testing import CliRunner
from translation.translator import cli


@patch("translation.translator.create_translator")
@patch("translation.translator.scan_recent_missing")
def test_translate_since_no_results(mock_scan, mock_create, env_config, tmp_db, monkeypatch):
    """translate --since with no missing subs does nothing."""
    mock_scan.return_value = []
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    runner = CliRunner()
    result = runner.invoke(cli, ["translate", "--since", "60", "--state-dir", os.path.dirname(tmp_db)])
    assert result.exit_code == 0
    assert "no files" in result.output.lower() or "0" in result.output


@patch("translation.translator.translate_file")
def test_translate_file_mode(mock_translate_file, env_config, tmp_path, monkeypatch):
    """translate --file calls translate_file for a single path."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
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


@patch("translation.translator.create_translator")
def test_usage_command(mock_create, env_config, monkeypatch):
    """usage command queries DeepL API."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    mock_translator = MagicMock()
    mock_usage = MagicMock()
    mock_usage.character = MagicMock(count=50000, limit=500000)
    mock_translator.get_usage.return_value = mock_usage
    mock_create.return_value = mock_translator
    runner = CliRunner()
    result = runner.invoke(cli, ["usage"])
    assert result.exit_code == 0


@patch("translation.translator.create_translator")
@patch("translation.translator.scan_recent_missing")
@patch("translation.translator.translate_file")
def test_translate_max_chars_stops_batch(mock_tf, mock_scan, mock_create, env_config, tmp_db, monkeypatch):
    """--max-chars stops processing after budget is exhausted."""
    monkeypatch.setenv("DEEPL_API_KEY", "test:fx")
    mock_scan.return_value = [
        {"path": "/path/a.mkv"},
        {"path": "/path/b.mkv"},
        {"path": "/path/c.mkv"},
    ]
    # First file uses 8000 chars, second would exceed 10000 budget
    mock_tf.side_effect = [
        ([{"file": "a.mkv", "target": "es", "chars": 8000}], []),
        ([{"file": "b.mkv", "target": "es", "chars": 5000}], []),
        ([{"file": "c.mkv", "target": "es", "chars": 3000}], []),
    ]
    runner = CliRunner()
    result = runner.invoke(cli, [
        "translate", "--since", "60", "--max-chars", "10000",
        "--state-dir", os.path.dirname(tmp_db),
    ])
    assert result.exit_code == 0
    # Should have processed at most 2 files (8000 + 5000 > 10000, but
    # translate_file is called before we check; the budget check happens
    # between files in the loop)
    assert mock_tf.call_count <= 2
