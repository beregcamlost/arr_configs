"""Tests for DeepL client (mocked — no real API calls)."""

from unittest.mock import MagicMock, patch
from translation.deepl_client import translate_texts, translate_srt_cues
from translation.srt_parser import Cue


def test_translate_texts_basic():
    """translate_texts sends text to DeepL and returns translations."""
    mock_translator = MagicMock()
    mock_result1 = MagicMock()
    mock_result1.text = "Hola, mundo!"
    mock_result2 = MagicMock()
    mock_result2.text = "Esto es una prueba."
    mock_translator.translate_text.return_value = [mock_result1, mock_result2]

    results = translate_texts(
        mock_translator,
        texts=["Hello, world!", "This is a test."],
        source_lang="EN",
        target_lang="ES",
    )
    assert results == ["Hola, mundo!", "Esto es una prueba."]
    mock_translator.translate_text.assert_called_once()


def test_translate_srt_cues():
    """translate_srt_cues returns new cues with translated text."""
    cues = [
        Cue(index=1, start="00:00:01,000", end="00:00:02,000", text="Hello"),
        Cue(index=2, start="00:00:03,000", end="00:00:04,000", text="World"),
    ]
    mock_translator = MagicMock()
    mock_r1 = MagicMock(text="Hola")
    mock_r2 = MagicMock(text="Mundo")
    mock_translator.translate_text.return_value = [mock_r1, mock_r2]

    translated, chars = translate_srt_cues(
        mock_translator, cues, source_lang="EN", target_lang="ES"
    )
    assert len(translated) == 2
    assert translated[0].text == "Hola"
    assert translated[0].start == "00:00:01,000"
    assert translated[1].text == "Mundo"
    assert chars == 10  # len("Hello") + len("World")


def test_translate_srt_cues_batching():
    """Large cue lists are batched to stay under size limit."""
    # Create 100 cues with 100-char text each (10KB total)
    cues = [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text="A" * 100)
        for i in range(100)
    ]
    mock_translator = MagicMock()
    # Return matching number of results for each batch call
    def side_effect(texts, **kwargs):
        return [MagicMock(text=f"T{i}") for i in range(len(texts))]
    mock_translator.translate_text.side_effect = side_effect

    translated, chars = translate_srt_cues(
        mock_translator, cues, source_lang="EN", target_lang="ES",
        batch_size=4000,  # 4KB batches → ~40 cues per batch → 3 calls
    )
    assert len(translated) == 100
    assert chars == 10000
    assert mock_translator.translate_text.call_count >= 2


def test_translate_texts_empty():
    """translate_texts with empty list returns empty list."""
    mock_translator = MagicMock()
    results = translate_texts(mock_translator, [], "EN", "ES")
    assert results == []
    mock_translator.translate_text.assert_not_called()
