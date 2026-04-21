"""Tests for build_corpus v2.3 additions: layers D/E/F and curriculum ordering."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from translation.build_corpus import _curriculum_sort, _read_jsonl, _write_jsonl, cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_layer(path: Path, records: list) -> None:
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )


def _make_record(source: str, n: int, input_len: int = 20) -> dict:
    return {
        "instruction": "Translate.",
        "input": ("x" * input_len) + f"-{source}-{n}",
        "output": f"output-{source}-{n}",
        "source": source,
    }


def _invoke(
    tmp_path,
    a_records, b_records, c_records,
    d_records=None, e_records=None, f_records=None,
    upsample=1,
    shuffle=True,
    curriculum_order=False,
    seed=42,
):
    a = tmp_path / "a.jsonl"; _write_layer(a, a_records)
    b = tmp_path / "b.jsonl"; _write_layer(b, b_records)
    c = tmp_path / "c.jsonl"; _write_layer(c, c_records)
    out = tmp_path / "corpus.jsonl"
    args = [
        "--layer-a", str(a),
        "--layer-b", str(b),
        "--layer-c", str(c),
        "--layer-c-upsample", str(upsample),
        "--seed", str(seed),
        "--output", str(out),
    ]
    if d_records is not None:
        d = tmp_path / "d.jsonl"; _write_layer(d, d_records)
        args += ["--layer-d", str(d)]
    if e_records is not None:
        e = tmp_path / "e.jsonl"; _write_layer(e, e_records)
        args += ["--layer-e", str(e)]
    if f_records is not None:
        f = tmp_path / "f.jsonl"; _write_layer(f, f_records)
        args += ["--layer-f", str(f)]
    if not shuffle:
        args.append("--no-shuffle")
    if curriculum_order:
        args.append("--curriculum-order")
    runner = CliRunner()
    result = runner.invoke(cli, args)
    return result, out


# ---------------------------------------------------------------------------
# Optional layer loading
# ---------------------------------------------------------------------------

class TestOptionalLayers:
    def test_without_optional_layers(self, tmp_path):
        """A/B/C only still works (backward compat)."""
        a = [_make_record("sub", i) for i in range(5)]
        b = [_make_record("freedict", i) for i in range(3)]
        c = [_make_record("curated", i) for i in range(2)]
        result, out = _invoke(tmp_path, a, b, c, upsample=1)
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 10  # 5 + 3 + 2

    def test_with_layer_d(self, tmp_path):
        a = [_make_record("sub", i) for i in range(5)]
        b = [_make_record("freedict", i) for i in range(3)]
        c = [_make_record("curated", i) for i in range(2)]
        d = [_make_record("opensubtitles", i) for i in range(4)]
        result, out = _invoke(tmp_path, a, b, c, d_records=d, upsample=1)
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 14  # 5+3+2+4

    def test_with_all_optional_layers(self, tmp_path):
        a = [_make_record("sub", i) for i in range(5)]
        b = [_make_record("freedict", i) for i in range(3)]
        c = [_make_record("curated", i) for i in range(2)]
        d = [_make_record("opensubtitles", i) for i in range(4)]
        e = [_make_record("wmt", i) for i in range(6)]
        f = [_make_record("distilled", i) for i in range(7)]
        result, out = _invoke(tmp_path, a, b, c, d_records=d, e_records=e, f_records=f, upsample=1)
        assert result.exit_code == 0, result.output + str(result.exception)
        lines = out.read_text().strip().splitlines()
        assert len(lines) == 27  # 5+3+2+4+6+7

    def test_source_breakdown_includes_new_sources(self, tmp_path):
        a = [_make_record("sub", i) for i in range(3)]
        b = [_make_record("freedict", i) for i in range(2)]
        c = [_make_record("curated", i) for i in range(1)]
        d = [_make_record("opensubtitles", i) for i in range(2)]
        e = [_make_record("wmt", i) for i in range(2)]
        result, _ = _invoke(tmp_path, a, b, c, d_records=d, e_records=e, upsample=1)
        assert result.exit_code == 0
        assert "opensubtitles" in result.output
        assert "wmt" in result.output


# ---------------------------------------------------------------------------
# Curriculum ordering
# ---------------------------------------------------------------------------

class TestCurriculumSort:
    def _easy_record(self, n: int) -> dict:
        return {
            "instruction": "T.",
            "input": "Short" + str(n),  # < 20 chars
            "output": "out",
            "source": "freedict",
        }

    def _medium_record(self, n: int) -> dict:
        return {
            "instruction": "T.",
            "input": "This is a medium length input sentence " + str(n),  # > 20 chars
            "output": "out",
            "source": "parallel-sub",
        }

    def _hard_record(self, n: int) -> dict:
        return {
            "instruction": "T.",
            "input": "Hard example " + str(n),
            "output": "out",
            "source": "curated",
        }

    def test_easy_before_medium(self):
        records = [self._medium_record(0), self._easy_record(0)]
        sorted_recs = _curriculum_sort(records)
        # Easy (tier 0) should come before medium (tier 1)
        assert sorted_recs[0]["source"] == "freedict"
        assert sorted_recs[1]["source"] == "parallel-sub"

    def test_curated_last(self):
        records = [
            self._hard_record(0),
            self._easy_record(0),
            self._medium_record(0),
        ]
        sorted_recs = _curriculum_sort(records)
        # Curated (tier 2) should be last
        assert sorted_recs[-1]["source"] == "curated"

    def test_within_tier_sorted_by_length(self):
        records = [
            {"instruction": "T.", "input": "Longer easy input", "output": "x", "source": "freedict"},
            {"instruction": "T.", "input": "Short", "output": "x", "source": "freedict"},
            {"instruction": "T.", "input": "Medium easy here", "output": "x", "source": "freedict"},
        ]
        sorted_recs = _curriculum_sort(records)
        lengths = [len(r["input"]) for r in sorted_recs]
        assert lengths == sorted(lengths)

    def test_curriculum_flag_in_cli(self, tmp_path):
        a = [
            {"instruction": "T.", "input": "A" * 25 + str(i), "output": "out", "source": "sub"}
            for i in range(5)
        ]
        b = [{"instruction": "T.", "input": "B" + str(i), "output": "out", "source": "freedict"} for i in range(5)]
        c = [{"instruction": "T.", "input": "curated " + str(i), "output": "out", "source": "curated"} for i in range(3)]
        result, out = _invoke(tmp_path, a, b, c, upsample=1, shuffle=False, curriculum_order=True)
        assert result.exit_code == 0, result.output + str(result.exception)
        records = [json.loads(l) for l in out.read_text().strip().splitlines()]
        # Last 3 should be curated
        for rec in records[-3:]:
            assert rec["source"] == "curated"
        assert "curriculum" in result.output.lower()

    def test_curriculum_overrides_shuffle(self, tmp_path):
        """--curriculum-order with --shuffle still applies curriculum."""
        a = [{"instruction": "T.", "input": "A" * 25 + str(i), "output": "out", "source": "sub"} for i in range(3)]
        b = [{"instruction": "T.", "input": "B" + str(i), "output": "out", "source": "freedict"} for i in range(3)]
        c = [{"instruction": "T.", "input": "curated " + str(i), "output": "out", "source": "curated"} for i in range(2)]
        result, out = _invoke(tmp_path, a, b, c, upsample=1, shuffle=True, curriculum_order=True)
        assert result.exit_code == 0, result.output + str(result.exception)
        records = [json.loads(l) for l in out.read_text().strip().splitlines()]
        # Curated should be last
        for rec in records[-2:]:
            assert rec["source"] == "curated"
