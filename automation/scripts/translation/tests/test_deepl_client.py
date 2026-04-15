"""Tests for DeepL client (mocked — no real API calls)."""

import pytest
from unittest.mock import MagicMock, patch
import deepl

from translation.deepl_client import (
    translate_texts,
    translate_srt_cues,
    DeeplKeysExhausted,
    reset_exhausted_keys,
    _exhausted_keys,
)
from translation.srt_parser import Cue


@pytest.fixture(autouse=True)
def clear_state():
    """Reset exhausted keys before each test to avoid cross-test pollution."""
    reset_exhausted_keys()
    yield
    reset_exhausted_keys()


def _make_translator(texts_return=None, side_effect=None):
    """Create a mock Translator whose translate_text behaves as specified."""
    mock = MagicMock()
    if side_effect is not None:
        mock.translate_text.side_effect = side_effect
    elif texts_return is not None:
        mock.translate_text.return_value = [MagicMock(text=t) for t in texts_return]
    return mock


@patch("translation.deepl_client._get_translator")
def test_translate_texts_basic(mock_get):
    """translate_texts sends text to DeepL and returns (translations, key_index)."""
    mock_translator = _make_translator(texts_return=["Hola, mundo!", "Esto es una prueba."])
    mock_get.return_value = mock_translator

    results, key_index = translate_texts(
        api_keys=["key1:fx"],
        texts=["Hello, world!", "This is a test."],
        source_lang="EN",
        target_lang="ES",
    )
    assert results == ["Hola, mundo!", "Esto es una prueba."]
    assert key_index == 0
    mock_translator.translate_text.assert_called_once()


@patch("translation.deepl_client._get_translator")
def test_translate_srt_cues(mock_get):
    """translate_srt_cues returns new cues with translated text."""
    cues = [
        Cue(index=1, start="00:00:01,000", end="00:00:02,000", text="Hello"),
        Cue(index=2, start="00:00:03,000", end="00:00:04,000", text="World"),
    ]
    mock_translator = _make_translator(texts_return=["Hola", "Mundo"])
    mock_get.return_value = mock_translator

    translated, chars, key_index = translate_srt_cues(
        api_keys=["key1:fx"], cues=cues, source_lang="EN", target_lang="ES"
    )
    assert len(translated) == 2
    assert translated[0].text == "Hola"
    assert translated[0].start == "00:00:01,000"
    assert translated[1].text == "Mundo"
    assert chars == 10  # len("Hello") + len("World")
    assert key_index == 0


@patch("translation.deepl_client._get_translator")
def test_translate_srt_cues_batching(mock_get):
    """Large cue lists are batched to stay under size limit."""
    cues = [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text="A" * 100)
        for i in range(100)
    ]

    def side_effect(texts, **kwargs):
        return [MagicMock(text=f"T{i}") for i in range(len(texts))]

    mock_translator = MagicMock()
    mock_translator.translate_text.side_effect = side_effect
    mock_get.return_value = mock_translator

    translated, chars, key_index = translate_srt_cues(
        api_keys=["key1:fx"], cues=cues, source_lang="EN", target_lang="ES",
        batch_size=4000,
    )
    assert len(translated) == 100
    assert chars == 10000
    assert mock_translator.translate_text.call_count >= 2


@patch("translation.deepl_client._get_translator")
def test_translate_texts_empty(mock_get):
    """translate_texts with empty list returns empty list."""
    mock_translator = MagicMock()
    mock_get.return_value = mock_translator
    results, key_index = translate_texts(api_keys=["key1:fx"], texts=[], source_lang="EN", target_lang="ES")
    assert results == []
    assert key_index == 0
    mock_translator.translate_text.assert_not_called()


@patch("translation.deepl_client._get_translator")
def test_failover_on_disabled_key(mock_get):
    """First key raises AuthorizationException, second key succeeds."""
    bad_translator = MagicMock()
    bad_translator.translate_text.side_effect = deepl.AuthorizationException("disabled")
    good_translator = _make_translator(texts_return=["Hola"])
    mock_get.side_effect = [bad_translator, good_translator]

    results, key_index = translate_texts(
        api_keys=["badkey123:fx", "goodky456:fx"],
        texts=["Hello"],
        source_lang="EN",
        target_lang="ES",
    )
    assert results == ["Hola"]
    assert key_index == 1  # second key (index 1) succeeded
    assert "badkey123:fx" in _exhausted_keys
    assert "goodky456:fx" not in _exhausted_keys


@patch("translation.deepl_client._get_translator")
def test_all_keys_exhausted_raises(mock_get):
    """All keys raise AuthorizationException → DeeplKeysExhausted raised."""
    bad = MagicMock()
    bad.translate_text.side_effect = deepl.AuthorizationException("disabled")
    mock_get.return_value = bad

    with pytest.raises(DeeplKeysExhausted):
        translate_texts(
            api_keys=["key1:fx", "key2:fx"],
            texts=["Hello"],
            source_lang="EN",
            target_lang="ES",
        )
    assert "key1:fx" in _exhausted_keys
    assert "key2:fx" in _exhausted_keys


@patch("translation.deepl_client._get_translator")
def test_quota_exceeded_marks_key_exhausted(mock_get):
    """QuotaExceededException marks key exhausted and raises DeeplKeysExhausted."""
    quota_translator = MagicMock()
    quota_translator.translate_text.side_effect = deepl.QuotaExceededException("quota")
    mock_get.return_value = quota_translator

    with pytest.raises(DeeplKeysExhausted):
        translate_texts(
            api_keys=["quotakey:fx"],
            texts=["Hello"],
            source_lang="EN",
            target_lang="ES",
        )
    assert "quotakey:fx" in _exhausted_keys


def test_reset_exhausted_keys_clears_state():
    """Marking a key exhausted then calling reset clears the state."""
    _exhausted_keys.add("somekey:fx")
    assert "somekey:fx" in _exhausted_keys
    reset_exhausted_keys()
    assert "somekey:fx" not in _exhausted_keys
    assert len(_exhausted_keys) == 0


@patch("translation.deepl_client._get_translator")
def test_rate_limit_retries_then_exhausts(mock_get):
    """TooManyRequestsException retries once then marks key exhausted."""
    rate_translator = MagicMock()
    rate_translator.translate_text.side_effect = deepl.TooManyRequestsException("rate limited")
    mock_get.return_value = rate_translator

    with patch("translation.deepl_client.time.sleep") as mock_sleep:
        with pytest.raises(DeeplKeysExhausted):
            translate_texts(
                api_keys=["ratekey:fx"],
                texts=["Hello"],
                source_lang="EN",
                target_lang="ES",
            )
        mock_sleep.assert_called_once_with(4)

    assert "ratekey:fx" in _exhausted_keys
    assert rate_translator.translate_text.call_count == 2


@patch("translation.deepl_client._get_translator")
def test_rate_limit_succeeds_on_retry(mock_get):
    """TooManyRequestsException on first attempt succeeds on retry."""
    translator = MagicMock()
    translator.translate_text.side_effect = [
        deepl.TooManyRequestsException("rate limited"),
        [MagicMock(text="Hola")],
    ]
    mock_get.return_value = translator

    with patch("translation.deepl_client.time.sleep"):
        results, key_index = translate_texts(
            api_keys=["key1:fx"],
            texts=["Hello"],
            source_lang="EN",
            target_lang="ES",
        )
    assert results == ["Hola"]
    assert key_index == 0
    assert "key1:fx" not in _exhausted_keys


@patch("translation.deepl_client._get_translator")
def test_transient_deepl_error_tries_next_key(mock_get):
    """Generic DeepLException on first key tries the next key without marking exhausted."""
    transient_translator = MagicMock()
    transient_translator.translate_text.side_effect = deepl.DeepLException("network error")
    good_translator = _make_translator(texts_return=["Hola"])
    mock_get.side_effect = [transient_translator, good_translator]

    results, key_index = translate_texts(
        api_keys=["key1:fx", "key2:fx"],
        texts=["Hello"],
        source_lang="EN",
        target_lang="ES",
    )
    assert results == ["Hola"]
    assert key_index == 1  # key2 succeeded (index 1)
    assert "key1:fx" not in _exhausted_keys
    assert "key2:fx" not in _exhausted_keys


@patch("translation.deepl_client._get_translator")
def test_already_exhausted_key_is_skipped(mock_get):
    """A key already in _exhausted_keys is skipped without an API call."""
    _exhausted_keys.add("key1:fx")
    good_translator = _make_translator(texts_return=["Hola"])
    mock_get.return_value = good_translator

    results, key_index = translate_texts(
        api_keys=["key1:fx", "key2:fx"],
        texts=["Hello"],
        source_lang="EN",
        target_lang="ES",
    )
    assert results == ["Hola"]
    assert key_index == 1  # key2 is at position 1
    # _get_translator only called for key2 — key1 was skipped
    mock_get.assert_called_once_with("key2:fx")
