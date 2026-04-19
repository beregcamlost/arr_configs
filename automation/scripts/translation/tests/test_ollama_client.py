"""Tests for Ollama client for local subtitle translation."""

import json
import pytest
from unittest.mock import patch, MagicMock
from urllib.error import URLError

from translation.ollama_client import (
    translate_srt_cues,
    _call_ollama,
    OllamaUnavailable,
    DEFAULT_MODEL,
)
from translation.prompt_utils import build_prompt, parse_response
from translation.srt_parser import Cue
from translation.tests.conftest import mock_ollama_response


BASE_URL = "http://localhost:11434"


def _make_cues(n=3):
    return [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text=f"Line {i}")
        for i in range(1, n + 1)
    ]


class TestBuildPrompt:
    def test_basic_prompt(self):
        cues = _make_cues(2)
        prompt = build_prompt(cues, "English", "Spanish")
        assert "Translate from English to Spanish:" in prompt
        assert "1: Line 1" in prompt
        assert "2: Line 2" in prompt

    def test_multiline_cue_encodes_br(self):
        cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hello\nWorld")]
        prompt = build_prompt(cues, "English", "Spanish")
        assert "Hello<br>World" in prompt
        assert "Hello\nWorld" not in prompt.split("\n", 1)[1]


class TestParseResponse:
    def test_basic_numbered_response(self):
        response = "1: Hola\n2: Mundo\n3: Adios"
        result = parse_response(response, 3)
        assert result == ["Hola", "Mundo", "Adios"]

    def test_strips_various_prefixes(self):
        response = "1. Hola\n2) Mundo\n3- Adios"
        result = parse_response(response, 3)
        assert result == ["Hola", "Mundo", "Adios"]

    def test_decodes_br_to_newlines(self):
        response = "1: Hola<br>Mundo"
        result = parse_response(response, 1)
        assert result == ["Hola\nMundo"]

    def test_pads_missing_results(self):
        response = "1: Hola"
        result = parse_response(response, 3)
        assert len(result) == 3
        assert result[0] == "Hola"
        assert result[1] == ""

    def test_truncates_extra_results(self):
        response = "1: Hola\n2: Mundo\n3: Extra\n4: More"
        result = parse_response(response, 2)
        assert len(result) == 2

    def test_skips_empty_lines(self):
        response = "1: Hola\n\n2: Mundo"
        result = parse_response(response, 2)
        assert result == ["Hola", "Mundo"]


class TestCallOllama:
    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_successful_call(self, mock_urlopen):
        mock_urlopen.return_value = mock_ollama_response("1: Hola")
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        result = _call_ollama(BASE_URL, "qwen2.5:7b", msgs)
        assert result == "1: Hola"

    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_connection_error_raises(self, mock_urlopen):
        mock_urlopen.side_effect = URLError("Connection refused")
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        with pytest.raises(OllamaUnavailable, match="unreachable"):
            _call_ollama(BASE_URL, "qwen2.5:7b", msgs)

    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_bad_json_raises(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        with pytest.raises(OllamaUnavailable, match="bad response"):
            _call_ollama(BASE_URL, "qwen2.5:7b", msgs)

    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_missing_message_key_raises(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"error": "model not found"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        with pytest.raises(OllamaUnavailable, match="bad response"):
            _call_ollama(BASE_URL, "qwen2.5:7b", msgs)

    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_sends_correct_payload(self, mock_urlopen):
        mock_urlopen.return_value = mock_ollama_response("1: Hola")
        messages = [
            {"role": "system", "content": "Be a translator"},
            {"role": "user", "content": "Translate this"},
        ]
        _call_ollama(BASE_URL, "qwen2.5:7b", messages)

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode("utf-8"))
        assert payload["model"] == "qwen2.5:7b"
        assert payload["stream"] is False
        assert payload["options"]["temperature"] == 0
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][0]["content"] == "Be a translator"
        assert payload["messages"][1]["role"] == "user"
        assert payload["messages"][1]["content"] == "Translate this"
        assert req.full_url == "http://localhost:11434/api/chat"


class TestTranslateSrtCues:
    @patch("translation.spell_check.validate_translated_cues", return_value=[])
    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_basic_translation(self, mock_urlopen, _mock_validate):
        mock_urlopen.return_value = mock_ollama_response("1: Hola\n2: Mundo\n3: Adios")
        cues = _make_cues(3)
        result_cues, chars, key_index = translate_srt_cues(
            BASE_URL, cues, "English", "Spanish"
        )
        assert len(result_cues) == 3
        assert result_cues[0].text == "Hola"
        assert result_cues[1].text == "Mundo"
        assert result_cues[2].text == "Adios"
        assert chars == sum(len(c.text) for c in cues)
        assert key_index is None

    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_empty_cues(self, mock_urlopen):
        result_cues, chars, key_index = translate_srt_cues(
            BASE_URL, [], "English", "Spanish"
        )
        assert result_cues == []
        assert chars == 0
        assert key_index is None
        mock_urlopen.assert_not_called()

    @patch("translation.spell_check.validate_translated_cues", return_value=[])
    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_multiline_cue_roundtrip(self, mock_urlopen, _mock_validate):
        mock_urlopen.return_value = mock_ollama_response("1: Hola<br>Mundo")
        cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hello\nWorld")]
        result_cues, chars, key_index = translate_srt_cues(
            BASE_URL, cues, "English", "Spanish"
        )
        assert result_cues[0].text == "Hola\nMundo"

    @patch("translation.spell_check.validate_translated_cues", return_value=[])
    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_preserves_cue_timing(self, mock_urlopen, _mock_validate):
        mock_urlopen.return_value = mock_ollama_response("1: Hola")
        cues = [Cue(42, "00:01:23,456", "00:01:25,789", "Hello")]
        result_cues, _, _ = translate_srt_cues(
            BASE_URL, cues, "English", "Spanish"
        )
        assert result_cues[0].index == 42
        assert result_cues[0].start == "00:01:23,456"
        assert result_cues[0].end == "00:01:25,789"

    @patch("translation.spell_check.validate_translated_cues", return_value=[])
    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_connection_error_raises_unavailable(self, mock_urlopen, _mock_validate):
        mock_urlopen.side_effect = URLError("Connection refused")
        cues = _make_cues(1)
        with pytest.raises(OllamaUnavailable):
            translate_srt_cues(BASE_URL, cues, "English", "Spanish")

    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_batching(self, mock_urlopen):
        """Large input is split into batches."""
        mock_urlopen.return_value = mock_ollama_response("1: Hola")
        # Create cues with enough text to force multiple batches (batch_size=20)
        cues = [
            Cue(index=i, start="00:00:01,000", end="00:00:02,000",
                text=f"This is a longer line number {i}")
            for i in range(1, 4)
        ]
        result_cues, chars, _ = translate_srt_cues(
            BASE_URL, cues, "English", "Spanish", batch_size=20
        )
        # Should have called urlopen multiple times (one per batch)
        assert mock_urlopen.call_count > 1
