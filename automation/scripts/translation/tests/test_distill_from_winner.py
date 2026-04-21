"""Tests for distill_from_winner — I/O helpers and prompt construction.

NOTE: Model inference is NOT tested here (requires WSL GPU).
We test: JSONL I/O, prompt building, response extraction, batch splitting.
"""

import json
from pathlib import Path

import pytest

from translation.distill_from_winner import (
    INSTRUCTION,
    _build_prompt,
    _extract_response,
    _read_input_jsonl,
    _batched,
)


# ---------------------------------------------------------------------------
# _read_input_jsonl
# ---------------------------------------------------------------------------

class TestReadInputJsonl:
    def test_reads_english_field(self, tmp_path):
        p = tmp_path / "input.jsonl"
        p.write_text(
            '{"english": "Hello world."}\n{"english": "How are you?"}\n',
            encoding="utf-8",
        )
        cues = _read_input_jsonl(p)
        assert cues == ["Hello world.", "How are you?"]

    def test_skips_blank_lines(self, tmp_path):
        p = tmp_path / "input.jsonl"
        p.write_text('{"english": "Hello."}\n\n{"english": "World."}\n', encoding="utf-8")
        cues = _read_input_jsonl(p)
        assert len(cues) == 2

    def test_skips_missing_english_field(self, tmp_path):
        p = tmp_path / "input.jsonl"
        p.write_text('{"other": "data"}\n{"english": "Valid line."}\n', encoding="utf-8")
        cues = _read_input_jsonl(p)
        assert cues == ["Valid line."]

    def test_skips_empty_english_value(self, tmp_path):
        p = tmp_path / "input.jsonl"
        p.write_text('{"english": ""}\n{"english": "  "}\n{"english": "Good."}\n', encoding="utf-8")
        cues = _read_input_jsonl(p)
        assert cues == ["Good."]

    def test_warns_on_bad_json(self, tmp_path, capsys):
        p = tmp_path / "input.jsonl"
        p.write_text('not json\n{"english": "Valid."}\n', encoding="utf-8")
        cues = _read_input_jsonl(p)
        # Bad line is skipped, valid line is kept
        assert cues == ["Valid."]

    def test_strips_whitespace_from_cues(self, tmp_path):
        p = tmp_path / "input.jsonl"
        p.write_text('{"english": "  Hello world.  "}\n', encoding="utf-8")
        cues = _read_input_jsonl(p)
        assert cues == ["Hello world."]


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_contains_instruction(self):
        prompt = _build_prompt("Hello world.")
        assert INSTRUCTION in prompt

    def test_contains_input(self):
        prompt = _build_prompt("She walked slowly.")
        assert "She walked slowly." in prompt

    def test_ends_with_response_marker(self):
        prompt = _build_prompt("Test sentence.")
        assert prompt.endswith("### Response:\n")

    def test_format_structure(self):
        prompt = _build_prompt("Test.")
        assert "### Instruction:" in prompt
        assert "### Input:" in prompt
        assert "### Response:" in prompt


# ---------------------------------------------------------------------------
# _extract_response
# ---------------------------------------------------------------------------

class TestExtractResponse:
    def test_strips_prompt_prefix(self):
        prompt = _build_prompt("Hello.")
        full_output = prompt + "Hola."
        result = _extract_response(full_output, prompt)
        assert result == "Hola."

    def test_falls_back_to_response_marker(self):
        prompt = _build_prompt("Hello.")
        # Simulate model that re-generates the prompt differently
        full_output = "some preamble\n### Response:\nHola."
        result = _extract_response(full_output, "different prefix")
        assert result == "Hola."

    def test_stops_at_next_section(self):
        prompt = _build_prompt("Hello.")
        full_output = prompt + "Hola.\n### Instruction:\nNext example"
        result = _extract_response(full_output, prompt)
        assert result == "Hola."

    def test_strips_whitespace(self):
        prompt = _build_prompt("Hello.")
        full_output = prompt + "  Hola.  "
        result = _extract_response(full_output, prompt)
        assert result == "Hola."

    def test_no_marker_returns_full(self):
        """When no marker found, returns the full generated text stripped."""
        result = _extract_response("  some output  ", "no match here")
        assert result == "some output"


# ---------------------------------------------------------------------------
# _batched
# ---------------------------------------------------------------------------

class TestBatched:
    def test_even_split(self):
        items = list(range(10))
        batches = list(_batched(items, 5))
        assert batches == [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]]

    def test_uneven_split(self):
        items = list(range(7))
        batches = list(_batched(items, 3))
        assert batches == [[0, 1, 2], [3, 4, 5], [6]]

    def test_single_batch(self):
        items = [1, 2, 3]
        batches = list(_batched(items, 10))
        assert batches == [[1, 2, 3]]

    def test_empty_input(self):
        batches = list(_batched([], 5))
        assert batches == []

    def test_batch_size_one(self):
        batches = list(_batched([1, 2, 3], 1))
        assert batches == [[1], [2], [3]]
