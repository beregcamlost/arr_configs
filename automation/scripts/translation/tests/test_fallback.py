"""Tests for DeepL -> Gemini -> Google Translate fallback logic."""

import os
from unittest.mock import MagicMock, patch, call
import translation.translator as translator_mod
from translation.tests.conftest import mock_ollama_response
# Disable date-based DeepL skip for tests
translator_mod.DEEPL_SKIP_UNTIL = None
from translation.translator import (
    _translate_cues_with_fallback, translate_file, _deepl_quota_exceeded,
)
from translation.config import Config, PROVIDER_OLLAMA
from translation.srt_parser import Cue


def _make_cfg(deepl_key="test:fx", google_enabled=True, gemini_keys=None, ollama_base_url=""):
    keys = [deepl_key] if deepl_key else []
    return Config(
        deepl_api_keys=keys,
        google_translate_enabled=google_enabled,
        gemini_api_keys=gemini_keys or [],
        ollama_base_url=ollama_base_url,
        state_dir="/tmp/test-state",
        bazarr_db="/tmp/test-bazarr.db",
    )


def _make_cues(n=3):
    return [
        Cue(index=i, start="00:00:01,000", end="00:00:02,000", text=f"Line {i}")
        for i in range(n)
    ]


class TestTranslateCuesWithFallback:
    """Tests for _translate_cues_with_fallback."""

    @patch("translation.translator.deepl_translate_srt_cues")
    def test_deepl_success(self, mock_deepl):
        """When DeepL succeeds, returns deepl provider."""
        translator_mod._deepl_quota_exceeded = False
        cfg = _make_cfg()
        cues = _make_cues()
        mock_deepl.return_value = (cues, 18, 0)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "deepl"
        assert chars == 18
        assert key_index == 0
        mock_deepl.assert_called_once()

    @patch("translation.google_client.create_translator")
    @patch("translation.google_client.translate_srt_cues")
    @patch("translation.translator.deepl_translate_srt_cues")
    def test_deepl_quota_falls_back_to_google(self, mock_deepl, mock_google, mock_create_google):
        """When DeepL raises DeeplKeysExhausted, falls back to Google."""
        from translation.deepl_client import DeeplKeysExhausted
        translator_mod._deepl_quota_exceeded = False
        cfg = _make_cfg()
        cues = _make_cues()
        mock_deepl.side_effect = DeeplKeysExhausted("all keys exhausted")
        mock_google.return_value = (cues, 18)
        mock_google_translator = MagicMock()
        mock_create_google.return_value = mock_google_translator

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "google"
        assert key_index is None
        assert translator_mod._deepl_quota_exceeded is True

    @patch("translation.google_client.translate_srt_cues")
    def test_google_only_mode(self, mock_google):
        """When no DeepL key, uses Google directly."""
        translator_mod._deepl_quota_exceeded = True
        cfg = _make_cfg(deepl_key="")
        cues = _make_cues()
        mock_google_translator = MagicMock()
        mock_google.return_value = (cues, 18)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", mock_google_translator
        )
        assert provider == "google"
        assert key_index is None

    def test_no_provider_available(self):
        """When no provider supports the language, raises ValueError."""
        translator_mod._deepl_quota_exceeded = True
        cfg = _make_cfg(deepl_key="", google_enabled=False)
        cues = _make_cues()

        import pytest
        with pytest.raises(ValueError, match="No translation provider"):
            _translate_cues_with_fallback(cfg, cues, "en", "es", None)

    @patch("translation.translator.deepl_translate_srt_cues")
    def test_deepl_non_quota_error_raises(self, mock_deepl):
        """Non-quota DeepL errors are re-raised without fallback."""
        translator_mod._deepl_quota_exceeded = False
        cfg = _make_cfg()
        cues = _make_cues()
        mock_deepl.side_effect = Exception("Network timeout")

        import pytest
        with pytest.raises(Exception, match="Network timeout"):
            _translate_cues_with_fallback(cfg, cues, "en", "es", None)

    @patch("translation.google_client.translate_srt_cues")
    def test_deepl_quota_flag_skips_deepl(self, mock_google):
        """Once quota flag is set, DeepL is skipped entirely."""
        translator_mod._deepl_quota_exceeded = True
        cfg = _make_cfg(deepl_key="test:fx")
        cues = _make_cues()
        mock_google_translator = MagicMock()
        mock_google.return_value = (cues, 18)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", mock_google_translator
        )
        assert provider == "google"  # skipped DeepL entirely
        assert key_index is None

    @patch("translation.gemini_client.translate_srt_cues")
    @patch("translation.translator.deepl_translate_srt_cues")
    def test_gemini_quota_falls_back_to_deepl(self, mock_deepl, mock_gemini):
        """When Gemini quota exhausted, falls back to DeepL."""
        from translation.gemini_client import GeminiQuotaExhausted
        translator_mod._deepl_quota_exceeded = False
        translator_mod._gemini_quota_exceeded = False
        cfg = _make_cfg(gemini_keys=["key1", "key2"])
        cues = _make_cues()
        mock_gemini.side_effect = GeminiQuotaExhausted("all keys exhausted")
        mock_deepl.return_value = (cues, 18, 0)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "deepl"
        assert translator_mod._gemini_quota_exceeded is True

    @patch("translation.translator.deepl_translate_srt_cues")
    @patch("translation.gemini_client.translate_srt_cues")
    def test_gemini_error_falls_back_to_deepl(self, mock_gemini, mock_deepl):
        """When Gemini raises a non-quota error, falls back to DeepL."""
        translator_mod._deepl_quota_exceeded = False
        translator_mod._gemini_quota_exceeded = False
        cfg = _make_cfg(gemini_keys=["key1", "key2"])
        cues = _make_cues()
        mock_gemini.side_effect = TypeError("the JSON object must be str, bytes or bytearray, not NoneType")
        mock_deepl.return_value = (cues, 18, 0)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "deepl"
        assert translator_mod._gemini_quota_exceeded is False  # flag NOT set for non-quota errors

    @patch("translation.google_client.create_translator")
    @patch("translation.google_client.translate_srt_cues")
    @patch("translation.gemini_client.translate_srt_cues")
    @patch("translation.translator.deepl_translate_srt_cues")
    def test_full_chain_gemini_deepl_google(self, mock_deepl, mock_gemini,
                                             mock_google, mock_create_google):
        """Full chain: Gemini fails -> DeepL fails -> Google succeeds."""
        from translation.gemini_client import GeminiQuotaExhausted
        translator_mod._deepl_quota_exceeded = False
        translator_mod._gemini_quota_exceeded = False
        cfg = _make_cfg(gemini_keys=["key1"])
        cues = _make_cues()
        mock_gemini.side_effect = GeminiQuotaExhausted("all keys exhausted")
        from translation.deepl_client import DeeplKeysExhausted
        mock_deepl.side_effect = DeeplKeysExhausted("all keys exhausted")
        mock_google.return_value = (cues, 18)
        mock_create_google.return_value = MagicMock()

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "google"
        assert key_index is None
        assert translator_mod._deepl_quota_exceeded is True
        assert translator_mod._gemini_quota_exceeded is True

    @patch("translation.google_client.translate_srt_cues")
    def test_gemini_quota_flag_skips_gemini(self, mock_google):
        """Once Gemini quota flag is set, Gemini is skipped."""
        translator_mod._deepl_quota_exceeded = True
        translator_mod._gemini_quota_exceeded = True
        cfg = _make_cfg(deepl_key="", gemini_keys=["key1"])
        cues = _make_cues()
        mock_google_translator = MagicMock()
        mock_google.return_value = (cues, 18)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", mock_google_translator
        )
        assert provider == "google"
        assert key_index is None

    @patch("translation.gemini_client.translate_srt_cues")
    def test_gemini_success_direct(self, mock_gemini):
        """Gemini succeeds when DeepL has no key."""
        translator_mod._deepl_quota_exceeded = True
        translator_mod._gemini_quota_exceeded = False
        cfg = _make_cfg(deepl_key="", gemini_keys=["key1", "key2"])
        cues = _make_cues()
        mock_gemini.return_value = (cues, 18, 0)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "gemini"
        assert chars == 18
        assert key_index == 0


class TestTranslateFileMarkers:
    """Tests for provider-specific marker files."""

    @patch("translation.translator._translate_cues_with_fallback")
    @patch("translation.translator._resolve_profile_for_path")
    @patch("translation.translator.get_profile_langs")
    @patch("translation.translator.find_missing_langs_on_disk")
    @patch("translation.translator.find_best_source_srt")
    @patch("translation.translator.is_on_cooldown")
    @patch("translation.translator.record_translation")
    def test_google_marker_file(self, mock_record, mock_cooldown, mock_source,
                                 mock_missing, mock_profile_langs, mock_profile,
                                 mock_translate, tmp_path):
        """Google translations create .gtranslate marker."""
        translator_mod._deepl_quota_exceeded = False
        cfg = _make_cfg()
        cfg.state_dir = str(tmp_path / "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        from translation.db import init_db
        init_db(os.path.join(cfg.state_dir, "translation_state.db"))

        video = tmp_path / "Movie.mkv"
        video.touch()
        source_srt = tmp_path / "Movie.en.srt"
        source_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")

        mock_profile.return_value = 1
        mock_profile_langs.return_value = ["es"]
        mock_missing.return_value = ["es"]
        mock_source.return_value = str(source_srt)
        mock_cooldown.return_value = False
        translated_cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hola")]
        mock_translate.return_value = (translated_cues, 5, "google", None)

        t, f = translate_file(cfg, str(video))
        assert len(t) == 1
        assert t[0]["provider"] == "google"
        assert os.path.exists(str(tmp_path / "Movie.es.srt.gtranslate"))
        assert not os.path.exists(str(tmp_path / "Movie.es.srt.deepl"))

    @patch("translation.translator._translate_cues_with_fallback")
    @patch("translation.translator._resolve_profile_for_path")
    @patch("translation.translator.get_profile_langs")
    @patch("translation.translator.find_missing_langs_on_disk")
    @patch("translation.translator.find_best_source_srt")
    @patch("translation.translator.is_on_cooldown")
    @patch("translation.translator.record_translation")
    def test_deepl_marker_file(self, mock_record, mock_cooldown, mock_source,
                                mock_missing, mock_profile_langs, mock_profile,
                                mock_translate, tmp_path):
        """DeepL translations create .deepl marker."""
        translator_mod._deepl_quota_exceeded = False
        cfg = _make_cfg()
        cfg.state_dir = str(tmp_path / "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        from translation.db import init_db
        init_db(os.path.join(cfg.state_dir, "translation_state.db"))

        video = tmp_path / "Movie.mkv"
        video.touch()
        source_srt = tmp_path / "Movie.en.srt"
        source_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")

        mock_profile.return_value = 1
        mock_profile_langs.return_value = ["es"]
        mock_missing.return_value = ["es"]
        mock_source.return_value = str(source_srt)
        mock_cooldown.return_value = False
        translated_cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hola")]
        mock_translate.return_value = (translated_cues, 5, "deepl", 0)

        t, f = translate_file(cfg, str(video))
        assert len(t) == 1
        assert t[0]["provider"] == "deepl"
        assert os.path.exists(str(tmp_path / "Movie.es.srt.deepl"))
        assert not os.path.exists(str(tmp_path / "Movie.es.srt.gtranslate"))

    @patch("translation.translator._translate_cues_with_fallback")
    @patch("translation.translator._resolve_profile_for_path")
    @patch("translation.translator.get_profile_langs")
    @patch("translation.translator.find_missing_langs_on_disk")
    @patch("translation.translator.find_best_source_srt")
    @patch("translation.translator.is_on_cooldown")
    @patch("translation.translator.record_translation")
    def test_gemini_marker_file(self, mock_record, mock_cooldown, mock_source,
                                 mock_missing, mock_profile_langs, mock_profile,
                                 mock_translate, tmp_path):
        """Gemini translations create .gemini marker."""
        translator_mod._deepl_quota_exceeded = False
        translator_mod._gemini_quota_exceeded = False
        cfg = _make_cfg(gemini_keys=["key1"])
        cfg.state_dir = str(tmp_path / "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        from translation.db import init_db
        init_db(os.path.join(cfg.state_dir, "translation_state.db"))

        video = tmp_path / "Movie.mkv"
        video.touch()
        source_srt = tmp_path / "Movie.en.srt"
        source_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")

        mock_profile.return_value = 1
        mock_profile_langs.return_value = ["es"]
        mock_missing.return_value = ["es"]
        mock_source.return_value = str(source_srt)
        mock_cooldown.return_value = False
        translated_cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hola")]
        mock_translate.return_value = (translated_cues, 5, "gemini", 2)

        t, f = translate_file(cfg, str(video))
        assert len(t) == 1
        assert t[0]["provider"] == "gemini"
        assert os.path.exists(str(tmp_path / "Movie.es.srt.gemini"))
        assert not os.path.exists(str(tmp_path / "Movie.es.srt.deepl"))
        assert not os.path.exists(str(tmp_path / "Movie.es.srt.gtranslate"))


class TestOllamaFallback:
    """Tests for Ollama in the fallback chain."""

    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_ollama_success(self, mock_urlopen):
        """When Ollama succeeds, returns ollama provider."""
        translator_mod._ollama_unavailable = False
        translator_mod._gemini_quota_exceeded = True
        translator_mod._deepl_quota_exceeded = True
        cfg = _make_cfg(deepl_key="", ollama_base_url="http://localhost:11434")
        cues = _make_cues()
        mock_urlopen.return_value = mock_ollama_response("1: Hola\n2: Mundo\n3: Adios")

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == PROVIDER_OLLAMA
        assert key_index is None

    @patch("translation.ollama_client.urllib.request.urlopen")
    @patch("translation.gemini_client.translate_srt_cues")
    def test_ollama_unavailable_falls_back_to_gemini(self, mock_gemini, mock_urlopen):
        """When Ollama is unreachable, falls back to Gemini."""
        from urllib.error import URLError
        translator_mod._ollama_unavailable = False
        translator_mod._gemini_quota_exceeded = False
        translator_mod._deepl_quota_exceeded = True
        cfg = _make_cfg(deepl_key="", ollama_base_url="http://localhost:11434", gemini_keys=["key1"])
        cues = _make_cues()
        mock_urlopen.side_effect = URLError("Connection refused")
        mock_gemini.return_value = (cues, 18, 0)

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "gemini"
        assert translator_mod._ollama_unavailable is True

    def test_ollama_flag_skips_ollama(self):
        """Once unavailable flag is set, Ollama is skipped."""
        translator_mod._ollama_unavailable = True
        translator_mod._deepl_quota_exceeded = True
        translator_mod._gemini_quota_exceeded = True
        cfg = _make_cfg(deepl_key="", ollama_base_url="http://localhost:11434")
        cues = _make_cues()
        mock_google_translator = MagicMock()

        with patch("translation.google_client.translate_srt_cues") as mock_google:
            mock_google.return_value = (cues, 18)
            result_cues, chars, provider, key_index = _translate_cues_with_fallback(
                cfg, cues, "en", "es", mock_google_translator
            )
        assert provider == "google"

    @patch("translation.google_client.create_translator")
    @patch("translation.google_client.translate_srt_cues")
    @patch("translation.translator.deepl_translate_srt_cues")
    @patch("translation.gemini_client.translate_srt_cues")
    @patch("translation.ollama_client.urllib.request.urlopen")
    def test_full_chain_all_fail_to_google(self, mock_urlopen, mock_gemini,
                                            mock_deepl, mock_google, mock_create_google):
        """Full chain: Ollama -> Gemini -> DeepL -> Google all fail except Google."""
        from urllib.error import URLError
        from translation.gemini_client import GeminiQuotaExhausted
        from translation.deepl_client import DeeplKeysExhausted
        translator_mod._ollama_unavailable = False
        translator_mod._deepl_quota_exceeded = False
        translator_mod._gemini_quota_exceeded = False
        cfg = _make_cfg(ollama_base_url="http://localhost:11434", gemini_keys=["key1"])
        cues = _make_cues()
        mock_urlopen.side_effect = URLError("Connection refused")
        mock_gemini.side_effect = GeminiQuotaExhausted("all keys exhausted")
        mock_deepl.side_effect = DeeplKeysExhausted("all keys exhausted")
        mock_google.return_value = (cues, 18)
        mock_create_google.return_value = MagicMock()

        result_cues, chars, provider, key_index = _translate_cues_with_fallback(
            cfg, cues, "en", "es", None
        )
        assert provider == "google"
        assert translator_mod._ollama_unavailable is True
        assert translator_mod._gemini_quota_exceeded is True
        assert translator_mod._deepl_quota_exceeded is True


class TestOllamaMarkerFile:
    """Tests for Ollama marker file creation."""

    @patch("translation.translator._translate_cues_with_fallback")
    @patch("translation.translator._resolve_profile_for_path")
    @patch("translation.translator.get_profile_langs")
    @patch("translation.translator.find_missing_langs_on_disk")
    @patch("translation.translator.find_best_source_srt")
    @patch("translation.translator.is_on_cooldown")
    @patch("translation.translator.record_translation")
    def test_ollama_marker_file(self, mock_record, mock_cooldown, mock_source,
                                 mock_missing, mock_profile_langs, mock_profile,
                                 mock_translate, tmp_path):
        """Ollama translations create .ollama marker."""
        translator_mod._ollama_unavailable = False
        cfg = _make_cfg(ollama_base_url="http://localhost:11434")
        cfg.state_dir = str(tmp_path / "state")
        os.makedirs(cfg.state_dir, exist_ok=True)
        from translation.db import init_db
        init_db(os.path.join(cfg.state_dir, "translation_state.db"))

        video = tmp_path / "Movie.mkv"
        video.touch()
        source_srt = tmp_path / "Movie.en.srt"
        source_srt.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n")

        mock_profile.return_value = 1
        mock_profile_langs.return_value = ["es"]
        mock_missing.return_value = ["es"]
        mock_source.return_value = str(source_srt)
        mock_cooldown.return_value = False
        translated_cues = [Cue(1, "00:00:01,000", "00:00:02,000", "Hola")]
        mock_translate.return_value = (translated_cues, 5, "ollama", None)

        t, f = translate_file(cfg, str(video))
        assert len(t) == 1
        assert t[0]["provider"] == PROVIDER_OLLAMA
        assert os.path.exists(str(tmp_path / "Movie.es.srt.ollama"))
        assert not os.path.exists(str(tmp_path / "Movie.es.srt.deepl"))
        assert not os.path.exists(str(tmp_path / "Movie.es.srt.gemini"))
        assert not os.path.exists(str(tmp_path / "Movie.es.srt.gtranslate"))
