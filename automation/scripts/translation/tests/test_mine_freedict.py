"""Tests for mine_freedict — dictd parser, TEI parser, dedup, and CLI."""

import gzip
import json
import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from translation.mine_freedict import (
    _format_record,
    _parse_dictd,
    _parse_tei_spa_eng,
    cli,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_dictd(path: Path, body: str) -> None:
    """Write a minimal .dict.dz file with the given body (gzip-compressed)."""
    # Prepend a fake header that the parser skips
    header = (
        "00-database-dictfmt-1.0\n"
        "English-Spanish FreeDict Dictionary\n"
        "\n"
        "Edition: test\n"
        "\n"
        "English-Spanish FreeDict Dictionary ver. 0.0.1\n"
        "http://freedict.org/\n"
    )
    content = (header + body).encode("utf-8")
    with gzip.open(str(path), "wb") as fh:
        fh.write(content)


def _write_tei(path: Path, entries_xml: str) -> None:
    """Write a minimal spa-eng TEI file."""
    content = textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <TEI xmlns="http://www.tei-c.org/ns/1.0">
          <teiHeader><fileDesc><titleStmt>
            <title>Spanish-English FreeDict Dictionary</title>
          </titleStmt></fileDesc></teiHeader>
          <text><body>
            {entries_xml}
          </body></text>
        </TEI>
    """)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# _parse_dictd
# ---------------------------------------------------------------------------

class TestParseDictd:
    def test_simple_entry(self, tmp_path):
        p = tmp_path / "test.dict.dz"
        # All headwords MUST have /phonetics/ — this is how the parser detects them
        _write_dictd(p, "hello /hɛloʊ/\nhola\n")
        pairs = list(_parse_dictd(p))
        assert ("hello", "hola") in pairs

    def test_entry_with_phonetics_stripped(self, tmp_path):
        p = tmp_path / "test.dict.dz"
        _write_dictd(p, "cat /kæt/\ngato\n")
        pairs = list(_parse_dictd(p))
        assert ("cat", "gato") in pairs

    def test_comma_separated_translations_expanded(self, tmp_path):
        p = tmp_path / "test.dict.dz"
        _write_dictd(p, "street /striːt/\ncalle, avenida\n")
        pairs = list(_parse_dictd(p))
        assert ("street", "calle") in pairs
        assert ("street", "avenida") in pairs

    def test_numbered_translations_expanded(self, tmp_path):
        p = tmp_path / "test.dict.dz"
        _write_dictd(p, "bank /bæŋk/\n1. banco\n2. orilla\n")
        pairs = list(_parse_dictd(p))
        results = {es for _, es in pairs}
        assert "banco" in results
        assert "orilla" in results

    def test_multiple_headwords(self, tmp_path):
        p = tmp_path / "test.dict.dz"
        _write_dictd(p, "cat /kæt/\ngato\ndog /dɔg/\nperro\n")
        pairs = list(_parse_dictd(p))
        words = {en for en, _ in pairs}
        assert "cat" in words
        assert "dog" in words

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nonexistent.dict.dz"
        pairs = list(_parse_dictd(p))
        assert pairs == []

    def test_empty_translations_skipped(self, tmp_path):
        p = tmp_path / "test.dict.dz"
        _write_dictd(p, "thing /θɪŋ/\n,\n")
        pairs = list(_parse_dictd(p))
        # Pairs with empty strings should not appear
        for en, es in pairs:
            assert en.strip() and es.strip()


# ---------------------------------------------------------------------------
# _parse_tei_spa_eng
# ---------------------------------------------------------------------------

class TestParseTeiSpaEng:
    def test_basic_reversal(self, tmp_path):
        p = tmp_path / "spa-eng.tei"
        _write_tei(p, """
            <entry>
              <form><orth>gato</orth></form>
              <sense><cit type="trans"><quote>cat</quote></cit></sense>
            </entry>
        """)
        pairs = list(_parse_tei_spa_eng(p))
        assert ("cat", "gato") in pairs

    def test_multiple_translations_per_entry(self, tmp_path):
        p = tmp_path / "spa-eng.tei"
        _write_tei(p, """
            <entry>
              <form><orth>banco</orth></form>
              <sense>
                <cit type="trans"><quote>bank</quote></cit>
                <cit type="trans"><quote>bench</quote></cit>
              </sense>
            </entry>
        """)
        pairs = list(_parse_tei_spa_eng(p))
        assert ("bank", "banco") in pairs
        assert ("bench", "banco") in pairs

    def test_missing_orth_skipped(self, tmp_path):
        p = tmp_path / "spa-eng.tei"
        _write_tei(p, """
            <entry>
              <form></form>
              <sense><cit type="trans"><quote>something</quote></cit></sense>
            </entry>
        """)
        pairs = list(_parse_tei_spa_eng(p))
        assert pairs == []

    def test_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nonexistent.tei"
        pairs = list(_parse_tei_spa_eng(p))
        assert pairs == []

    def test_invalid_xml_returns_empty(self, tmp_path):
        p = tmp_path / "bad.tei"
        p.write_text("<not valid xml<<", encoding="utf-8")
        pairs = list(_parse_tei_spa_eng(p))
        assert pairs == []


# ---------------------------------------------------------------------------
# _format_record
# ---------------------------------------------------------------------------

class TestFormatRecord:
    def test_valid_json(self):
        line = _format_record("hello", "hola")
        obj = json.loads(line)
        assert obj["input"] == "hello"
        assert obj["output"] == "hola"
        assert obj["source"] == "freedict"
        assert "instruction" in obj

    def test_unicode_preserved(self):
        line = _format_record("schedule", "programación")
        obj = json.loads(line)
        assert obj["output"] == "programación"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class TestCli:
    def test_cli_with_mock_dictd(self, tmp_path, monkeypatch):
        """CLI reads from dictd file and writes JSONL output."""
        dictd_path = tmp_path / "eng-spa.dict.dz"
        _write_dictd(dictd_path, "hello /hɛloʊ/\nhola\ncat /kæt/\ngato\n")

        tei_path = tmp_path / "spa-eng.tei"
        _write_tei(tei_path, """
            <entry>
              <form><orth>perro</orth></form>
              <sense><cit type="trans"><quote>dog</quote></cit></sense>
            </entry>
        """)

        # Patch the module-level constants
        import translation.mine_freedict as mf
        monkeypatch.setattr(mf, "DICTD_ENG_SPA", dictd_path)
        monkeypatch.setattr(mf, "TEI_SPA_ENG_CACHE", tei_path)

        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, ["--output", str(out_file)])
        assert result.exit_code == 0, result.output + str(getattr(result, "exception", ""))
        lines = out_file.read_text().strip().splitlines()
        assert len(lines) >= 2
        obj = json.loads(lines[0])
        assert obj["source"] == "freedict"

    def test_cli_deduplicates(self, tmp_path, monkeypatch):
        """Duplicate pairs (case-insensitive) are emitted only once."""
        dictd_path = tmp_path / "eng-spa.dict.dz"
        # "hello" → "hola" twice (once from dictd, once from reversed TEI)
        _write_dictd(dictd_path, "hello /hɛloʊ/\nhola\n")

        tei_path = tmp_path / "spa-eng.tei"
        _write_tei(tei_path, """
            <entry>
              <form><orth>hola</orth></form>
              <sense><cit type="trans"><quote>hello</quote></cit></sense>
            </entry>
        """)

        import translation.mine_freedict as mf
        monkeypatch.setattr(mf, "DICTD_ENG_SPA", dictd_path)
        monkeypatch.setattr(mf, "TEI_SPA_ENG_CACHE", tei_path)

        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, ["--output", str(out_file)])
        assert result.exit_code == 0, result.output
        lines = out_file.read_text().strip().splitlines()
        entries = [json.loads(l) for l in lines]
        hello_hola = [e for e in entries if e["input"].lower() == "hello" and e["output"].lower() == "hola"]
        assert len(hello_hola) == 1

    def test_cli_min_length_filter(self, tmp_path, monkeypatch):
        """Pairs below min_length are excluded."""
        dictd_path = tmp_path / "eng-spa.dict.dz"
        _write_dictd(dictd_path, "a /eɪ/\nb\nhello /hɛloʊ/\nhola\n")

        tei_path = tmp_path / "empty.tei"
        _write_tei(tei_path, "")

        import translation.mine_freedict as mf
        monkeypatch.setattr(mf, "DICTD_ENG_SPA", dictd_path)
        monkeypatch.setattr(mf, "TEI_SPA_ENG_CACHE", tei_path)

        out_file = tmp_path / "out.jsonl"
        runner = CliRunner()
        result = runner.invoke(cli, ["--output", str(out_file), "--min-length", "4"])
        assert result.exit_code == 0, result.output
        lines = out_file.read_text().strip().splitlines()
        for line in lines:
            obj = json.loads(line)
            assert len(obj["input"]) >= 4
            assert len(obj["output"]) >= 4
