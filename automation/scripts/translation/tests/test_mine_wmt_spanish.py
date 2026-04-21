"""Tests for mine_wmt_spanish — filter, dedup, CLI with mocked data sources."""

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from translation.mine_wmt_spanish import (
    INSTRUCTION,
    _stream_opus_zip,
    cli,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_opus_zip(tmp_path: Path, en_lines: list[str], es_lines: list[str],
                   name: str = "test") -> Path:
    zip_path = tmp_path / f"{name}.zip"
    en_content = "\n".join(en_lines).encode("utf-8")
    es_content = "\n".join(es_lines).encode("utf-8")
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr("parallel.en", en_content)
        zf.writestr("parallel.es", es_content)
    return zip_path


# ---------------------------------------------------------------------------
# _stream_opus_zip
# ---------------------------------------------------------------------------

class TestStreamOpusZip:
    def test_basic_streaming(self, tmp_path):
        en = ["Hello there.", "How are you?"]
        es = ["Hola.", "¿Cómo estás?"]
        zp = _make_opus_zip(tmp_path, en, es)
        pairs = list(_stream_opus_zip(zp))
        assert pairs[0] == ("Hello there.", "Hola.")
        assert pairs[1] == ("How are you?", "¿Cómo estás?")

    def test_missing_en_es_files(self, tmp_path):
        zp = tmp_path / "bad.zip"
        with zipfile.ZipFile(str(zp), "w") as zf:
            zf.writestr("other.txt", "nothing")
        pairs = list(_stream_opus_zip(zp))
        assert pairs == []

    def test_strips_newlines(self, tmp_path):
        en = ["Hello world\n"]
        es = ["Hola mundo\n"]
        zp = _make_opus_zip(tmp_path, en, es)
        pairs = list(_stream_opus_zip(zp))
        assert "\n" not in pairs[0][0]
        assert "\n" not in pairs[0][1]


# ---------------------------------------------------------------------------
# CLI — using OPUS fallback (mocked downloads)
# ---------------------------------------------------------------------------

class TestCliOpusFallback:
    def _prepare_zips(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create two small valid OPUS zips for europarl + news."""
        en_lines = [
            f"This is a European parliament sentence number {i} in English here today."
            for i in range(100)
        ]
        es_lines = [
            f"Esta es la oración del parlamento europeo número {i} en español hoy aquí."
            for i in range(100)
        ]
        europarl_zip = _make_opus_zip(tmp_path, en_lines, es_lines, "opus_europarl_en-es")
        news_lines_en = [
            f"This is news commentary sentence number {i} in English for testing."
            for i in range(50)
        ]
        news_lines_es = [
            f"Este es el comentario de noticias número {i} en español para prueba."
            for i in range(50)
        ]
        news_zip = _make_opus_zip(tmp_path, news_lines_en, news_lines_es, "opus_news_commentary_en-es")
        return europarl_zip, news_zip

    def test_basic_run(self, tmp_path):
        europarl_zip, news_zip = self._prepare_zips(tmp_path)
        out_path = tmp_path / "out.jsonl"

        # Patch _download_file to not actually download, just return True
        with patch("translation.mine_wmt_spanish._download_file", return_value=True):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", str(out_path),
                "--cache-dir", str(tmp_path),
                "--target-pairs", "200",
                "--seed", "42",
            ])

        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_path.read_text().strip().splitlines()
        assert len(lines) > 0
        obj = json.loads(lines[0])
        assert obj["source"] == "wmt"
        assert obj["instruction"] == INSTRUCTION

    def test_output_capped_at_target(self, tmp_path):
        europarl_zip, news_zip = self._prepare_zips(tmp_path)
        out_path = tmp_path / "out_capped.jsonl"

        with patch("translation.mine_wmt_spanish._download_file", return_value=True):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", str(out_path),
                "--cache-dir", str(tmp_path),
                "--target-pairs", "5",
                "--seed", "42",
            ])

        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_path.read_text().strip().splitlines()
        assert len(lines) == 5

    def test_all_output_valid_json(self, tmp_path):
        europarl_zip, news_zip = self._prepare_zips(tmp_path)
        out_path = tmp_path / "out_valid.jsonl"

        with patch("translation.mine_wmt_spanish._download_file", return_value=True):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", str(out_path),
                "--cache-dir", str(tmp_path),
                "--target-pairs", "50",
            ])

        assert result.exit_code == 0
        for line in out_path.read_text().strip().splitlines():
            obj = json.loads(line)
            assert {"instruction", "input", "output", "source"} <= obj.keys()

    def test_failed_download_skips_source(self, tmp_path):
        """When download fails, that source is skipped gracefully."""
        out_path = tmp_path / "out_skip.jsonl"
        cache_dir = tmp_path / "empty_cache"
        cache_dir.mkdir()

        with patch("translation.mine_wmt_spanish._download_file", return_value=False):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "--output", str(out_path),
                "--cache-dir", str(cache_dir),
                "--target-pairs", "100",
            ])

        # Should exit cleanly (0 records written, no crash)
        assert result.exit_code == 0, result.output + str(result.exception)
