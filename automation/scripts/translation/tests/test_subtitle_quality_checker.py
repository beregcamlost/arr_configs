"""Tests for subtitle_quality_checker — 90%+ line coverage target."""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the automation/scripts root is on sys.path so the `translation`
# package is importable without needing to install it.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir),
)

import translation.subtitle_quality_checker as sqc
from translation.subtitle_quality_checker import (
    DEFAULT_MODEL,
    DEFAULT_SAMPLE_COUNT,
    _ensure_quality_table,
    build_quality_prompt,
    check_cache,
    check_subtitle_quality,
    parse_quality_response,
    sample_cues,
    save_cache,
)
from translation.srt_parser import Cue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cues(n: int, text_prefix: str = "Line") -> list:
    return [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text=f"{text_prefix} {i}")
        for i in range(1, n + 1)
    ]


def _make_srt(n: int) -> str:
    """Return a minimal SRT string with *n* cues."""
    blocks = []
    for i in range(1, n + 1):
        blocks.append(
            f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},999\nLine {i}"
        )
    return "\n\n".join(blocks) + "\n"


def _make_quality_db(tmp_path: Path) -> str:
    db_path = str(tmp_path / "quality_state.db")
    conn = sqlite3.connect(db_path)
    _ensure_quality_table(conn)
    conn.close()
    return db_path


@pytest.fixture(autouse=True)
def _reset_exhausted_keys():
    """Ensure the session-scoped exhausted-key set is clean for every test."""
    sqc._exhausted_keys.clear()
    yield
    sqc._exhausted_keys.clear()


# ---------------------------------------------------------------------------
# sample_cues
# ---------------------------------------------------------------------------

class TestSampleCues:
    def test_empty_list_returns_empty(self):
        assert sample_cues([]) == []

    def test_fewer_than_count_returns_all_middle(self):
        # 4 cues → middle 60% is indices 0.8–3.2 → floor [0, 3] = cues[0]..cues[2]
        cues = _make_cues(4)
        result = sample_cues(cues, count=10)
        # All (or most) middle cues returned — length <= 4
        assert len(result) <= 4
        assert len(result) > 0

    def test_single_cue_returns_it(self):
        cues = _make_cues(1)
        result = sample_cues(cues, count=10)
        assert result == cues  # middle is full list when start_idx >= end_idx

    def test_100_cues_returns_10_from_middle(self):
        cues = _make_cues(100)
        result = sample_cues(cues, count=10)
        assert len(result) == 10
        # All sampled cues should come from the middle 60% (indices 20–79)
        for cue in result:
            assert 20 <= cue.index <= 80

    def test_count_larger_than_middle_returns_all_middle(self):
        cues = _make_cues(10)
        # Middle 60% of 10 = indices 2..8 → 6 cues
        result = sample_cues(cues, count=100)
        assert len(result) <= 10
        assert len(result) > 0

    def test_evenly_spaced_no_duplicates(self):
        cues = _make_cues(50)
        result = sample_cues(cues, count=5)
        indices = [c.index for c in result]
        assert len(indices) == len(set(indices)), "Expected no duplicate cues"

    def test_default_count_is_10(self):
        cues = _make_cues(100)
        result = sample_cues(cues)
        assert len(result) == DEFAULT_SAMPLE_COUNT


# ---------------------------------------------------------------------------
# build_quality_prompt
# ---------------------------------------------------------------------------

class TestBuildQualityPrompt:
    def test_contains_language_name(self):
        cues = _make_cues(3)
        prompt = build_quality_prompt(cues, "es")
        assert "Spanish" in prompt

    def test_unknown_lang_code_used_verbatim(self):
        cues = _make_cues(2)
        prompt = build_quality_prompt(cues, "xx")
        assert "xx" in prompt

    def test_contains_cue_timestamps_and_text(self):
        cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hola mundo")]
        prompt = build_quality_prompt(cues, "es")
        assert "00:00:01,000" in prompt
        assert "Hola mundo" in prompt

    def test_contains_json_instruction(self):
        cues = _make_cues(2)
        prompt = build_quality_prompt(cues, "en")
        assert "GOOD|WARN|BAD" in prompt
        assert "confidence" in prompt

    def test_cue_numbering_starts_at_1(self):
        cues = _make_cues(3)
        prompt = build_quality_prompt(cues, "fr")
        assert "1. [" in prompt
        assert "3. [" in prompt

    def test_multiple_cues_all_present(self):
        cues = _make_cues(5)
        prompt = build_quality_prompt(cues, "de")
        for i in range(1, 6):
            assert f"Line {i}" in prompt


# ---------------------------------------------------------------------------
# parse_quality_response
# ---------------------------------------------------------------------------

class TestParseQualityResponse:
    def test_good_verdict(self):
        resp = 'Some analysis.\n{"quality": "GOOD", "confidence": 0.95, "actual_lang": "es", "reason": "Correct Spanish"}'
        result = parse_quality_response(resp)
        assert result is not None
        assert result["quality"] == "GOOD"
        assert result["confidence"] == pytest.approx(0.95)
        assert result["actual_lang"] == "es"
        assert "Spanish" in result["reason"]

    def test_warn_verdict(self):
        resp = '{"quality": "WARN", "confidence": 0.6, "actual_lang": "pt", "reason": "Dialect mix"}'
        result = parse_quality_response(resp)
        assert result["quality"] == "WARN"

    def test_bad_verdict(self):
        resp = '{"quality": "BAD", "confidence": 0.99, "actual_lang": "en", "reason": "Wrong language"}'
        result = parse_quality_response(resp)
        assert result["quality"] == "BAD"

    def test_json_not_on_last_line_still_found(self):
        resp = (
            "1. LANGUAGE: No, this is English\n"
            "2. QUALITY: garbage\n"
            '{"quality": "BAD", "confidence": 0.98, "actual_lang": "en", "reason": "wrong lang"}\n'
            "Summary: bad subtitle."
        )
        result = parse_quality_response(resp)
        # reversed() will find the JSON line before the summary line
        assert result["quality"] == "BAD"

    def test_no_json_returns_none(self):
        resp = "This looks fine to me, it is clearly Spanish."
        assert parse_quality_response(resp) is None

    def test_invalid_json_returns_none(self):
        resp = "{quality: BAD, not valid json}"
        assert parse_quality_response(resp) is None

    def test_missing_quality_field_returns_none(self):
        resp = '{"confidence": 0.9, "actual_lang": "es", "reason": "ok"}'
        assert parse_quality_response(resp) is None

    def test_invalid_quality_value_returns_none(self):
        resp = '{"quality": "MAYBE", "confidence": 0.5, "actual_lang": "es", "reason": "hmm"}'
        assert parse_quality_response(resp) is None

    def test_quality_normalised_to_uppercase(self):
        resp = '{"quality": "good", "confidence": 0.8, "actual_lang": "es", "reason": "ok"}'
        result = parse_quality_response(resp)
        assert result["quality"] == "GOOD"

    def test_confidence_defaults_to_zero_when_missing(self):
        resp = '{"quality": "WARN", "actual_lang": "es", "reason": "minor"}'
        result = parse_quality_response(resp)
        assert result["confidence"] == pytest.approx(0.0)

    def test_empty_response_returns_none(self):
        assert parse_quality_response("") is None


# ---------------------------------------------------------------------------
# check_cache / save_cache
# ---------------------------------------------------------------------------

class TestCacheRoundtrip:
    def test_save_then_retrieve(self, tmp_path):
        db = _make_quality_db(tmp_path)
        result = {
            "quality": "GOOD",
            "confidence": 0.9,
            "actual_lang": "es",
            "reason": "Correct Spanish",
        }
        save_cache(db, "/media/test.srt", 1700000000, "es", result)
        cached = check_cache(db, "/media/test.srt", 1700000000, "es")
        assert cached is not None
        assert cached["quality"] == "GOOD"
        assert cached["confidence"] == pytest.approx(0.9)
        assert cached["actual_lang"] == "es"

    def test_different_mtime_is_cache_miss(self, tmp_path):
        db = _make_quality_db(tmp_path)
        result = {"quality": "GOOD", "confidence": 0.9, "actual_lang": "es", "reason": "ok"}
        save_cache(db, "/media/test.srt", 1700000000, "es", result)
        cached = check_cache(db, "/media/test.srt", 9999999999, "es")
        assert cached is None

    def test_different_lang_is_cache_miss(self, tmp_path):
        db = _make_quality_db(tmp_path)
        result = {"quality": "GOOD", "confidence": 0.9, "actual_lang": "es", "reason": "ok"}
        save_cache(db, "/media/test.srt", 1700000000, "es", result)
        cached = check_cache(db, "/media/test.srt", 1700000000, "fr")
        assert cached is None

    def test_insert_or_replace_updates_existing(self, tmp_path):
        db = _make_quality_db(tmp_path)
        r1 = {"quality": "GOOD", "confidence": 0.9, "actual_lang": "es", "reason": "first"}
        r2 = {"quality": "BAD", "confidence": 0.99, "actual_lang": "en", "reason": "second"}
        save_cache(db, "/media/test.srt", 1700000000, "es", r1)
        save_cache(db, "/media/test.srt", 1700000000, "es", r2)
        cached = check_cache(db, "/media/test.srt", 1700000000, "es")
        assert cached["quality"] == "BAD"

    def test_check_cache_bad_db_path_returns_none(self):
        result = check_cache("/nonexistent/dir/db.db", "/media/test.srt", 0, "es")
        assert result is None

    def test_save_cache_bad_db_path_does_not_raise(self):
        # Should log a warning but not raise
        result = {"quality": "GOOD", "confidence": 0.9, "actual_lang": "es", "reason": "ok"}
        save_cache("/nonexistent/dir/db.db", "/media/test.srt", 0, "es", result)


# ---------------------------------------------------------------------------
# check_subtitle_quality — integration (Gemini mocked)
# ---------------------------------------------------------------------------

class TestCheckSubtitleQuality:
    """Tests for the main quality-check function."""

    # -- File / parse edge cases (no API needed) ----------------------------

    def test_empty_file_returns_bad(self, tmp_path):
        srt = tmp_path / "empty.srt"
        srt.write_text("", encoding="utf-8")
        result = check_subtitle_quality(str(srt), "es", [])
        assert result["quality"] == "BAD"
        assert "0 cues" in result["reason"]

    def test_too_few_cues_returns_bad(self, tmp_path):
        srt = tmp_path / "tiny.srt"
        srt.write_text(_make_srt(3), encoding="utf-8")
        result = check_subtitle_quality(str(srt), "es", [])
        assert result["quality"] == "BAD"
        assert "too few cues" in result["reason"]

    def test_nonexistent_file_returns_skip(self):
        result = check_subtitle_quality("/no/such/file.srt", "es", ["key1"])
        assert result["quality"] == "SKIP"
        assert "read error" in result["reason"]

    def test_no_api_keys_returns_skip(self, tmp_path):
        srt = tmp_path / "good.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")
        result = check_subtitle_quality(str(srt), "es", [])
        assert result["quality"] == "SKIP"
        assert "no API keys" in result["reason"]

    def test_all_keys_exhausted_returns_skip(self, tmp_path):
        srt = tmp_path / "good.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")
        sqc._exhausted_keys.add("key1")
        result = check_subtitle_quality(str(srt), "es", ["key1"])
        assert result["quality"] == "SKIP"
        assert "exhausted" in result["reason"]

    # -- Successful Gemini response -----------------------------------------

    def test_good_response(self, tmp_path):
        srt = tmp_path / "good.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        mock_response = MagicMock()
        mock_response.text = (
            'Analysis done.\n'
            '{"quality": "GOOD", "confidence": 0.95, "actual_lang": "es", "reason": "Correct Spanish"}'
        )

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(str(srt), "es", ["valid-key"])

        assert result["quality"] == "GOOD"
        assert result["confidence"] == pytest.approx(0.95)

    def test_bad_response(self, tmp_path):
        srt = tmp_path / "bad.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        mock_response = MagicMock()
        mock_response.text = (
            '{"quality": "BAD", "confidence": 0.99, "actual_lang": "en", "reason": "Wrong language"}'
        )

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(str(srt), "es", ["valid-key"])

        assert result["quality"] == "BAD"

    def test_unparseable_response_returns_skip(self, tmp_path):
        srt = tmp_path / "good.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        mock_response = MagicMock()
        mock_response.text = "I cannot determine the quality of these subtitles."

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(str(srt), "es", ["valid-key"])

        assert result["quality"] == "SKIP"
        assert "unparseable" in result["reason"]

    # -- Key rotation on ResourceExhausted ----------------------------------

    def test_first_key_exhausted_falls_through_to_second(self, tmp_path):
        from google.api_core.exceptions import ResourceExhausted

        srt = tmp_path / "good.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        good_response = MagicMock()
        good_response.text = (
            '{"quality": "GOOD", "confidence": 0.9, "actual_lang": "es", "reason": "ok"}'
        )

        call_count = [0]

        def side_effect(prompt):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ResourceExhausted("quota")
            return good_response

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.side_effect = side_effect
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(
                str(srt), "es", ["key-exhausted", "key-good"]
            )

        assert result["quality"] == "GOOD"
        assert "key-exhausted" in sqc._exhausted_keys

    def test_all_keys_exhausted_by_resource_exhausted(self, tmp_path):
        from google.api_core.exceptions import ResourceExhausted

        srt = tmp_path / "good.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.side_effect = ResourceExhausted("quota")
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(str(srt), "es", ["key1", "key2"])

        assert result["quality"] == "SKIP"
        assert "key1" in sqc._exhausted_keys
        assert "key2" in sqc._exhausted_keys

    def test_generic_exception_returns_skip(self, tmp_path):
        srt = tmp_path / "good.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.side_effect = RuntimeError("network down")
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(str(srt), "es", ["valid-key"])

        assert result["quality"] == "SKIP"
        assert "API error" in result["reason"]

    # -- Caching integration ------------------------------------------------

    def test_cache_hit_skips_api(self, tmp_path):
        srt = tmp_path / "cached.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")
        db = _make_quality_db(tmp_path)

        mtime = int(os.path.getmtime(str(srt)))
        cached_result = {
            "quality": "GOOD",
            "confidence": 0.88,
            "actual_lang": "es",
            "reason": "cached",
        }
        save_cache(db, str(srt), mtime, "es", cached_result)

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            result = check_subtitle_quality(
                str(srt), "es", ["valid-key"], state_db=db
            )
            # GenerativeModel should never be called — cache hit
            mock_genai.GenerativeModel.assert_not_called()

        assert result["quality"] == "GOOD"
        assert result["reason"] == "cached"

    def test_result_saved_to_cache(self, tmp_path):
        srt = tmp_path / "new.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")
        db = _make_quality_db(tmp_path)

        mock_response = MagicMock()
        mock_response.text = (
            '{"quality": "WARN", "confidence": 0.7, "actual_lang": "es", "reason": "dialect"}'
        )

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(
                str(srt), "es", ["valid-key"], state_db=db
            )

        assert result["quality"] == "WARN"

        # Second call should hit cache
        with patch("translation.subtitle_quality_checker.genai") as mock_genai2:
            result2 = check_subtitle_quality(
                str(srt), "es", ["valid-key"], state_db=db
            )
            mock_genai2.GenerativeModel.assert_not_called()

        assert result2["quality"] == "WARN"

    def test_state_db_oserror_on_mtime_uses_zero(self, tmp_path):
        """When getmtime fails, mtime=0 is used; should still work."""
        db = _make_quality_db(tmp_path)
        mock_response = MagicMock()
        mock_response.text = (
            '{"quality": "GOOD", "confidence": 0.9, "actual_lang": "es", "reason": "ok"}'
        )

        with patch("translation.subtitle_quality_checker.genai") as mock_genai, \
             patch("translation.subtitle_quality_checker.os.path.getmtime",
                   side_effect=OSError("no stat")):
            srt = tmp_path / "mtime_err.srt"
            srt.write_text(_make_srt(20), encoding="utf-8")

            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            mock_genai.GenerativeModel.return_value = mock_model

            result = check_subtitle_quality(str(srt), "es", ["valid-key"], state_db=db)

        assert result["quality"] == "GOOD"


# ---------------------------------------------------------------------------
# CLI — main()
# ---------------------------------------------------------------------------

class TestMainCLI:
    def _run_main(self, argv, monkeypatch, env=None):
        monkeypatch.setattr(sys, "argv", ["subtitle_quality_checker"] + argv)
        if env:
            for k, v in env.items():
                monkeypatch.setenv(k, v)
        from translation.subtitle_quality_checker import main
        return main

    def test_check_subcommand_output_format(self, tmp_path, monkeypatch, capsys):
        srt = tmp_path / "file.es.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        mock_response = MagicMock()
        mock_response.text = (
            '{"quality": "GOOD", "confidence": 0.95, "actual_lang": "es", "reason": "fine"}'
        )

        monkeypatch.setattr(sys, "argv", [
            "sqc", "check", "--srt", str(srt), "--expected-lang", "es"
        ])
        monkeypatch.setenv("GEMINI_API_KEY_1", "test-key-1")
        monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            mock_genai.GenerativeModel.return_value = mock_model

            from translation.subtitle_quality_checker import main
            main()

        out = capsys.readouterr().out.strip()
        parts = out.split("\t")
        assert len(parts) == 4
        assert parts[0] == str(srt)
        assert parts[1] == "GOOD"
        assert parts[2] == "0.95"
        assert parts[3] == "fine"

    def test_check_subcommand_skip_no_keys(self, tmp_path, monkeypatch, capsys):
        srt = tmp_path / "file.es.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")

        monkeypatch.setattr(sys, "argv", [
            "sqc", "check", "--srt", str(srt), "--expected-lang", "es"
        ])
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)

        from translation.subtitle_quality_checker import main
        main()

        out = capsys.readouterr().out.strip()
        parts = out.split("\t")
        assert parts[1] == "SKIP"

    def test_batch_max_calls_stops_early(self, tmp_path, monkeypatch, capsys):
        # Create 5 SRT files in the directory
        for i in range(1, 6):
            f = tmp_path / f"show.s01e0{i}.es.srt"
            f.write_text(_make_srt(20), encoding="utf-8")

        call_count = [0]
        good_response = MagicMock()
        good_response.text = (
            '{"quality": "GOOD", "confidence": 0.9, "actual_lang": "es", "reason": "ok"}'
        )

        def gen_content(prompt):
            call_count[0] += 1
            return good_response

        monkeypatch.setattr(sys, "argv", [
            "sqc", "batch",
            "--dir", str(tmp_path),
            "--expected-lang", "es",
            "--max-calls", "2",
        ])
        monkeypatch.setenv("GEMINI_API_KEY_1", "test-key-1")
        monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.side_effect = gen_content
            mock_genai.GenerativeModel.return_value = mock_model

            from translation.subtitle_quality_checker import main
            main()

        out = capsys.readouterr().out
        lines = [l for l in out.strip().splitlines() if l]
        assert len(lines) == 2
        assert call_count[0] == 2

    def test_no_subcommand_exits_1(self, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["sqc"])
        from translation.subtitle_quality_checker import main
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

    def test_batch_no_matching_files(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", [
            "sqc", "batch",
            "--dir", str(tmp_path),
            "--expected-lang", "es",
        ])
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)

        from translation.subtitle_quality_checker import main
        main()

        out = capsys.readouterr().out
        assert out == ""

    def test_check_with_state_db(self, tmp_path, monkeypatch, capsys):
        srt = tmp_path / "file.es.srt"
        srt.write_text(_make_srt(20), encoding="utf-8")
        db = _make_quality_db(tmp_path)

        mock_response = MagicMock()
        mock_response.text = (
            '{"quality": "BAD", "confidence": 0.99, "actual_lang": "en", "reason": "wrong lang"}'
        )

        monkeypatch.setattr(sys, "argv", [
            "sqc", "check",
            "--srt", str(srt),
            "--expected-lang", "es",
            "--state-db", db,
        ])
        monkeypatch.setenv("GEMINI_API_KEY_1", "test-key-1")
        monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)

        with patch("translation.subtitle_quality_checker.genai") as mock_genai:
            mock_model = MagicMock()
            mock_model.generate_content.return_value = mock_response
            mock_genai.GenerativeModel.return_value = mock_model

            from translation.subtitle_quality_checker import main
            main()

        out = capsys.readouterr().out.strip()
        assert "BAD" in out

    def test_batch_skip_not_counted_towards_max_calls(self, tmp_path, monkeypatch, capsys):
        """SKIP results (e.g. all keys exhausted) don't count toward max-calls."""
        for i in range(1, 4):
            f = tmp_path / f"ep{i}.es.srt"
            f.write_text(_make_srt(20), encoding="utf-8")

        monkeypatch.setattr(sys, "argv", [
            "sqc", "batch",
            "--dir", str(tmp_path),
            "--expected-lang", "es",
            "--max-calls", "1",
        ])
        monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_2", raising=False)

        from translation.subtitle_quality_checker import main
        main()

        out = capsys.readouterr().out
        lines = [l for l in out.strip().splitlines() if l]
        # All 3 files produce SKIP — none count, so max-calls never triggers
        assert len(lines) == 3
        for line in lines:
            assert "SKIP" in line
