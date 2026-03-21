"""Tests for Google Translate client (mocked — no real API calls)."""

from unittest.mock import MagicMock, patch
from translation.google_client import translate_texts, translate_srt_cues
from translation.srt_parser import Cue


def test_translate_texts_basic():
    """translate_texts sends each text individually to Google and returns translations."""
    mock_translator = MagicMock()
    mock_result1 = MagicMock()
    mock_result1.text = "Hola, mundo!"
    mock_result2 = MagicMock()
    mock_result2.text = "Esto es una prueba."
    mock_translator.translate.side_effect = [mock_result1, mock_result2]

    results = translate_texts(
        mock_translator,
        texts=["Hello, world!", "This is a test."],
        source_lang="en",
        target_lang="es",
    )
    assert results == ["Hola, mundo!", "Esto es una prueba."]
    assert mock_translator.translate.call_count == 2


def test_translate_texts_single():
    """translate_texts handles single text."""
    mock_translator = MagicMock()
    mock_result = MagicMock()
    mock_result.text = "Hola"
    mock_translator.translate.return_value = mock_result

    results = translate_texts(
        mock_translator,
        texts=["Hello"],
        source_lang="en",
        target_lang="es",
    )
    assert results == ["Hola"]


def test_translate_texts_error_fallback():
    """translate_texts keeps original text when Google raises any exception (TypeError, HTTP errors, etc.)."""
    mock_translator = MagicMock()
    mock_result = MagicMock()
    mock_result.text = "Hola"
    mock_translator.translate.side_effect = [
        mock_result,
        TypeError("'NoneType' object is not iterable"),
        mock_result,
        Exception("429 Too Many Requests"),
        mock_result,
    ]

    results = translate_texts(
        mock_translator,
        texts=["Hello", "broken cue", "Hello", "rate limited", "Hello"],
        source_lang="en",
        target_lang="es",
    )
    assert results == ["Hola", "broken cue", "Hola", "rate limited", "Hola"]


def test_translate_srt_cues():
    """translate_srt_cues returns new cues with translated text."""
    cues = [
        Cue(index=1, start="00:00:01,000", end="00:00:02,000", text="Hello"),
        Cue(index=2, start="00:00:03,000", end="00:00:04,000", text="World"),
    ]
    mock_translator = MagicMock()
    mock_translator.translate.side_effect = [
        MagicMock(text="Hola"),
        MagicMock(text="Mundo"),
    ]

    translated, chars = translate_srt_cues(
        mock_translator, cues, source_lang="en", target_lang="es"
    )
    assert len(translated) == 2
    assert translated[0].text == "Hola"
    assert translated[0].start == "00:00:01,000"
    assert translated[1].text == "Mundo"
    assert chars == 10  # len("Hello") + len("World")


def test_translate_srt_cues_batching():
    """Large cue lists are batched to stay under size limit."""
    cues = [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text="A" * 100)
        for i in range(100)
    ]
    mock_translator = MagicMock()

    def side_effect(text, **kwargs):
        return MagicMock(text=f"T_{text[:5]}")

    mock_translator.translate.side_effect = side_effect

    translated, chars = translate_srt_cues(
        mock_translator, cues, source_lang="en", target_lang="es",
        batch_size=4000,
    )
    assert len(translated) == 100
    assert chars == 10000
    assert mock_translator.translate.call_count == 100


def test_translate_texts_empty():
    """translate_texts with empty list returns empty list."""
    mock_translator = MagicMock()
    results = translate_texts(mock_translator, [], "en", "es")
    assert results == []
    mock_translator.translate.assert_not_called()


def test_translate_srt_cues_empty():
    """translate_srt_cues with empty list returns empty list."""
    mock_translator = MagicMock()
    translated, chars = translate_srt_cues(mock_translator, [], "en", "es")
    assert translated == []
    assert chars == 0
    mock_translator.translate.assert_not_called()
