"""Tests for local_proofreader (mocked Ollama)."""

import json
import unittest
from unittest.mock import patch, MagicMock

from translation.local_proofreader import proofread_cues, is_proofreader_available


def _mock_generate_response(text: str) -> MagicMock:
    """Build a mock urlopen context manager that returns a /api/generate response."""
    resp = MagicMock()
    resp.read.return_value = json.dumps({"response": text}).encode("utf-8")
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestProofreadCues(unittest.TestCase):
    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_accepts_reasonable_correction(self, mock_urlopen):
        mock_urlopen.return_value = _mock_generate_response("Ella estaba emocionada.")
        result = proofread_cues(
            source_texts=["She was excited."],
            translated_texts=["Él estaba emocionada."],
            indices=[0],
            base_url="http://test:11434",
        )
        self.assertEqual(result[0], "Ella estaba emocionada.")

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_rejects_hallucination(self, mock_urlopen):
        # Proofreader returns something totally unrelated
        resp = MagicMock()
        resp.read.return_value = b'{"response":"No tengo idea de nada aqui realmente."}'
        mock_urlopen.return_value.__enter__.return_value = resp
        result = proofread_cues(
            source_texts=["Hi."],
            translated_texts=["Hola."],
            indices=[0],
            base_url="http://test:11434",
        )
        self.assertEqual(result[0], "Hola.")  # original preserved

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_only_proofreads_given_indices(self, mock_urlopen):
        mock_urlopen.return_value = _mock_generate_response("Corregido.")
        result = proofread_cues(
            source_texts=["A", "B", "C"],
            translated_texts=["uno", "dos", "tres"],
            indices=[1],
            base_url="http://test:11434",
        )
        self.assertEqual(result[0], "uno")   # unchanged
        self.assertEqual(result[2], "tres")  # unchanged

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_returns_unchanged_on_network_error(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        result = proofread_cues(
            source_texts=["Hello."],
            translated_texts=["Hola."],
            indices=[0],
            base_url="http://test:11434",
        )
        self.assertEqual(result[0], "Hola.")

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_strips_label_prefix_from_response(self, mock_urlopen):
        mock_urlopen.return_value = _mock_generate_response("Corrected: Ella estaba emocionada.")
        result = proofread_cues(
            source_texts=["She was excited."],
            translated_texts=["Él estaba emocionada."],
            indices=[0],
            base_url="http://test:11434",
        )
        self.assertEqual(result[0], "Ella estaba emocionada.")

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_strips_surrounding_quotes(self, mock_urlopen):
        mock_urlopen.return_value = _mock_generate_response('"Ella estaba emocionada."')
        result = proofread_cues(
            source_texts=["She was excited."],
            translated_texts=["Él estaba emocionada."],
            indices=[0],
            base_url="http://test:11434",
        )
        self.assertEqual(result[0], "Ella estaba emocionada.")

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_empty_indices_returns_copy(self, mock_urlopen):
        result = proofread_cues(
            source_texts=["Hello."],
            translated_texts=["Hola."],
            indices=[],
            base_url="http://test:11434",
        )
        self.assertEqual(result, ["Hola."])
        mock_urlopen.assert_not_called()

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_unchanged_text_kept_as_is(self, mock_urlopen):
        # Proofreader returns text identical to original — should keep it
        mock_urlopen.return_value = _mock_generate_response("No lo olvidaré.")
        result = proofread_cues(
            source_texts=["I won't forget."],
            translated_texts=["No lo olvidaré."],
            indices=[0],
            base_url="http://test:11434",
        )
        self.assertEqual(result[0], "No lo olvidaré.")


class TestIsProofreaderAvailable(unittest.TestCase):
    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_returns_true_when_model_listed(self, mock_urlopen):
        tags_resp = MagicMock()
        tags_resp.read.return_value = json.dumps({
            "models": [
                {"name": "phi4-mini-proofread:latest"},
                {"name": "phi4-mini-subs:latest"},
            ]
        }).encode("utf-8")
        tags_resp.__enter__ = lambda s: s
        tags_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = tags_resp
        self.assertTrue(is_proofreader_available("http://test:11434"))

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_returns_false_when_model_not_listed(self, mock_urlopen):
        tags_resp = MagicMock()
        tags_resp.read.return_value = json.dumps({
            "models": [
                {"name": "phi4-mini-subs:latest"},
            ]
        }).encode("utf-8")
        tags_resp.__enter__ = lambda s: s
        tags_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = tags_resp
        self.assertFalse(is_proofreader_available("http://test:11434"))

    @patch("translation.local_proofreader.urllib.request.urlopen")
    def test_returns_false_on_connection_error(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        self.assertFalse(is_proofreader_available("http://test:11434"))


if __name__ == "__main__":
    unittest.main()
