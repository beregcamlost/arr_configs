"""Tests for build_corpus — layer merging, upsampling, shuffling, CLI."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from translation.build_corpus import _read_jsonl, _write_jsonl, cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_layer(path: Path, records: list) -> None:
    """Write a list of dicts to a JSONL file."""
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _make_record(source: str, n: int) -> dict:
    return {
        "instruction": "Translate.",
        "input": f"input-{source}-{n}",
        "output": f"output-{source}-{n}",
        "source": source,
    }


# ---------------------------------------------------------------------------
# _read_jsonl
# ---------------------------------------------------------------------------

class TestReadJsonl:
    def test_reads_valid_file(self, tmp_path):
        p = tmp_path / "test.jsonl"
        _write_layer(p, [{"a": 1}, {"b": 2}])
        records = _read_jsonl(p)
        assert records == [{"a": 1}, {"b": 2}]

    def test_skips_blank_lines(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text('{"a":1}\n\n{"b":2}\n', encoding="utf-8")
        records = _read_jsonl(p)
        assert len(records) == 2

    def test_raises_on_bad_json(self, tmp_path):
        p = tmp_path / "bad.jsonl"
        p.write_text("not json\n", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            _read_jsonl(p)


# ---------------------------------------------------------------------------
# _write_jsonl
# ---------------------------------------------------------------------------

class TestWriteJsonl:
    def test_writes_valid_jsonl(self, tmp_path):
        p = tmp_path / "out.jsonl"
        _write_jsonl([{"a": 1}, {"b": 2}], p)
        lines = p.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"a": 1}

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "deep" / "out.jsonl"
        _write_jsonl([{"x": 1}], p)
        assert p.exists()

    def test_unicode_preserved(self, tmp_path):
        p = tmp_path / "out.jsonl"
        _write_jsonl([{"text": "¡Hola! ¿Cómo estás?"}], p)
        obj = json.loads(p.read_text().strip())
        assert obj["text"] == "¡Hola! ¿Cómo estás?"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class TestCli:
    def _invoke(self, tmp_path, a_records, b_records, c_records,
                upsample=3, shuffle=True, seed=42):
        a_path = tmp_path / "layerA.jsonl"
        b_path = tmp_path / "layerB.jsonl"
        c_path = tmp_path / "layerC.jsonl"
        out_path = tmp_path / "corpus.jsonl"
        _write_layer(a_path, a_records)
        _write_layer(b_path, b_records)
        _write_layer(c_path, c_records)
        runner = CliRunner()
        args = [
            "--layer-a", str(a_path),
            "--layer-b", str(b_path),
            "--layer-c", str(c_path),
            "--layer-c-upsample", str(upsample),
            "--seed", str(seed),
            "--output", str(out_path),
        ]
        if not shuffle:
            args.append("--no-shuffle")
        result = runner.invoke(cli, args)
        return result, out_path

    def test_basic_merge_counts(self, tmp_path):
        """Total lines = A + B + C × upsample."""
        a = [_make_record("sub", i) for i in range(10)]
        b = [_make_record("freedict", i) for i in range(5)]
        c = [_make_record("curated", i) for i in range(4)]
        result, out = self._invoke(tmp_path, a, b, c, upsample=3)
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 10 + 5 + 4 * 3  # = 27

    def test_layer_c_upsample_one(self, tmp_path):
        """upsample=1 means layer C appears exactly once."""
        a = [_make_record("sub", i) for i in range(2)]
        b = [_make_record("freedict", i) for i in range(2)]
        c = [_make_record("curated", i) for i in range(3)]
        result, out = self._invoke(tmp_path, a, b, c, upsample=1)
        assert result.exit_code == 0, result.output
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 2 + 2 + 3  # = 7

    def test_shuffle_deterministic(self, tmp_path):
        """Same seed produces same output."""
        a = [_make_record("sub", i) for i in range(20)]
        b = [_make_record("freedict", i) for i in range(10)]
        c = [_make_record("curated", i) for i in range(5)]
        _, out1 = self._invoke(tmp_path, a, b, c, seed=42)
        # Second run in a different output
        a2 = tmp_path / "a2"
        b2 = tmp_path / "b2"
        c2 = tmp_path / "c2"
        out2 = tmp_path / "corpus2.jsonl"
        a2.mkdir(); b2.mkdir(); c2.mkdir()
        a_path = a2 / "a.jsonl"; _write_layer(a_path, a)
        b_path = b2 / "b.jsonl"; _write_layer(b_path, b)
        c_path = c2 / "c.jsonl"; _write_layer(c_path, c)
        runner = CliRunner()
        runner.invoke(cli, [
            "--layer-a", str(a_path), "--layer-b", str(b_path),
            "--layer-c", str(c_path), "--layer-c-upsample", "3",
            "--seed", "42", "--output", str(out2),
        ])
        assert out1.read_text() == out2.read_text()

    def test_no_shuffle_preserves_order(self, tmp_path):
        """--no-shuffle keeps A, then B, then C×upsample in order."""
        a = [_make_record("sub", i) for i in range(3)]
        b = [_make_record("freedict", i) for i in range(2)]
        c = [_make_record("curated", i) for i in range(1)]
        result, out = self._invoke(tmp_path, a, b, c, upsample=2, shuffle=False)
        assert result.exit_code == 0, result.output
        records = [json.loads(l) for l in out.read_text().strip().splitlines()]
        # First 3 are from layer A
        for i in range(3):
            assert records[i]["source"] == "sub"
        # Next 2 are from layer B
        for i in range(3, 5):
            assert records[i]["source"] == "freedict"
        # Next 2 are from C × 2
        for i in range(5, 7):
            assert records[i]["source"] == "curated"

    def test_source_breakdown_in_output(self, tmp_path):
        """Output log includes source breakdown stats."""
        a = [_make_record("sub", i) for i in range(5)]
        b = [_make_record("freedict", i) for i in range(3)]
        c = [_make_record("curated", i) for i in range(2)]
        result, _ = self._invoke(tmp_path, a, b, c, upsample=1)
        assert result.exit_code == 0
        assert "parallel-sub" in result.output or "freedict" in result.output

    def test_empty_layer_b_allowed(self, tmp_path):
        """Layer B can be empty (zero records)."""
        a = [_make_record("sub", i) for i in range(5)]
        b = []
        c = [_make_record("curated", i) for i in range(2)]
        result, out = self._invoke(tmp_path, a, b, c, upsample=1)
        assert result.exit_code == 0, result.output
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 5 + 0 + 2

    def test_all_records_valid_json(self, tmp_path):
        """Every output line is valid JSON with expected fields."""
        a = [_make_record("sub", i) for i in range(10)]
        b = [_make_record("freedict", i) for i in range(5)]
        c = [_make_record("curated", i) for i in range(3)]
        result, out = self._invoke(tmp_path, a, b, c)
        assert result.exit_code == 0
        for line in out.read_text().strip().splitlines():
            obj = json.loads(line)
            assert "instruction" in obj
            assert "input" in obj
            assert "output" in obj
            assert "source" in obj
