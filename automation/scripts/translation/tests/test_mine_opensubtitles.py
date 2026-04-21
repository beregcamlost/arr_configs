"""Tests for mine_opensubtitles — streaming, filtering, dedup, CLI."""

import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import translation.mine_opensubtitles as mo
from translation.mine_opensubtitles import (
    EN_FILENAME,
    ES_FILENAME,
    INSTRUCTION,
    _stream_pairs,
    cli,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_opus_zip(tmp_path: Path, en_lines: list[str], es_lines: list[str]) -> Path:
    """Create a minimal OPUS-style zip with parallel en/es files."""
    zip_path = tmp_path / "test_opensubs.zip"
    en_content = "\n".join(en_lines).encode("utf-8")
    es_content = "\n".join(es_lines).encode("utf-8")
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr(EN_FILENAME, en_content)
        zf.writestr(ES_FILENAME, es_content)
    return zip_path


def _make_cached_zip(tmp_path: Path, en_lines: list[str], es_lines: list[str]) -> tuple[Path, Path]:
    """Create a zip already named as the expected cache file. Returns (zip_path, cache_dir)."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    zip_path = cache_dir / mo.CACHE_FILENAME
    en_content = "\n".join(en_lines).encode("utf-8")
    es_content = "\n".join(es_lines).encode("utf-8")
    with zipfile.ZipFile(str(zip_path), "w") as zf:
        zf.writestr(EN_FILENAME, en_content)
        zf.writestr(ES_FILENAME, es_content)
    return zip_path, cache_dir


# ---------------------------------------------------------------------------
# _stream_pairs
# ---------------------------------------------------------------------------

class TestStreamPairs:
    def test_basic_streaming(self, tmp_path):
        en_lines = ["Hello world.", "How are you?", "I am fine."]
        es_lines = ["Hola mundo.", "¿Cómo estás?", "Estoy bien."]
        zip_path = _make_opus_zip(tmp_path, en_lines, es_lines)
        pairs = list(_stream_pairs(zip_path))
        assert len(pairs) == 3
        assert pairs[0] == ("Hello world.", "Hola mundo.")
        assert pairs[1] == ("How are you?", "¿Cómo estás?")

    def test_strips_newlines(self, tmp_path):
        en_lines = ["Hello\n", "World\n"]
        es_lines = ["Hola\n", "Mundo\n"]
        zip_path = _make_opus_zip(tmp_path, en_lines, es_lines)
        pairs = list(_stream_pairs(zip_path))
        for en, es in pairs:
            assert "\n" not in en
            assert "\n" not in es

    def test_missing_files_in_zip_yields_nothing(self, tmp_path):
        zip_path = tmp_path / "empty.zip"
        with zipfile.ZipFile(str(zip_path), "w") as zf:
            zf.writestr("other.txt", "nothing here")
        pairs = list(_stream_pairs(zip_path))
        assert pairs == []

    def test_unicode_preserved(self, tmp_path):
        en_lines = ["She smiled at him gently."]
        es_lines = ["Ella le sonrió con ternura."]
        zip_path = _make_opus_zip(tmp_path, en_lines, es_lines)
        pairs = list(_stream_pairs(zip_path))
        assert pairs[0][1] == "Ella le sonrió con ternura."


# ---------------------------------------------------------------------------
# CLI integration — zip is pre-placed as cached file to avoid network calls
# ---------------------------------------------------------------------------

class TestCli:
    def _make_valid_pairs(self, n: int) -> tuple[list[str], list[str]]:
        en_lines = [
            f"This is English sentence number {i} long enough for filter here."
            for i in range(n)
        ]
        es_lines = [
            f"Esta es la oración española número {i} suficientemente larga aquí."
            for i in range(n)
        ]
        return en_lines, es_lines

    def test_basic_output(self, tmp_path):
        en_lines, es_lines = self._make_valid_pairs(20)
        _, cache_dir = _make_cached_zip(tmp_path, en_lines, es_lines)
        out_path = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--cache-dir", str(cache_dir),
            "--target-pairs", "100",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_path.read_text().strip().splitlines()
        assert len(lines) > 0
        obj = json.loads(lines[0])
        assert obj["source"] == "opensubtitles"
        assert obj["instruction"] == INSTRUCTION
        assert "input" in obj
        assert "output" in obj

    def test_output_capped_at_target(self, tmp_path):
        """When target < available pairs, output is capped."""
        en_lines, es_lines = self._make_valid_pairs(50)
        _, cache_dir = _make_cached_zip(tmp_path, en_lines, es_lines)
        out_path = tmp_path / "out_capped.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--cache-dir", str(cache_dir),
            "--target-pairs", "10",
            "--seed", "42",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_path.read_text().strip().splitlines()
        assert len(lines) == 10

    def test_invalid_pairs_filtered(self, tmp_path):
        """URL/HTML pairs are filtered out."""
        en_lines = ["http://example.com/stuff here", "<b>bold</b>"]
        es_lines = ["http://ejemplo.com/cosas aquí", "<b>negrita</b>"]
        _, cache_dir = _make_cached_zip(tmp_path, en_lines, es_lines)
        out_path = tmp_path / "out_filtered.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--cache-dir", str(cache_dir),
            "--target-pairs", "1000",
        ])
        assert result.exit_code == 0, result.output
        # Output file should be empty (all filtered)
        if out_path.exists():
            content = out_path.read_text().strip()
            if content:
                for line in content.splitlines():
                    obj = json.loads(line)
                    assert "http" not in obj["input"]
                    assert "<" not in obj["input"]

    def test_deduplication(self, tmp_path):
        """Duplicate pairs appear only once in output."""
        en_lines = ["This sentence appears multiple times here long enough."] * 10
        es_lines = ["Esta oración aparece múltiples veces aquí suficientemente."] * 10
        _, cache_dir = _make_cached_zip(tmp_path, en_lines, es_lines)
        out_path = tmp_path / "out_dedup.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--cache-dir", str(cache_dir),
            "--target-pairs", "1000",
        ])
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out_path.read_text().strip().splitlines()
        # Should be deduplicated to 1
        assert len(lines) == 1

    def test_all_output_valid_json(self, tmp_path):
        en_lines, es_lines = self._make_valid_pairs(30)
        _, cache_dir = _make_cached_zip(tmp_path, en_lines, es_lines)
        out_path = tmp_path / "out_valid.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, [
            "--output", str(out_path),
            "--cache-dir", str(cache_dir),
            "--target-pairs", "100",
        ])
        assert result.exit_code == 0
        for line in out_path.read_text().strip().splitlines():
            obj = json.loads(line)
            assert {"instruction", "input", "output", "source"} <= obj.keys()
