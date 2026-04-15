"""Tests for Gemini API client with multi-key rotation."""

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from translation.gemini_client import (
    translate_srt_cues,
    reset_exhausted_keys,
    has_available_keys,
    _build_prompt,
    _parse_response,
    _exhausted_for,
    GeminiQuotaExhausted,
    _exhausted_keys,
    DEFAULT_MODEL,
    FALLBACK_MODEL,
)
from translation.srt_parser import Cue


KEYS = ["key-aaa-111", "key-bbb-222"]


def _make_cues(n=3):
    return [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text=f"Line {i}")
        for i in range(1, n + 1)
    ]


@pytest.fixture(autouse=True)
def _reset_keys():
    """Reset exhausted keys before each test."""
    reset_exhausted_keys()
    yield


class TestBuildPrompt:
    def test_basic_prompt(self):
        cues = _make_cues(2)
        prompt = _build_prompt(cues, "English", "Spanish")
        assert "Translate from English to Spanish:" in prompt
        assert "1: Line 1" in prompt
        assert "2: Line 2" in prompt

    def test_multiline_cue_encodes_br(self):
        cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hello\nWorld")]
        prompt = _build_prompt(cues, "English", "Spanish")
        assert "Hello<br>World" in prompt
        assert "Hello\nWorld" not in prompt.split("\n", 1)[1]  # not raw newline in cue


class TestParseResponse:
    def test_basic_numbered_response(self):
        response = "1: Hola\n2: Mundo\n3: Adios"
        result = _parse_response(response, 3)
        assert result == ["Hola", "Mundo", "Adios"]

    def test_strips_various_prefixes(self):
        response = "1. Hola\n2) Mundo\n3- Adios"
        result = _parse_response(response, 3)
        assert result == ["Hola", "Mundo", "Adios"]

    def test_decodes_br_to_newlines(self):
        response = "1: Hola<br>Mundo"
        result = _parse_response(response, 1)
        assert result == ["Hola\nMundo"]

    def test_pads_missing_results(self):
        response = "1: Hola"
        result = _parse_response(response, 3)
        assert len(result) == 3
        assert result[0] == "Hola"
        assert result[1] == ""
        assert result[2] == ""

    def test_truncates_extra_results(self):
        response = "1: Hola\n2: Mundo\n3: Extra\n4: More"
        result = _parse_response(response, 2)
        assert len(result) == 2

    def test_skips_empty_lines(self):
        response = "1: Hola\n\n2: Mundo"
        result = _parse_response(response, 2)
        assert result == ["Hola", "Mundo"]


class TestTranslateSrtCues:
    @patch("translation.gemini_client.genai")
    def test_basic_translation(self, mock_genai):
        """Successful translation returns cues and char count."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "1: Hola\n2: Mundo\n3: Adios"
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        cues = _make_cues(3)
        result_cues, chars, key_index = translate_srt_cues(
            KEYS, cues, "English", "Spanish"
        )
        assert len(result_cues) == 3
        assert result_cues[0].text == "Hola"
        assert result_cues[1].text == "Mundo"
        assert result_cues[2].text == "Adios"
        assert chars == sum(len(c.text) for c in cues)
        assert key_index == 0

    @patch("translation.gemini_client.genai")
    def test_empty_cues(self, mock_genai):
        """Empty input returns empty output."""
        result_cues, chars, key_index = translate_srt_cues(KEYS, [], "English", "Spanish")
        assert result_cues == []
        assert chars == 0
        assert key_index == 0
        mock_genai.GenerativeModel.assert_not_called()

    @patch("translation.gemini_client.genai")
    def test_multiline_cue_roundtrip(self, mock_genai):
        """Multi-line cues survive <br> encoding/decoding."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "1: Hola<br>Mundo"
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hello\nWorld")]
        result_cues, chars, key_index = translate_srt_cues(KEYS, cues, "English", "Spanish")
        assert result_cues[0].text == "Hola\nMundo"

    @patch("translation.gemini_client.genai")
    def test_preserves_cue_timing(self, mock_genai):
        """Translated cues keep original timing."""
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "1: Hola"
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        cues = [Cue(42, "00:01:23,456", "00:01:25,789", "Hello")]
        result_cues, _, _ki = translate_srt_cues(KEYS, cues, "English", "Spanish")
        assert result_cues[0].index == 42
        assert result_cues[0].start == "00:01:23,456"
        assert result_cues[0].end == "00:01:25,789"


class TestKeyRotation:
    @patch("translation.gemini_client.time.sleep")
    @patch("translation.gemini_client.genai")
    def test_rotates_to_second_key_on_daily_quota(self, mock_genai, mock_sleep):
        """When first key hits daily quota, rotates to second key."""
        from google.api_core.exceptions import ResourceExhausted

        mock_model_bad = MagicMock()
        mock_model_bad.generate_content.side_effect = ResourceExhausted(
            "Quota exceeded: per_day limit"
        )
        mock_model_good = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "1: Hola"
        mock_model_good.generate_content.return_value = mock_response

        call_count = [0]
        def make_model(**kwargs):
            call_count[0] += 1
            if call_count[0] <= 1:
                return mock_model_bad
            return mock_model_good
        mock_genai.GenerativeModel.side_effect = make_model

        cues = _make_cues(1)
        result_cues, chars, key_index = translate_srt_cues(KEYS, cues, "English", "Spanish")
        assert result_cues[0].text == "Hola"
        assert key_index == 1  # second key succeeded
        assert KEYS[0] in _exhausted_for(DEFAULT_MODEL)
        assert KEYS[1] not in _exhausted_for(DEFAULT_MODEL)

    @patch("translation.gemini_client.time.sleep")
    @patch("translation.gemini_client.genai")
    def test_rpm_retry_then_success(self, mock_genai, mock_sleep):
        """Transient RPM limit triggers retry after sleep."""
        from google.api_core.exceptions import ResourceExhausted

        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "1: Hola"
        # First call: RPM error, second call: success
        mock_model.generate_content.side_effect = [
            ResourceExhausted("rate limit: per_minute quota"),
            mock_response,
        ]
        mock_genai.GenerativeModel.return_value = mock_model

        cues = _make_cues(1)
        result_cues, _, _ki = translate_srt_cues(KEYS, cues, "English", "Spanish")
        assert result_cues[0].text == "Hola"
        mock_sleep.assert_called_once_with(4)
        # Key should NOT be exhausted since retry succeeded
        assert KEYS[0] not in _exhausted_for(DEFAULT_MODEL)

    @patch("translation.gemini_client.time.sleep")
    @patch("translation.gemini_client.genai")
    def test_all_keys_exhausted_raises(self, mock_genai, _mock_sleep):
        """When all keys are exhausted, raises GeminiQuotaExhausted."""
        from google.api_core.exceptions import ResourceExhausted

        mock_model = MagicMock()
        mock_model.generate_content.side_effect = ResourceExhausted(
            "Quota exceeded: per_day limit"
        )
        mock_genai.GenerativeModel.return_value = mock_model

        cues = _make_cues(1)
        with pytest.raises(GeminiQuotaExhausted):
            translate_srt_cues(KEYS, cues, "English", "Spanish")
        # Both keys exhausted on both models (Pro tried first, then Flash fallback)
        for m in (DEFAULT_MODEL, FALLBACK_MODEL):
            assert KEYS[0] in _exhausted_for(m)
            assert KEYS[1] in _exhausted_for(m)

    @patch("translation.gemini_client.genai")
    def test_skips_already_exhausted_keys(self, mock_genai):
        """Already-exhausted keys are skipped."""
        _exhausted_for(DEFAULT_MODEL).add(KEYS[0])

        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "1: Hola"
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        cues = _make_cues(1)
        result_cues, _, _ki = translate_srt_cues(KEYS, cues, "English", "Spanish")
        assert result_cues[0].text == "Hola"
        # genai.configure should only be called with second key
        mock_genai.configure.assert_called_with(api_key=KEYS[1])


class TestHelpers:
    def test_reset_exhausted_keys(self):
        _exhausted_for(DEFAULT_MODEL).add("test-key")
        assert len(_exhausted_for(DEFAULT_MODEL)) == 1
        reset_exhausted_keys()
        assert len(_exhausted_keys) == 0

    def test_has_available_keys_all_available(self):
        assert has_available_keys(KEYS) is True

    def test_has_available_keys_none_available(self):
        for k in KEYS:
            _exhausted_for(DEFAULT_MODEL).add(k)
        assert has_available_keys(KEYS) is False

    def test_has_available_keys_partial(self):
        _exhausted_for(DEFAULT_MODEL).add(KEYS[0])
        assert has_available_keys(KEYS) is True

    def test_has_available_keys_empty_list(self):
        assert has_available_keys([]) is False
