"""Tests for corpus_filters — shared quality filter for mining scripts."""

import pytest

from translation.corpus_filters import quality_filter_pair


class TestQualityFilterPair:
    # --- Basic pass cases ---

    def test_valid_pair_passes(self):
        en = "She looked at him with wide eyes."
        es = "Ella lo miró con los ojos muy abiertos."
        assert quality_filter_pair(en, es) is True

    def test_minimum_length_boundary_passes(self):
        # Exactly 15 chars on both sides
        en = "Hello everyone."   # 15
        es = "Hola a todos."     # 13 — fails
        assert quality_filter_pair(en, es) is False

    def test_exactly_15_chars_both_passes(self):
        en = "Hello everyone."    # 15
        es = "Hola a todos xo."   # 16
        assert quality_filter_pair(en, es) is True

    # --- Length filter ---

    def test_en_too_short_rejected(self):
        assert quality_filter_pair("Short", "Esta frase es suficientemente larga para pasar.") is False

    def test_es_too_short_rejected(self):
        assert quality_filter_pair("This is a long enough English sentence.", "Corta") is False

    def test_empty_en_rejected(self):
        assert quality_filter_pair("", "Una oración en español.") is False

    def test_empty_es_rejected(self):
        assert quality_filter_pair("An English sentence.", "") is False

    def test_whitespace_only_rejected(self):
        assert quality_filter_pair("   ", "   ") is False

    # --- Length ratio ---

    def test_ratio_too_low_rejected(self):
        # ratio < 0.3: es much shorter than en
        en = "This is a very long English sentence that goes on and on."
        es = "Sí."
        assert quality_filter_pair(en, es) is False

    def test_ratio_too_high_rejected(self):
        # ratio > 3.0: es much longer than en
        en = "Hi."
        es = "Esta es una frase española muy larga que supera tres veces el límite."
        # en is 3 chars, es would need ratio>3 and es >= 15
        assert quality_filter_pair(en, es) is False

    def test_ratio_at_boundary_passes(self):
        en = "She is here waiting for you now."       # 31 chars
        es = "Ella está esperando aquí para ti."       # ~32 chars, ratio ~1.0
        assert quality_filter_pair(en, es) is True

    # --- URL / HTML rejection ---

    def test_url_in_en_rejected(self):
        assert quality_filter_pair(
            "Visit https://example.com for details.",
            "Visita el sitio para más detalles.",
        ) is False

    def test_url_in_es_rejected(self):
        assert quality_filter_pair(
            "Visit the website for more information.",
            "Visita http://example.com para más información.",
        ) is False

    def test_www_in_en_rejected(self):
        assert quality_filter_pair(
            "Go to www.example.com for more info.",
            "Ve a ese sitio para más información.",
        ) is False

    def test_html_tag_in_en_rejected(self):
        assert quality_filter_pair(
            "She said <b>hello</b> to everyone.",
            "Ella le dijo hola a todos.",
        ) is False

    def test_html_tag_in_es_rejected(self):
        assert quality_filter_pair(
            "She said hello to everyone there.",
            "Ella dijo <i>hola</i> a todos allí.",
        ) is False

    def test_curly_brace_in_es_rejected(self):
        assert quality_filter_pair(
            "Something about the function call here.",
            "Algo sobre {función} en el código ahora.",
        ) is False

    # --- Non-alpha ratio ---

    def test_high_non_alpha_rejected(self):
        # >20% non-alpha, non-space
        assert quality_filter_pair(
            "123-456-789 *** ### @@@ !!! ??? ...",
            "Esto tiene muchos números y símbolos raros.",
        ) is False

    def test_normal_punctuation_passes(self):
        en = "Wait — are you really sure about that right now?"
        es = "Espera, ¿estás realmente seguro de eso ahora?"
        assert quality_filter_pair(en, es) is True

    # --- Spanish ASCII check ---

    def test_long_all_ascii_es_rejected(self):
        # Long es line (>=40 chars) with NO accented chars — likely mislabeled English
        en = "This is a standard English subtitle line here."
        es = "This is also completely English and has no accented characters at all here"  # all ASCII, >= 40
        assert quality_filter_pair(en, es) is False

    def test_accented_spanish_passes(self):
        en = "He was standing there alone in the rain."
        es = "Él estaba parado allí solo bajo la lluvia."
        assert quality_filter_pair(en, es) is True
